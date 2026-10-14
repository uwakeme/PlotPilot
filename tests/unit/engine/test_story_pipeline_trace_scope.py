"""章节管线的 trace 作用域。

run_chapter 期间 ambient trace 必须带 novel_id，退出后必须还原成进入前的那个对象。
还原不是可有可无的：守护进程的任务上下文是长生命周期的，残留的 trace 会把上一本书的
novel_id 泄漏给下一本。
"""
from types import SimpleNamespace

import pytest

from application.ai.trace_context import (
    TraceContext,
    get_current_trace,
    reset_current_trace,
    set_current_trace,
)
from engine.pipeline.base import BaseStoryPipeline, _enter_chapter_trace, _exit_chapter_trace
from engine.pipeline.steps import StepResult


def test_enter_installs_fresh_trace_with_novel_id():
    ctx = SimpleNamespace(novel_id="n1")
    token = _enter_chapter_trace(ctx)
    try:
        trace = get_current_trace()
        assert trace.novel_id == "n1"
        assert trace.stage == "pipeline.chapter"
        assert trace.stage_label == "章节生成"
    finally:
        _exit_chapter_trace(token)


def test_enter_does_not_mutate_a_pre_existing_trace():
    """残留的 ambient trace 必须被整体替换，而不是就地改 novel_id。

    这是跨书泄漏的防线：就地改只会把空 novel_id 补上，不会覆盖上一本书的 id。
    """
    stale = TraceContext.create(novel_id="stale-novel", stage="old", stage_label="旧")
    outer = set_current_trace(stale)
    try:
        token = _enter_chapter_trace(SimpleNamespace(novel_id="n1"))
        try:
            inside = get_current_trace()
            assert inside is not stale
            assert inside.novel_id == "n1"
        finally:
            _exit_chapter_trace(token)
        assert stale.novel_id == "stale-novel"  # 原对象没被动过
        assert get_current_trace() is stale      # 退出后还原
    finally:
        reset_current_trace(outer)


@pytest.mark.asyncio
async def test_run_chapter_scopes_trace_for_the_duration_of_the_pipeline():
    """集成点：run_chapter 确实进入了作用域，并在返回后还原。

    **必须写成 async def + await，不能用 asyncio.run()。**
    `asyncio.run` 会把协程包进 Task，而 `Task.__init__` 做 `contextvars.copy_context()`，
    run_chapter 内部对 contextvar 的写入（含「不还原」）**永远不会**传播回调用方上下文。
    那样 `assert get_current_trace() is stale` 恒成立——把 `finally` 整段删掉，1863 条测试
    依然全绿，这个任务最核心的不变量就没人守了。
    直接 `await pipeline.run_chapter(ctx)` 让测试体与管线共享同一个 Task 上下文，
    断言才真正生效。
    """
    from engine.pipeline.context import PipelineContext

    observed = {}

    class _P(BaseStoryPipeline):
        pass

    pipeline = _P()

    async def _find(ctx):
        observed["inside"] = get_current_trace()
        # 让管线在第 1 步就短路，不必走完 12 步
        return StepResult(passed=False, message="到此为止")

    pipeline._step_find_next_chapter = _find

    stale = TraceContext.create(novel_id="stale-novel", stage="old", stage_label="旧")
    outer = set_current_trace(stale)
    try:
        ctx = PipelineContext(novel_id="n1", chapter_number=1)
        result = await pipeline.run_chapter(ctx)

        assert result.success is False
        assert observed["inside"] is not stale
        assert observed["inside"].novel_id == "n1"
        assert get_current_trace() is stale
    finally:
        reset_current_trace(outer)
