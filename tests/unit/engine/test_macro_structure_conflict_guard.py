"""H-05：宏观结构合并的红色阻断不得被 fallback 吞掉。

背景（登记表 H-05 的真实病因链，比登记表描述更深）：

1. `confirm_macro_plan_safe` 在「新结构会孤立已有正文的章节」时抛
   `MergeConflictException`（红色阻断）—— 那是**数据安全**问题。
2. 但它的上游 `persist_macro_structure_with_fallback` 用一个
   `except Exception` 把**所有**异常都接住，回退到 `confirm_macro_plan` ——
   而后者的 docstring 自己写着「不检查已有数据，可能导致僵尸节点或数据丢失」。
   ⇒ 红色阻断在**两条入口**（守护进程落库 / API 生成落库）上被吞成
   「照写不误」，只有 `POST /macro/confirm` 那条路真的 409。
3. 守护进程那边更糟：异常一路冒到 daemon 顶层（只打一条 error），
   小说停在 macro_planning ⇒ 下一 tick 又重跑一次宏观规划（每次烧一次 LLM），
   而用户全程看不到「结构没落库」。

本文件的守卫覆盖：服务层不吞冲突 / 守护进程暂停待人工 / 不重复烧 LLM /
全自动模式也不许跳过暂停 / 审阅闸门文案说真话 / API 回 409。
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.ai_invocation.autopilot.review_gate import (
    MACRO_STRUCTURE_CONFLICT_REASON,
    review_gate_from_status,
)
from application.audit.services.macro_merge_engine import MergeConflictException
from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
)
from domain.novel.entities.novel import AutopilotStatus, NovelStage
from engine.runtime.macro_planning_delegate import run_macro_planning
from engine.runtime.novel_lifecycle import process_novel


def _conflict() -> MergeConflictException:
    return MergeConflictException(
        message="重构导致部分已有正文的章节孤立",
        conflicts=[{"node_id": "act-1", "reason": "carries_content"}],
    )


def _service() -> ContinuousPlanningService:
    svc = ContinuousPlanningService.__new__(ContinuousPlanningService)
    svc.confirm_macro_plan_safe = AsyncMock()
    svc.confirm_macro_plan = AsyncMock(return_value={"created_nodes": 9, "message": "回退写入"})
    svc.story_node_repo = MagicMock()
    svc.story_node_repo.get_by_novel = AsyncMock(return_value=[])
    return svc


# ─────────────────────────── 服务层：冲突不许回退 ───────────────────────────


@pytest.mark.asyncio
async def test_conflict_is_not_swallowed_by_unsafe_fallback():
    """红色阻断必须原样上抛，**绝不许**回退到「不检查已有数据」的旧写入。"""
    svc = _service()
    svc.confirm_macro_plan_safe = AsyncMock(side_effect=_conflict())

    with pytest.raises(MergeConflictException):
        await svc.persist_macro_structure_with_fallback(
            novel_id="n-1",
            structure=[{"title": "第一部", "volumes": [{"title": "卷一", "acts": []}]}],
        )

    # 关键断言：不安全的那条路一次都没被走到。
    svc.confirm_macro_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_conflict_failure_still_falls_back():
    """反向守卫：普通异常仍要回退（fallback 不是死代码）。"""
    svc = _service()
    svc.confirm_macro_plan_safe = AsyncMock(side_effect=RuntimeError("偶发失败"))

    result = await svc.persist_macro_structure_with_fallback(
        novel_id="n-1",
        structure=[{"title": "第一部", "volumes": [{"title": "卷一", "acts": []}]}],
    )

    svc.confirm_macro_plan.assert_awaited_once()
    assert result["created_nodes"] == 9


# ─────────────────────── 守护进程：冲突 ⇒ 暂停待人工 ───────────────────────


def _macro_invocation_outcome() -> SimpleNamespace:
    return SimpleNamespace(
        status="completed",
        session_id="session-1",
        operation="autopilot.macro.plan",
        node_key="planning-quick-macro",
        autopilot_pause_reason="",
        payload={
            "commit": SimpleNamespace(
                result={"continuation": {"macro_plan": {"success": True, "structure": []}}}
            )
        },
    )


@pytest.mark.asyncio
async def test_daemon_pauses_for_review_on_macro_conflict(monkeypatch):
    """冲突 ⇒ 停在 PAUSED_FOR_REVIEW，共享状态写明原因，不推进阶段。"""
    host = MagicMock()
    host._is_still_running.return_value = True
    host.planning_service.apply_macro_plan_from_llm_result = AsyncMock(
        side_effect=_conflict()
    )
    novel = MagicMock()
    novel.novel_id.value = "n-1"
    novel.target_chapters = 12
    novel.auto_approve_mode = False

    monkeypatch.setattr(
        "engine.runtime.macro_planning_delegate._read_shared_state",
        lambda _novel_id: {},
    )
    monkeypatch.setattr(
        "engine.runtime.macro_planning_delegate._request_macro_invocation",
        AsyncMock(return_value=_macro_invocation_outcome()),
    )

    await run_macro_planning(host, novel)

    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    host._update_shared_state.assert_any_call(
        "n-1",
        autopilot_pause_reason=MACRO_STRUCTURE_CONFLICT_REASON,
        macro_structure_ready=False,
        writing_substep="macro_planning",
        writing_substep_label="宏观规划 · 结构冲突待处理",
    )
    # 结构没有落库 —— 也不许「顺便」回退写一次。
    assert host.planning_service.apply_macro_plan_from_llm_result.await_count == 1


@pytest.mark.asyncio
async def test_daemon_does_not_replan_while_conflict_pending(monkeypatch):
    """冲突未处理时不得再跑一次宏观规划（每个 tick 烧一次 LLM）。"""
    host = MagicMock()
    host._is_still_running.return_value = True
    host.planning_service.apply_macro_plan_from_llm_result = AsyncMock()
    novel = MagicMock()
    novel.novel_id.value = "n-1"
    novel.target_chapters = 12
    novel.auto_approve_mode = False

    monkeypatch.setattr(
        "engine.runtime.macro_planning_delegate._read_shared_state",
        lambda _novel_id: {"autopilot_pause_reason": MACRO_STRUCTURE_CONFLICT_REASON},
    )
    request_invocation = AsyncMock(return_value=_macro_invocation_outcome())
    monkeypatch.setattr(
        "engine.runtime.macro_planning_delegate._request_macro_invocation",
        request_invocation,
    )

    await run_macro_planning(host, novel)

    request_invocation.assert_not_awaited()
    host.planning_service.apply_macro_plan_from_llm_result.assert_not_awaited()


# ───────────────── 全自动模式：冲突未处理也不许跳过人工闸门 ─────────────────


@pytest.mark.asyncio
async def test_auto_approve_does_not_skip_pending_macro_conflict(monkeypatch):
    """全自动模式会跳过人工审阅闸门 —— 但结构性冲突下不能跳。"""
    monkeypatch.setattr(
        "application.ai_invocation.autopilot.shared_state.read_autopilot_shared_state",
        lambda _novel_id: {"autopilot_pause_reason": MACRO_STRUCTURE_CONFLICT_REASON},
    )
    host = MagicMock()
    host._is_still_running.return_value = True
    host.circuit_breaker = None
    novel = MagicMock()
    novel.novel_id.value = "n-1"
    novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.auto_approve_mode = True

    await process_novel(host, novel)

    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW


@pytest.mark.asyncio
async def test_auto_approve_still_skips_normal_review_gate(monkeypatch):
    """反向守卫：没有冲突时，全自动模式照旧跳过人工闸门（功能没被改坏）。"""
    monkeypatch.setattr(
        "application.ai_invocation.autopilot.shared_state.read_autopilot_shared_state",
        lambda _novel_id: {},
    )
    host = MagicMock()
    host._is_still_running.return_value = True
    host.circuit_breaker = None
    novel = MagicMock()
    novel.novel_id.value = "n-1"
    novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.auto_approve_mode = True

    await process_novel(host, novel)

    assert novel.current_stage == NovelStage.ACT_PLANNING


# ─────────────────────────── 审阅闸门：文案必须说真话 ───────────────────────────


def test_review_gate_for_macro_conflict_is_honest_and_resumable():
    """冲突时闸门不能说「宏观结构已生成」—— 新结构一个节点都没落库。"""
    gate = review_gate_from_status(
        {
            "current_stage": "paused_for_review",
            "autopilot_status": "running",
            "autopilot_pause_reason": MACRO_STRUCTURE_CONFLICT_REASON,
            "macro_structure_ready": False,
            "writing_substep": "macro_planning",
            "current_auto_chapters": 3,
        }
    )

    assert gate is not None
    assert gate["type"] == "macro_plan"
    assert gate["can_resume"] is True
    assert gate["primary_action"] == "resume"
    message = str(gate["message"])
    assert "冲突" in message
    assert "保留" in message
    # 旧文案（"宏观结构已生成"）在这里是**假的**：新结构根本没落库。
    assert "已生成" not in message


def test_review_gate_without_conflict_keeps_normal_macro_message():
    """反向守卫：没有冲突时，闸门文案不受影响。"""
    gate = review_gate_from_status(
        {
            "current_stage": "paused_for_review",
            "autopilot_status": "running",
            "autopilot_pause_reason": "",
            "macro_structure_ready": True,
            "writing_substep": "macro_planning",
            "current_auto_chapters": 3,
        }
    )

    assert gate is not None
    assert "已生成" in str(gate["message"])


# ───────────────────────────── API：冲突回 409 ─────────────────────────────


@pytest.mark.asyncio
async def test_plan_endpoint_returns_409_on_macro_conflict():
    """API 生成落库路径上，冲突必须是 409（与 POST /macro/confirm 一致）。"""
    from fastapi import HTTPException

    from interfaces.api.v1.engine.generation import PlanRequest, plan_novel

    novel_service = MagicMock()
    novel_service.get_novel.return_value = SimpleNamespace(
        title="测试书", target_chapters=30
    )
    planning = MagicMock()
    planning.generate_macro_plan = AsyncMock(
        return_value={"success": True, "structure": [{"title": "第一部"}]}
    )
    planning.apply_macro_plan_from_llm_result = AsyncMock(side_effect=_conflict())

    with pytest.raises(HTTPException) as exc_info:
        await plan_novel(
            "n-1",
            PlanRequest(),
            MagicMock(),
            MagicMock(),
            novel_service,
            MagicMock(),
            planning,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"] == "MERGE_CONFLICT"
    assert exc_info.value.detail["conflicts"] == [
        {"node_id": "act-1", "reason": "carries_content"}
    ]
