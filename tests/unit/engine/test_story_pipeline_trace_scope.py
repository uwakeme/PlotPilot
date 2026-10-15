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
    set_trace_stage,
)
from engine.pipeline.base import (
    _CHAPTER_STAGE,
    _CHAPTER_STAGE_LABEL,
    _PIPELINE_STAGE_BY_STEP,
    BaseStoryPipeline,
    _enter_chapter_trace,
    _exit_chapter_trace,
)
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


# ── 步骤级 stage 标记 ────────────────────────────────────────────────
# 不做这一步的话，一章 12 步里所有 LLM 调用在 ai_trace_spans 和 AI 调用流里
# 都挂同一个「章节生成」标签，用户看不出某次调用发生在哪一步。


def _bare_ctx():
    return SimpleNamespace(novel_id="n1", get_dep=lambda name: None)


def test_mapped_pipeline_step_advances_the_trace_stage():
    """进入某一步时，trace 的 stage 必须跟着走，AI 调用流才认得出是哪一步。"""
    pipeline = BaseStoryPipeline()
    token = _enter_chapter_trace(_bare_ctx())
    try:
        assert get_current_trace().stage == _CHAPTER_STAGE

        pipeline._mark_pipeline_step(_bare_ctx(), "generate")

        trace = get_current_trace()
        assert trace.stage == "pipeline.chapter.prose"
        assert trace.stage_label == "正文撰写"
    finally:
        _exit_chapter_trace(token)


def test_completed_notification_does_not_move_the_stage():
    """completed=True 是收尾通知，不是进入新步骤，不能改 stage。"""
    pipeline = BaseStoryPipeline()
    token = _enter_chapter_trace(_bare_ctx())
    try:
        pipeline._mark_pipeline_step(_bare_ctx(), "generate")
        pipeline._mark_pipeline_step(_bare_ctx(), "save_chapter", completed=True)
        assert get_current_trace().stage == "pipeline.chapter.prose"
    finally:
        _exit_chapter_trace(token)


def test_unmapped_step_falls_back_to_chapter_label():
    """未映射的步骤必须回落，不能沿用上一步的标签。"""
    pipeline = BaseStoryPipeline()
    token = _enter_chapter_trace(_bare_ctx())
    try:
        pipeline._mark_pipeline_step(_bare_ctx(), "generate")
        pipeline._mark_pipeline_step(_bare_ctx(), "find_next_chapter")

        trace = get_current_trace()
        assert trace.stage == _CHAPTER_STAGE
        assert trace.stage_label == _CHAPTER_STAGE_LABEL
    finally:
        _exit_chapter_trace(token)


def test_stage_rescope_preserves_novel_id():
    """重标记绝不能碰 novel_id——那是跨书隔离的唯一依据。"""
    pipeline = BaseStoryPipeline()
    token = _enter_chapter_trace(_bare_ctx())
    try:
        before = get_current_trace()
        pipeline._mark_pipeline_step(_bare_ctx(), "score_tension")
        after = get_current_trace()

        assert after is before
        assert after.novel_id == "n1"
        assert after.stage == "pipeline.chapter.tension"
    finally:
        _exit_chapter_trace(token)


def test_every_mapped_stage_key_exists_in_the_taxonomy():
    """映射表的 key 必须落在 AI_CALL_STAGES 里。

    前端靠 stage key 去 aiCallStages.ts 查 label 和 semantic 配色；key 一旦
    对不上，UI 会退化成直接显示裸 key，标签和配色一起失效。这条钉住它。
    """
    from application.ai.ai_call_stage import get_stage_label

    for step_name, key in _PIPELINE_STAGE_BY_STEP.items():
        label = get_stage_label(key)
        assert label != key, f"{step_name} 的 stage key {key!r} 不在 AI_CALL_STAGES 中"
        assert label.strip(), f"{step_name} 的 stage {key!r} 没有中文标签"


def test_set_trace_stage_is_a_noop_without_trace():
    """没有 ambient trace 时不能凭空造一个。"""
    outer = set_current_trace(None)
    try:
        set_trace_stage("pipeline.chapter.prose", "正文撰写")
        assert get_current_trace() is None
    finally:
        reset_current_trace(outer)
