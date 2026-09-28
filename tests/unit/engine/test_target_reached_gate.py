"""目标章数软目标闸门（target_reached_gate）与两处判定点、生命周期 COMPLETED 分支测试。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from domain.novel.entities.novel import AutopilotStatus, NovelStage
from domain.novel.value_objects.generation_preferences import GenerationPreferences
from engine.runtime.novel_lifecycle import process_novel
from engine.runtime.target_reached_gate import (
    TARGET_REACHED_PAUSE_REASON,
    resolve_target_reached,
)


def _make_novel(**prefs_kwargs):
    """构造 gate 所需的最小 novel 形状（真实 GenerationPreferences，其余为命名空间）。"""
    return SimpleNamespace(
        novel_id=SimpleNamespace(value="n-1"),
        target_chapters=300,
        generation_prefs=GenerationPreferences(**prefs_kwargs),
    )


def _make_host(completed=300):
    host = MagicMock()
    host._count_completed_chapters.return_value = completed
    return host


# ── resolve_target_reached：策略分流 ──────────────────────────────


def test_policy_ask_returns_pause_without_mutation():
    novel = _make_novel()
    host = _make_host(completed=300)

    assert resolve_target_reached(host, novel, 300) == "pause"
    assert novel.target_chapters == 300
    assert novel.generation_prefs.finale_mode is False
    host._save_novel_state.assert_not_called()


def test_policy_complete_returns_complete_without_mutation():
    novel = _make_novel(target_reached_policy="complete")
    host = _make_host(completed=300)

    assert resolve_target_reached(host, novel, 300) == "complete"
    assert novel.target_chapters == 300
    host._save_novel_state.assert_not_called()


def test_policy_continue_extends_target_by_block():
    novel = _make_novel(target_reached_policy="continue")
    host = _make_host(completed=300)

    assert resolve_target_reached(host, novel, 300) == "extended"
    assert novel.target_chapters == 330  # 300 + max(10, 300*0.1)
    host._save_novel_state.assert_called_once()


def test_policy_finale_first_time_extends_and_sets_flag():
    novel = _make_novel(target_reached_policy="finale", finale_chapter_budget=25)
    host = _make_host(completed=303)

    assert resolve_target_reached(host, novel, 303) == "extended"
    assert novel.target_chapters == 328  # max(300, 303) + 25
    assert novel.generation_prefs.finale_mode is True
    host._save_novel_state.assert_called_once()


def test_policy_finale_budget_done_completes_and_clears_flag():
    novel = _make_novel(target_reached_policy="finale", finale_mode=True)
    host = _make_host(completed=328)

    assert resolve_target_reached(host, novel, 328) == "complete"
    assert novel.generation_prefs.finale_mode is False
    host._save_novel_state.assert_called_once()


def test_completed_count_defaults_to_host_count_for_extending_policies():
    novel = _make_novel(target_reached_policy="continue")
    host = _make_host(completed=290)  # 未显式传完成数时由 host 统计

    assert resolve_target_reached(host, novel) == "extended"
    host._count_completed_chapters.assert_called_once_with(novel.novel_id)
    assert novel.target_chapters == 330  # max(300, 290) + max(10, 300*0.1)
    host._save_novel_state.assert_called_once()


# ── audit_delegate._apply_book_done_outcome：达标分流 ─────────────


def test_audit_book_done_ask_pauses_with_reason():
    from engine.runtime.audit_delegate import _apply_book_done_outcome

    host = MagicMock()
    novel = _make_novel()
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.current_stage = NovelStage.WRITING

    target_reached, is_completed = _apply_book_done_outcome(
        host, novel, completed_count=300, pause_gate=False
    )
    assert (target_reached, is_completed) == (True, False)
    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    assert novel.autopilot_status == AutopilotStatus.STOPPED
    host._update_shared_state.assert_called_once_with(
        "n-1", autopilot_pause_reason=TARGET_REACHED_PAUSE_REASON
    )


def test_audit_book_done_complete_policy_completes():
    from engine.runtime.audit_delegate import _apply_book_done_outcome

    host = MagicMock()
    novel = _make_novel(target_reached_policy="complete")
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.current_stage = NovelStage.WRITING

    target_reached, is_completed = _apply_book_done_outcome(
        host, novel, completed_count=300, pause_gate=False
    )
    assert (target_reached, is_completed) == (False, True)
    assert novel.current_stage == NovelStage.COMPLETED
    assert novel.autopilot_status == AutopilotStatus.STOPPED
    host._update_shared_state.assert_not_called()


def test_audit_book_done_extended_keeps_writing_not_completed():
    from engine.runtime.audit_delegate import _apply_book_done_outcome

    host = MagicMock()
    novel = _make_novel(target_reached_policy="continue")
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.current_stage = NovelStage.WRITING

    target_reached, is_completed = _apply_book_done_outcome(
        host, novel, completed_count=300, pause_gate=False
    )
    assert (target_reached, is_completed) == (False, False)
    assert novel.current_stage == NovelStage.WRITING
    assert novel.target_chapters == 330


def test_audit_book_done_with_pause_gate_defers():
    from engine.runtime.audit_delegate import _apply_book_done_outcome

    host = MagicMock()
    novel = _make_novel()
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.current_stage = NovelStage.PAUSED_FOR_REVIEW

    target_reached, is_completed = _apply_book_done_outcome(
        host, novel, completed_count=300, pause_gate=True
    )
    assert (target_reached, is_completed) == (False, False)
    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    host._update_shared_state.assert_not_called()


def test_audit_below_target_is_noop():
    from engine.runtime.audit_delegate import _apply_book_done_outcome

    host = MagicMock()
    novel = _make_novel()
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.current_stage = NovelStage.WRITING

    target_reached, is_completed = _apply_book_done_outcome(
        host, novel, completed_count=299, pause_gate=False
    )
    assert (target_reached, is_completed) == (False, False)
    assert novel.current_stage == NovelStage.WRITING


# ── legacy_writing_delegate：写作前门槛 ───────────────────────────


@pytest.mark.asyncio
async def test_legacy_gate_ask_pauses_instead_of_completing():
    from engine.runtime.legacy_writing_delegate import run_legacy_writing

    host = MagicMock()
    host._is_still_running.return_value = True
    host.story_node_repo.get_by_novel = AsyncMock(return_value=[])  # 无卷 → 提前分支？
    novel = _make_novel()
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.current_stage = NovelStage.WRITING
    novel.current_auto_chapters = 300
    novel.max_auto_chapters = 9999
    # 注意：无卷会先回宏观规划；给一个卷节点避开
    volume = SimpleNamespace(node_type=SimpleNamespace(value="volume"))
    host.story_node_repo.get_by_novel = AsyncMock(return_value=[volume])

    await run_legacy_writing(host, novel)

    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    assert novel.autopilot_status == AutopilotStatus.STOPPED
    host._update_shared_state.assert_called_with(
        "n-1", autopilot_pause_reason=TARGET_REACHED_PAUSE_REASON
    )


@pytest.mark.asyncio
async def test_legacy_gate_complete_policy_completes():
    from engine.runtime.legacy_writing_delegate import run_legacy_writing

    host = MagicMock()
    host._is_still_running.return_value = True
    volume = SimpleNamespace(node_type=SimpleNamespace(value="volume"))
    host.story_node_repo.get_by_novel = AsyncMock(return_value=[volume])
    novel = _make_novel(target_reached_policy="complete")
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.current_stage = NovelStage.WRITING
    novel.current_auto_chapters = 300
    novel.max_auto_chapters = 9999

    await run_legacy_writing(host, novel)

    assert novel.current_stage == NovelStage.COMPLETED
    assert novel.autopilot_status == AutopilotStatus.STOPPED


@pytest.mark.asyncio
async def test_legacy_gate_continue_extends_then_hits_max_guard():
    from engine.runtime.legacy_writing_delegate import run_legacy_writing

    host = MagicMock()
    host._is_still_running.return_value = True
    host._count_completed_chapters.return_value = 300
    volume = SimpleNamespace(node_type=SimpleNamespace(value="volume"))
    host.story_node_repo.get_by_novel = AsyncMock(return_value=[volume])
    novel = _make_novel(target_reached_policy="continue")
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.current_stage = NovelStage.WRITING
    novel.current_auto_chapters = 300
    novel.max_auto_chapters = 299  # extended 后落进保护上限，早退便于断言

    await run_legacy_writing(host, novel)

    assert novel.target_chapters == 330  # 门槛建续写：目标已上调
    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW  # 保护上限分支
    assert novel.autopilot_status == AutopilotStatus.STOPPED


# ── novel_lifecycle：COMPLETED 分支 ───────────────────────────────


@pytest.mark.asyncio
async def test_process_novel_completed_with_raised_target_resumes_writing():
    host = MagicMock()
    host._is_still_running.return_value = True
    host.circuit_breaker = None
    host._count_completed_chapters.return_value = 303
    novel = MagicMock()
    novel.novel_id.value = "n-1"
    novel.current_stage = NovelStage.COMPLETED
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.target_chapters = 350

    await process_novel(host, novel)

    assert novel.current_stage == NovelStage.WRITING
    host._save_novel_state.assert_called()


@pytest.mark.asyncio
async def test_process_novel_completed_stays_when_target_met():
    host = MagicMock()
    host._is_still_running.return_value = True
    host.circuit_breaker = None
    host._count_completed_chapters.return_value = 303
    novel = MagicMock()
    novel.novel_id.value = "n-1"
    novel.current_stage = NovelStage.COMPLETED
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.target_chapters = 303

    await process_novel(host, novel)

    assert novel.current_stage == NovelStage.COMPLETED
    host._save_novel_state.assert_not_called()