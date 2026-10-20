"""A4 崩溃恢复：半途续写（workspace 底稿注入）+ finalize 接 CheckpointManager。

D-08 —— 续不续、从哪续、怎么续由 **AI** 判断（登记表 D-08 的判定是 C）。
旧实现是「preview ≥500 字自动切续写 + 注入一句代码写死的话术」；现在
底稿还在，但**要不要接、怎么接**由 ``planning-continuation-strategy`` 决定；
判不出来就不注入底稿，代码不再代写续写指令。
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.token_usage import TokenUsage
from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext


def _pipeline() -> BaseStoryPipeline:
    return BaseStoryPipeline.__new__(BaseStoryPipeline)


def _ctx(workspace=None) -> PipelineContext:
    ctx = PipelineContext(novel_id="n-1", chapter_number=7)
    ctx.context_text = "原始上下文"
    ctx.chapter_generation_workspace = workspace
    ctx.chapter_saved = True
    ctx.word_count = 2600
    ctx.llm_service = MagicMock()
    return ctx


class _StubWorkspace:
    def __init__(self, preview: str | None):
        self._preview = preview
        self.discarded = []

    def read_latest_preview(self, novel_id, chapter_number, run_id=None):
        return self._preview

    def discard(self, novel_id, chapter_number, run_id=None):
        self.discarded.append((novel_id, chapter_number, run_id))

    def begin(self, novel_id, chapter_number, run_id=None):
        return MagicMock()

    def append_preview(self, novel_id, chapter_number, run_id, content):
        pass


@pytest.mark.asyncio
async def test_interrupted_preview_becomes_resume_draft(monkeypatch):
    """崩溃残留的半章 preview 注入 context_text 作为续写底稿，不再静默丢弃。"""
    from engine.pipeline.base import BaseStoryPipeline as BSP

    half_chapter = "上回生成的半章正文。" * 120  # > 500 字
    workspace = _StubWorkspace(preview=half_chapter)
    ctx = _ctx(workspace)
    # D-08：续不续由 AI 判断 —— 这里让 AI 判定「接着写」。
    ctx.llm_service.generate = AsyncMock(
        return_value=GenerationResult(
            json.dumps(
                {
                    "strategy": "continue",
                    "directive": "从最后一句接上，继续推进第二拍。",
                    "reason": "断点落在两拍之间",
                }
            ),
            TokenUsage(input_tokens=1, output_tokens=1),
        )
    )

    composed_request = {}

    class _Composer:
        async def compose(self, request):
            composed_request["context_text"] = request.context_text
            result = MagicMock()
            result.awaiting_review = False
            result.interrupted = False
            result.content = "生成的正文内容。"
            result.word_count = 8
            return result

    async def _noop(*a, **k):
        pass

    monkeypatch.setattr(BSP, "_novel_stream_should_stop", lambda self, nid: False)
    monkeypatch.setattr(BSP, "_push_streaming_snapshot", _noop)
    pipeline = _pipeline()
    pipeline._log_step = lambda *a, **k: None
    pipeline._mark_pipeline_step = lambda *a, **k: None

    result = await pipeline._step_generate_with_composer(ctx, _Composer())

    assert "续写底稿" in composed_request["context_text"]
    assert half_chapter[:50] in composed_request["context_text"]
    assert "原始上下文" in composed_request["context_text"]
    # D-08：续写指令由 AI 给 —— 代码代写的那句固定话术不许再出现
    assert "从最后一句接上" in composed_request["context_text"]
    assert "请从其结尾处自然续写至全章完成" not in composed_request["context_text"]


@pytest.mark.asyncio
async def test_short_or_absent_preview_starts_fresh(monkeypatch):
    """无残留/过短残留（<500 字）按全新生成，context_text 不注入底稿。"""
    from engine.pipeline.base import BaseStoryPipeline as BSP

    workspace = _StubWorkspace(preview="太短的残留")  # < 500
    ctx = _ctx(workspace)
    composed_request = {}

    class _Composer:
        async def compose(self, request):
            composed_request["context_text"] = request.context_text
            result = MagicMock()
            result.awaiting_review = False
            result.interrupted = False
            result.content = "生成的正文内容。"
            result.word_count = 8
            return result

    async def _noop(*a, **k):
        pass

    monkeypatch.setattr(BSP, "_novel_stream_should_stop", lambda self, nid: False)
    monkeypatch.setattr(BSP, "_push_streaming_snapshot", _noop)
    pipeline = _pipeline()
    pipeline._log_step = lambda *a, **k: None
    pipeline._mark_pipeline_step = lambda *a, **k: None

    await pipeline._step_generate_with_composer(ctx, _Composer())

    assert "续写底稿" not in composed_request["context_text"]


@pytest.mark.asyncio
async def test_finalize_creates_chapter_checkpoint(monkeypatch):
    """章保存成功后 finalize 自动触发 CheckpointManager.on_chapter_completed。"""
    manager = MagicMock()
    manager.on_chapter_completed = AsyncMock(return_value=MagicMock(value="ckpt-1"))
    captured = {}

    def _factory(store):
        captured["store"] = store
        return manager

    monkeypatch.setattr(
        "engine.runtime.checkpoint_manager.manager.CheckpointManager", _factory
    )
    ctx = _ctx()
    pipeline = _pipeline()
    pipeline._log_step = lambda *a, **k: None

    await pipeline._step_finalize(ctx)

    manager.on_chapter_completed.assert_awaited_once()
    kwargs = manager.on_chapter_completed.call_args.kwargs
    assert kwargs["story_id"] == "n-1"
    assert kwargs["chapter_number"] == 7
    assert ctx.audit_snapshot is not None


@pytest.mark.asyncio
async def test_finalize_survives_checkpoint_failure(monkeypatch):
    """checkpoint 抛异常不阻断 finalize（恢复增强不是主链路）。"""

    def _factory(store):
        manager = MagicMock()
        manager.on_chapter_completed = AsyncMock(side_effect=RuntimeError("db locked"))
        return manager

    monkeypatch.setattr(
        "engine.runtime.checkpoint_manager.manager.CheckpointManager", _factory
    )
    ctx = _ctx()
    pipeline = _pipeline()
    pipeline._log_step = lambda *a, **k: None

    result = await pipeline._step_finalize(ctx)
    assert result.passed is True


def test_discard_generation_workspace_clears_all_runs():
    workspace = _StubWorkspace(preview=None)
    ctx = _ctx(workspace)
    pipeline = _pipeline()

    pipeline._discard_generation_workspace(ctx)

    assert workspace.discarded == [("n-1", 7, None)]  # 全量清理（不限定 run_id）


# ======================================================================
#  D-08：续写策略由 AI 判断，判不出来就不注入底稿
# ======================================================================


def _decision_llm(payload):
    llm = MagicMock()
    llm.generate = AsyncMock(
        return_value=GenerationResult(
            json.dumps(payload, ensure_ascii=False),
            TokenUsage(input_tokens=1, output_tokens=1),
        )
    )
    return llm


async def _run_generate(monkeypatch, *, preview, decision_payload=None, decision_error=None):
    """跑一遍生成步骤，返回最终进模型的 context_text。"""
    from engine.pipeline.base import BaseStoryPipeline as BSP

    half_chapter = "上回生成的半章正文。" * 120  # > 500 字
    workspace = _StubWorkspace(preview=preview if preview is not None else half_chapter)
    ctx = _ctx(workspace)
    if decision_error is not None:
        ctx.llm_service.generate = AsyncMock(side_effect=decision_error)
    elif decision_payload is None:
        ctx.llm_service.generate = AsyncMock(
            return_value=GenerationResult(
                json.dumps({"strategy": None, "directive": "", "reason": "信息不足"}),
                TokenUsage(input_tokens=1, output_tokens=1),
            )
        )
    else:
        ctx.llm_service.generate = AsyncMock(
            return_value=GenerationResult(
                json.dumps(decision_payload, ensure_ascii=False),
                TokenUsage(input_tokens=1, output_tokens=1),
            )
        )
    composed_request = {}

    class _Composer:
        async def compose(self, request):
            composed_request["context_text"] = request.context_text
            result = MagicMock()
            result.awaiting_review = False
            result.interrupted = False
            result.content = "生成的正文内容。"
            result.word_count = 8
            return result

    async def _noop(*a, **k):
        pass

    monkeypatch.setattr(BSP, "_novel_stream_should_stop", lambda self, nid: False)
    monkeypatch.setattr(BSP, "_push_streaming_snapshot", _noop)
    pipeline = _pipeline()
    pipeline._log_step = lambda *a, **k: None
    pipeline._mark_pipeline_step = lambda *a, **k: None

    await pipeline._step_generate_with_composer(ctx, _Composer())
    return composed_request["context_text"]


def test_normalize_keeps_undecided_as_none():
    """判不出来必须留 None —— 不得回填成 continue/rewrite。"""
    from application.engine.services.continuation_strategy_schema import (
        SOURCE_NONE,
        normalize_continuation_strategy,
    )

    intent = normalize_continuation_strategy({"strategy": None, "directive": "接着写"})

    assert intent.strategy is None
    assert intent.source == SOURCE_NONE
    assert intent.is_usable is False


@pytest.mark.asyncio
async def test_ai_decides_rewrite_and_the_draft_is_not_injected(monkeypatch):
    """AI 判定整章重写 ⇒ 底稿不进上下文（不接它）。"""
    text = await _run_generate(
        monkeypatch,
        preview=None,
        decision_payload={
            "strategy": "rewrite",
            "directive": "",
            "reason": "草稿与本章大纲对不上",
        },
    )

    assert "续写底稿" not in text
    assert "上回生成的半章正文" not in text


@pytest.mark.asyncio
async def test_undecided_ai_means_no_draft_and_no_fabricated_directive(monkeypatch):
    """判不出来 ⇒ 不注入底稿，**也不许有代码代写的续写指令**。"""
    text = await _run_generate(
        monkeypatch,
        preview=None,
        decision_payload={"strategy": None, "directive": "", "reason": "信息不足"},
    )

    assert "续写底稿" not in text
    assert "请从其结尾处自然续写至全章完成" not in text
    assert "续写" not in text


@pytest.mark.asyncio
async def test_llm_failure_means_no_draft(monkeypatch):
    """判定调用失败 ⇒ 按未判定处理（不注入底稿），不让整章生成崩掉。"""
    text = await _run_generate(
        monkeypatch,
        preview=None,
        decision_error=RuntimeError("upstream 502"),
    )

    assert "续写底稿" not in text
    assert "请从其结尾处自然续写至全章完成" not in text


@pytest.mark.asyncio
async def test_unknown_strategy_value_is_treated_as_undecided(monkeypatch):
    """含糊的 strategy（不是 continue/rewrite）⇒ 判不出来。"""
    text = await _run_generate(
        monkeypatch,
        preview=None,
        decision_payload={"strategy": "resume", "directive": "接着写", "reason": ""},
    )

    assert "续写底稿" not in text


@pytest.mark.asyncio
async def test_continue_without_directive_does_not_inject_the_draft(monkeypatch):
    """AI 说继续、但没给续写指令 ⇒ 不注入底稿。

    没有指令就注入底稿 = 模型只看到一段旧正文却没人告诉它该干什么 ——
    那正是「代写话术」在替 AI 做决定。
    """
    text = await _run_generate(
        monkeypatch,
        preview=None,
        decision_payload={"strategy": "continue", "directive": "", "reason": "能接"},
    )

    assert "续写底稿" not in text
