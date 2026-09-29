"""幕级规划僵死 invocation 清理（_cancel_stalled_invocation）与重新规划恢复路径测试。"""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from domain.novel.entities.novel import AutopilotStatus, NovelStage
from engine.runtime import act_planning_delegate
from engine.runtime.act_planning_delegate import _cancel_stalled_invocation

MODULE_DB = "infrastructure.persistence.database.connection.get_database"


def _utc_stamped_row(status: str, age_seconds: float) -> dict:
    from datetime import datetime, timedelta, timezone

    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return {"id": "sess-1", "status": status, "policy": "DIRECT", "updated_at": ts}


def _mock_db(row):
    db = MagicMock()
    db.fetch_one.return_value = row
    return db


@pytest.fixture()
def fake_db(monkeypatch):
    """接管 delegate 内部的 get_database() 读写，避免测试触真实库。"""
    holder = {}

    @contextmanager
    def _null_ctx():
        yield None

    db = MagicMock()
    holder["db"] = db
    monkeypatch.setattr(MODULE_DB, lambda: db)
    return holder


def test_stale_prompt_compiled_session_is_cancelled(fake_db):
    fake_db["db"].fetch_one.return_value = _utc_stamped_row("prompt_compiled", 3600)
    host = MagicMock()
    novel_id = "n-1"

    assert _cancel_stalled_invocation(host, novel_id, "sess-1") is True
    fake_db["db"].execute.assert_called_once()
    assert "cancelled" in fake_db["db"].execute.call_args.args[0]
    # 必须 commit：否则管线线程的线程本地连接会残留隐式事务，钉死后续读快照
    fake_db["db"].commit.assert_called_once()
    host._update_shared_state.assert_called_once()
    clear_kwargs = host._update_shared_state.call_args.kwargs
    assert clear_kwargs["has_active_invocation"] is False
    assert clear_kwargs["requires_ai_review"] is False
    assert clear_kwargs["autopilot_pause_reason"] == ""


def test_awaiting_acceptance_session_is_not_cancelled(fake_db):
    fake_db["db"].fetch_one.return_value = _utc_stamped_row("awaiting_acceptance", 36000)
    host = MagicMock()

    assert _cancel_stalled_invocation(host, novel_id="n-1", session_id="sess-1") is False
    fake_db["db"].execute.assert_not_called()
    host._update_shared_state.assert_not_called()


def test_fresh_generating_session_is_not_cancelled(fake_db):
    fake_db["db"].fetch_one.return_value = _utc_stamped_row("generating", 30)
    host = MagicMock()

    assert _cancel_stalled_invocation(host, novel_id="n-1", session_id="sess-1") is False
    fake_db["db"].execute.assert_not_called()


def test_missing_session_is_treated_as_stale(fake_db):
    fake_db["db"].fetch_one.return_value = None
    host = MagicMock()

    assert _cancel_stalled_invocation(host, novel_id="n-1", session_id="sess-1") is True
    fake_db["db"].execute.assert_not_called()
    host._update_shared_state.assert_called_once()


def test_invocation_age_parses_utc_and_garbage():
    from engine.runtime.act_planning_delegate import _invocation_age_seconds

    assert _invocation_age_seconds("not-a-date") == float("inf")
    assert _invocation_age_seconds(None) == float("inf")
    age = _invocation_age_seconds("2000-01-01 00:00:00")
    assert age > 60 * 60 * 24 * 365


def _make_act_nodes():
    volume = SimpleNamespace(
        id="vol-1", number=1, node_type=SimpleNamespace(value="volume")
    )
    act53 = SimpleNamespace(
        id="act-53",
        number=53,
        node_type=SimpleNamespace(value="act"),
        suggested_chapter_count=8,
        parent_id="vol-1",
    )
    return [volume, act53], act53


@pytest.mark.asyncio
async def test_run_act_planning_recovers_after_stale_session(monkeypatch):
    """停滞会话被取消后，同一 tick 应重新发起规划并进入写作（全自动）。"""
    from domain.novel.entities.novel import AutopilotStatus, NovelStage

    act_nodes, act53 = _make_act_nodes()
    host = MagicMock()
    host._is_still_running.return_value = True
    host.story_node_repo.get_by_novel = AsyncMock(return_value=act_nodes)
    host.story_node_repo.get_children_sync.side_effect = [
        [],
        [SimpleNamespace(id="ch-1", node_type=SimpleNamespace(value="chapter"))],
    ]
    host.planning_service.confirm_act_planning = AsyncMock()

    novel = MagicMock()
    novel.novel_id.value = "n-1"
    novel.current_act = 52
    novel.target_chapters = 350
    novel.auto_approve_mode = True
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.current_stage = NovelStage.ACT_PLANNING

    shared = {
        "active_invocation_session_id": "sess-dead",
        "has_active_invocation": True,
        "requires_ai_review": True,
    }
    monkeypatch.setattr(act_planning_delegate, "_read_shared_state", lambda _nid: shared)

    cancelled = []
    monkeypatch.setattr(
        act_planning_delegate,
        "_cancel_stalled_invocation",
        lambda h, nid, sid: cancelled.append(sid) or True,
    )

    outcome = SimpleNamespace(
        status="completed",
        payload={
            "commit": SimpleNamespace(
                result={
                    "continuation": {
                        "act_plan": {
                            "chapters": [
                                {"title": "镜牢第一夜", "outline": "…", "word_target": 2000}
                            ]
                        }
                    }
                }
            )
        },
    )
    request_mock = AsyncMock(return_value=outcome)
    monkeypatch.setattr(act_planning_delegate, "_request_act_invocation", request_mock)

    await act_planning_delegate.run_act_planning(host, novel)

    assert cancelled == ["sess-dead"]
    request_mock.assert_awaited_once()
    host.planning_service.confirm_act_planning.assert_awaited_once()
    assert novel.current_stage == NovelStage.WRITING
    host._clear_llm_error_summary.assert_called_once_with("n-1")
    host._report_llm_failure.assert_not_called()


def _planning_test_fixture(monkeypatch, shared):
    act_nodes, act53 = _make_act_nodes()
    host = MagicMock()
    host._is_still_running.return_value = True
    host.story_node_repo.get_by_novel = AsyncMock(return_value=act_nodes)
    host.story_node_repo.get_children_sync.return_value = []
    host.planning_service.confirm_act_planning = AsyncMock()

    novel = MagicMock()
    novel.novel_id.value = "n-1"
    novel.current_act = 52
    novel.target_chapters = 350
    novel.auto_approve_mode = True
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.current_stage = NovelStage.ACT_PLANNING

    monkeypatch.setattr(act_planning_delegate, "_read_shared_state", lambda _nid: shared)
    return host, novel


@pytest.mark.asyncio
async def test_run_act_planning_failure_reports_summary_to_frontend(monkeypatch):
    from domain.novel.entities.novel import AutopilotStatus, NovelStage

    shared = {
        "autopilot_llm_failure_count": 0,
        "active_invocation_session_id": "sess-live",
        "has_active_invocation": False,
    }
    host, novel = _planning_test_fixture(monkeypatch, shared)
    monkeypatch.setattr(
        act_planning_delegate,
        "_cancel_stalled_invocation",
        lambda h, nid, sid: False,
    )
    request_mock = AsyncMock(side_effect=RuntimeError("Request timed out or interrupted"))
    monkeypatch.setattr(act_planning_delegate, "_request_act_invocation", request_mock)

    await act_planning_delegate.run_act_planning(host, novel)

    host._report_llm_failure.assert_called_once()
    args, kwargs = host._report_llm_failure.call_args
    assert kwargs["failure_count"] == 1
    assert "连续第 1/3 次" in args[1]
    assert "RuntimeError" in args[1]
    assert novel.autopilot_status == AutopilotStatus.RUNNING  # 未达 3 次不挂起
    assert novel.current_stage == NovelStage.ACT_PLANNING
    host._clear_llm_error_summary.assert_not_called()


@pytest.mark.asyncio
async def test_run_act_planning_third_failure_sets_error(monkeypatch):
    from domain.novel.entities.novel import AutopilotStatus, NovelStage

    shared = {"autopilot_llm_failure_count": 2}
    host, novel = _planning_test_fixture(monkeypatch, shared)
    request_mock = AsyncMock(side_effect=TimeoutError("read timeout"))
    monkeypatch.setattr(act_planning_delegate, "_request_act_invocation", request_mock)

    await act_planning_delegate.run_act_planning(host, novel)

    args, kwargs = host._report_llm_failure.call_args
    assert kwargs["failure_count"] == 3
    assert "连续第 3/3 次" in args[1]
    assert novel.autopilot_status == AutopilotStatus.ERROR


@pytest.mark.asyncio
async def test_run_act_planning_empty_payload_reports_summary(monkeypatch):
    from domain.novel.entities.novel import NovelStage

    shared = {"autopilot_llm_failure_count": 0}
    host, novel = _planning_test_fixture(monkeypatch, shared)
    outcome = SimpleNamespace(status="completed", payload={"commit": SimpleNamespace(result={})})
    monkeypatch.setattr(
        act_planning_delegate,
        "_request_act_invocation",
        AsyncMock(return_value=outcome),
    )

    await act_planning_delegate.run_act_planning(host, novel)

    args, kwargs = host._report_llm_failure.call_args
    assert kwargs["failure_count"] == 1
    assert "格式无效" in args[1]
    assert novel.current_stage == NovelStage.ACT_PLANNING


def test_retryable_stages_include_legacy_planning():
    """会话 metadata 里的旧 stage 值 'planning' 也应被启动清理覆盖。"""
    from application.engine.services.autopilot_recovery_policy import RETRYABLE_STAGES

    assert "planning" in RETRYABLE_STAGES
