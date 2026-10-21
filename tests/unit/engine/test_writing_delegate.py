"""StoryPipeline 写作委托测试"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from domain.novel.entities.novel import NovelStage
from engine.runtime.writing_delegate import (
    get_story_pipeline_mode,
    is_story_pipeline_writing_enabled,
    run_writing,
    run_story_pipeline_writing,
    story_pipeline_mode_was_unset,
)
from engine.pipeline.context import PipelineResult


def test_is_story_pipeline_writing_enabled_default_writing(monkeypatch):
    monkeypatch.delenv("PLOTPILOT_USE_STORY_PIPELINE", raising=False)
    assert is_story_pipeline_writing_enabled() is True


def test_is_story_pipeline_writing_enabled_explicit_off(monkeypatch):
    monkeypatch.setenv("PLOTPILOT_USE_STORY_PIPELINE", "off")
    assert is_story_pipeline_writing_enabled() is False


def test_get_story_pipeline_mode_default_writing(monkeypatch):
    monkeypatch.delenv("PLOTPILOT_USE_STORY_PIPELINE", raising=False)
    assert get_story_pipeline_mode() == "writing"
    assert story_pipeline_mode_was_unset() is True


def test_is_story_pipeline_writing_enabled_on(monkeypatch):
    monkeypatch.setenv("PLOTPILOT_USE_STORY_PIPELINE", "1")
    assert is_story_pipeline_writing_enabled() is True


def test_get_story_pipeline_mode_full(monkeypatch):
    monkeypatch.setenv("PLOTPILOT_USE_STORY_PIPELINE", "full")
    assert get_story_pipeline_mode() == "full"
    assert story_pipeline_mode_was_unset() is False
    assert is_story_pipeline_writing_enabled() is True


def test_get_story_pipeline_mode_unknown_warns_and_defaults(monkeypatch, caplog):
    monkeypatch.setenv("PLOTPILOT_USE_STORY_PIPELINE", "surprise")

    assert get_story_pipeline_mode() == "writing"
    assert "未知 PLOTPILOT_USE_STORY_PIPELINE" in caplog.text


@pytest.mark.asyncio
async def test_run_story_pipeline_writing_success_updates_novel():
    novel = MagicMock()
    novel.novel_id.value = "novel-1"
    novel.genre = "wuxia"
    novel.target_words_per_chapter = 3000
    novel.auto_approve_mode = True
    novel.era = "ancient"
    novel.current_auto_chapters = 2
    novel.current_chapter_in_act = 1
    novel.current_beat_index = 3
    novel.beats_completed = True

    daemon = MagicMock()
    daemon._update_shared_state = MagicMock()
    daemon._flush_novel = MagicMock()

    mock_ctx = MagicMock()
    mock_ctx.chapter_number = 5
    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = mock_ctx
    mock_runner._get_novel_phase.return_value = "development"

    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(
            success=True,
            chapter_number=5,
            word_count=2800,
            tension=72,
        )
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    assert novel.current_auto_chapters == 3
    assert novel.current_chapter_in_act == 2
    assert novel.current_beat_index == 0
    assert novel.beats_completed is False
    assert novel.last_chapter_tension == 72
    assert novel.current_stage == NovelStage.AUDITING
    daemon._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
async def test_run_story_pipeline_writing_all_chapters_done_transitions_act():
    novel = MagicMock()
    novel.novel_id.value = "novel-1"
    novel.genre = ""
    novel.current_act = 1
    novel.current_chapter_in_act = 5

    daemon = MagicMock()
    daemon._update_shared_state = MagicMock()
    daemon._flush_novel = MagicMock()
    daemon._current_act_fully_written = AsyncMock(return_value=True)

    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = MagicMock(chapter_number=0)
    mock_runner._get_novel_phase.return_value = "development"

    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(success=False, error="所有章节已写完，无需继续")
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    assert novel.current_act == 2
    assert novel.current_chapter_in_act == 0
    assert novel.current_stage == NovelStage.ACT_PLANNING
    daemon._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
async def test_run_story_pipeline_writing_all_chapters_done_without_full_act_replans_same_act():
    novel = MagicMock()
    novel.novel_id.value = "novel-1"
    novel.genre = ""
    novel.current_act = 0
    novel.current_chapter_in_act = 3

    daemon = MagicMock()
    daemon._update_shared_state = MagicMock()
    daemon._flush_novel = MagicMock()
    daemon._current_act_fully_written = AsyncMock(return_value=False)

    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = MagicMock(chapter_number=0)
    mock_runner._get_novel_phase.return_value = "opening"

    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(success=False, error="所有章节已写完，无需继续")
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    assert novel.current_act == 0
    assert novel.current_chapter_in_act == 0
    assert novel.current_stage == NovelStage.ACT_PLANNING
    daemon._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
async def test_run_writing_dispatches_legacy_when_pipeline_disabled():
    host = MagicMock()
    host.use_story_pipeline_for_writing = False
    novel = MagicMock()

    with patch("engine.runtime.legacy_writing_delegate.run_legacy_writing", new=AsyncMock()) as legacy:
        await run_writing(host, novel)

    legacy.assert_awaited_once_with(host, novel)


def _make_failure_env(monkeypatch, prior_failures: int = 0):
    """构造写作失败路径的最小 daemon/novel 与共享内存计数 mock。"""
    novel = MagicMock()
    novel.novel_id.value = "novel-1"
    novel.genre = "wuxia"
    novel.target_words_per_chapter = 3000
    novel.auto_approve_mode = True
    novel.era = "ancient"
    novel.consecutive_error_count = 0
    novel.autopilot_status = MagicMock(name="status-unchanged-marker")

    daemon = MagicMock()
    daemon._update_shared_state = MagicMock()
    daemon._flush_novel = MagicMock()
    daemon._report_llm_failure = MagicMock()
    daemon._clear_llm_error_summary = MagicMock()

    shared = {"autopilot_llm_failure_count": prior_failures}
    monkeypatch.setattr(
        "application.ai_invocation.autopilot.shared_state.read_autopilot_shared_state",
        lambda nid: dict(shared),
    )
    return daemon, novel, shared


@pytest.mark.asyncio
async def test_writing_failure_escalates_via_shared_counter(monkeypatch):
    """写作失败用共享内存计数上报：不依赖会被 tick 重置的 novel.consecutive_error_count。"""
    daemon, novel, _ = _make_failure_env(monkeypatch, prior_failures=0)

    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = MagicMock(chapter_number=7)
    mock_runner._get_novel_phase.return_value = "development"
    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(success=False, chapter_number=7, error="LLM 端点 502")
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    daemon._report_llm_failure.assert_called_once()
    args, kwargs = daemon._report_llm_failure.call_args
    assert args[0] == "novel-1"
    assert "连续第 1/3 次" in args[1]
    assert kwargs.get("failure_count") == 1
    assert novel.consecutive_error_count == 1
    assert novel.autopilot_status != "ERROR" or novel.autopilot_status == "status-unchanged-marker"


@pytest.mark.asyncio
async def test_writing_failure_third_time_suspends_novel(monkeypatch):
    """连续第 3 次写作失败：小说挂起为 ERROR，不再无限重试。"""
    from domain.novel.entities.novel import AutopilotStatus

    daemon, novel, _ = _make_failure_env(monkeypatch, prior_failures=2)

    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = MagicMock(chapter_number=8)
    mock_runner._get_novel_phase.return_value = "development"
    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(success=False, chapter_number=8, error="empty content")
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    assert novel.autopilot_status == AutopilotStatus.ERROR
    daemon._report_llm_failure.assert_called_once()
    assert daemon._report_llm_failure.call_args.kwargs.get("failure_count") == 3


@pytest.mark.asyncio
async def test_writing_success_clears_failure_summary(monkeypatch):
    """写作成功后清除模型失败摘要，前端错误提示随之消失。"""
    daemon, novel, _ = _make_failure_env(monkeypatch, prior_failures=1)

    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = MagicMock(chapter_number=5)
    mock_runner._get_novel_phase.return_value = "development"
    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(success=True, chapter_number=5, word_count=2600, tension=70)
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    daemon._clear_llm_error_summary.assert_called_once_with("novel-1")
    daemon._report_llm_failure.assert_not_called()


@pytest.mark.asyncio
async def test_interrupted_writing_uses_interrupted_recovery_decision(monkeypatch):
    """D-07：写作中断后必须走「中断」判定（**要**丢弃临时产物），不是常规 tick 判定。

    常规 tick 判定已经不再丢弃生成临时产物（否则每 tick 都清大纲），
    中断路径若还在用 tick 判定 ⇒ 中断留下的半截七段规划会被当稳定输入复用。
    """
    from types import SimpleNamespace

    from application.engine.services.autopilot_recovery_policy import AutopilotRecoveryPolicy

    daemon, novel, _ = _make_failure_env(monkeypatch, prior_failures=0)

    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = MagicMock(chapter_number=9)
    mock_runner._get_novel_phase.return_value = "development"
    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(success=False, chapter_number=9, error="interrupted")
    )

    calls: list[str] = []
    dummy = SimpleNamespace(chapter_number=9, discard_transient_generation=True)

    monkeypatch.setattr(
        AutopilotRecoveryPolicy,
        "decide_after_interrupted_writing",
        lambda self, novel: calls.append("interrupted") or dummy,
    )
    monkeypatch.setattr(
        AutopilotRecoveryPolicy,
        "decide_on_daemon_tick",
        lambda self, novel: calls.append("tick") or dummy,
    )
    monkeypatch.setattr(
        AutopilotRecoveryPolicy, "apply_transient_cleanup", lambda self, decision: None
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    assert calls == ["interrupted"]


@pytest.mark.asyncio
async def test_process_novel_does_not_record_success_when_llm_failed(monkeypatch):
    """本轮已有模型失败（共享内存计数>0）时：熔断器不记成功、失败计数不清零。"""
    from domain.novel.entities.novel import AutopilotStatus, NovelStage

    from engine.runtime.novel_lifecycle import process_novel

    novel = MagicMock()
    novel.novel_id.value = "novel-1"
    novel.current_stage = NovelStage.AUDITING
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.consecutive_error_count = 1

    host = MagicMock()
    host._read_autopilot_status_ephemeral.return_value = AutopilotStatus.RUNNING
    host._is_still_running.return_value = True
    host._merge_autopilot_status_from_db = MagicMock()
    host._save_novel_state = MagicMock()
    host._update_shared_state = MagicMock()

    monkeypatch.setattr(
        "application.ai_invocation.autopilot.shared_state.read_autopilot_shared_state",
        lambda nid: {"autopilot_llm_failure_count": 2},
    )
    monkeypatch.setattr(
        "engine.runtime.novel_lifecycle.run_chapter_audit", AsyncMock()
    )

    await process_novel(host, novel)

    host.circuit_breaker.record_success.assert_not_called()
    assert novel.consecutive_error_count == 1  # 未被清零


@pytest.mark.asyncio
async def test_process_novel_records_success_when_no_failures(monkeypatch):
    """无失败时维持原行为：熔断器记成功、失败计数清零。"""
    from domain.novel.entities.novel import AutopilotStatus, NovelStage

    from engine.runtime.novel_lifecycle import process_novel

    novel = MagicMock()
    novel.novel_id.value = "novel-2"
    novel.current_stage = NovelStage.AUDITING
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.consecutive_error_count = 2

    host = MagicMock()
    host._read_autopilot_status_ephemeral.return_value = AutopilotStatus.RUNNING
    host._is_still_running.return_value = True
    host._merge_autopilot_status_from_db = MagicMock()
    host._save_novel_state = MagicMock()
    host._update_shared_state = MagicMock()

    monkeypatch.setattr(
        "application.ai_invocation.autopilot.shared_state.read_autopilot_shared_state",
        lambda nid: {"autopilot_llm_failure_count": 0},
    )
    monkeypatch.setattr(
        "engine.runtime.novel_lifecycle.run_chapter_audit", AsyncMock()
    )

    await process_novel(host, novel)

    host.circuit_breaker.record_success.assert_called_once()
    assert novel.consecutive_error_count == 0
