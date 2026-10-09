"""A4 崩溃恢复：半途续写（workspace 底稿注入）+ finalize 接 CheckpointManager。"""
from unittest.mock import AsyncMock, MagicMock

import pytest

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
