"""宏观规划委托 — Phase 5 从 AutopilotDaemon 迁入 engine/runtime"""
from __future__ import annotations

import logging
from engine.runtime.protocols import DaemonHostProtocol
from typing import Any, Mapping

from domain.novel.entities.novel import AutopilotStatus, Novel, NovelStage

logger = logging.getLogger(__name__)


def _read_shared_state(novel_id: str) -> dict[str, Any]:
    from application.ai_invocation.autopilot.shared_state import read_autopilot_shared_state

    return read_autopilot_shared_state(novel_id)


def _consume_pending_macro_plan(host: "DaemonHostProtocol", *, novel_id: str, target_chapters: int) -> dict[str, Any] | None:
    shared = _read_shared_state(novel_id)
    pending_target = int(shared.get("autopilot_pending_macro_target_chapters") or 0)
    pending = shared.get("autopilot_pending_macro_plan")
    if pending_target != int(target_chapters or 0) or not isinstance(pending, dict):
        return None
    host._update_shared_state(
        novel_id,
        autopilot_pending_macro_plan=None,
        autopilot_pending_macro_target_chapters=None,
        active_invocation_session_id="",
        active_invocation_operation="",
        active_invocation_node_key="",
        active_invocation_status="completed",
        active_invocation_policy="",
        has_active_invocation=False,
        requires_ai_review=False,
        autopilot_pause_reason="",
        macro_structure_ready=False,
        writing_substep="macro_planning",
        writing_substep_label="宏观规划 · 写入结构树",
    )
    return dict(pending)


def _write_macro_variables(*, novel_id: str, variables: Mapping[str, Any], node_key: str) -> None:
    from application.ai_invocation.variable_hub import VariableWrite
    from infrastructure.persistence.database.connection import get_database
    from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteVariableHubRepository

    repo = SqliteVariableHubRepository(get_database())
    context_key = f"novel_id:{novel_id}"
    integer_aliases = {
        "target_chapters",
        "rec_parts",
        "rec_volumes_per_part",
        "rec_acts_per_volume",
        "rec_chapters_per_act",
        "total_recommended_acts",
    }
    variable_keys = {
        "planning_depth": "novel.planning.macro.depth",
        "rec_parts": "novel.planning.macro.rec_parts",
        "rec_volumes_per_part": "novel.planning.macro.rec_volumes_per_part",
        "rec_acts_per_volume": "novel.planning.macro.rec_acts_per_volume",
        "rec_chapters_per_act": "novel.planning.macro.rec_chapters_per_act",
        "total_recommended_acts": "novel.planning.macro.total_recommended_acts",
    }
    for alias, value in dict(variables or {}).items():
        if alias not in variable_keys:
            continue
        repo.set_value(
            VariableWrite(
                key=variable_keys[alias],
                value=value,
                context_key=context_key,
                source_node_key=node_key,
                source_trace_id=node_key,
                scope="novel",
                stage="planning",
                display_name=alias,
                value_type="integer" if alias in integer_aliases else "string",
            )
        )


def _build_macro_story_context_variables(variables: Mapping[str, Any]) -> dict[str, Any]:
    """把 ``build_quick_macro_variables`` 的产出映射成**模板声明的 alias**。

    模板（``planning-quick-macro/user.md``）用点号访问故事上下文：
    ``{{worldbuilding.content}}`` / ``{{locations.list}}`` / ``{{characters}}``；
    而 ``build_quick_macro_variables`` 给的是**根**形态（``worldbuilding`` /
    ``locations`` 是 dict，``characters`` 是文本）。

    ⚠ 为什么必须在这里摊平（真实跑章实测出来的坑，不是推演）：
      * invocation 解析器按 **binding.alias**（点号形态）匹配显式变量，
        根形态的 key 匹配不上 ⇒ 取不到值；
      * ``characters`` 是**必填**变量 ⇒ 取不到就报「必填变量缺失」⇒ 会话直接 blocked，
        **模型根本没被调用**（实测：attempt 记录为空）；
      * ``worldbuilding.content`` / ``locations.list`` 不是必填 ⇒ 取不到就**静默渲染成空串**，
        模型拿到的是「没有世界观、没有地点」的结构提案（更隐蔽的坑）。

    ⇒ 故事上下文由**调用方在调用时**提供（与 ``act_planning_delegate`` 同一套路），
    中枢只留结构类事实（planning_depth / rec_* 等）。
    """
    worldbuilding = variables.get("worldbuilding")
    locations = variables.get("locations")
    return {
        "characters": str(variables.get("characters") or ""),
        "worldbuilding.content": (
            str(worldbuilding.get("content") or "") if isinstance(worldbuilding, Mapping) else ""
        ),
        "locations.list": (
            str(locations.get("list") or "") if isinstance(locations, Mapping) else ""
        ),
    }


def _pause_for_invocation(host: "DaemonHostProtocol", novel: Novel, outcome) -> None:
    session = outcome.payload.get("session") if isinstance(outcome.payload, Mapping) else None
    policy_value = getattr(getattr(session, "policy", ""), "value", "") or "AUTOPILOT_PAUSE"
    host._update_shared_state(
        novel.novel_id.value,
        active_invocation_session_id=outcome.session_id,
        active_invocation_operation=outcome.operation,
        active_invocation_node_key=outcome.node_key,
        active_invocation_status=outcome.status,
        active_invocation_policy=policy_value,
        has_active_invocation=True,
        requires_ai_review=True,
        autopilot_pause_reason=outcome.autopilot_pause_reason or "awaiting_ai_review",
        macro_structure_ready=False,
        writing_substep="macro_planning",
        writing_substep_label="宏观规划 · AI 请求面板",
    )
    novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
    novel.autopilot_status = AutopilotStatus.RUNNING
    host._flush_novel(novel)


async def _assess_planning_depth(
    host: "DaemonHostProtocol", novel: Novel, target_chapters: int
) -> Optional[str]:
    """AI 自评规划深度；判不出来 ⇒ ``None``（调用方回落章数阈值）。"""
    llm_service = getattr(host, "llm_service", None)
    if llm_service is None:
        return None
    try:
        from application.engine.services.planning_depth_assessor import (
            PlanningDepthAssessor,
        )

        return await PlanningDepthAssessor(llm_service).assess(
            genre=str(getattr(novel, "genre", "") or ""),
            premise=str(getattr(novel, "premise", "") or ""),
            target_chapters=target_chapters,
        )
    except Exception as exc:
        logger.warning("规划深度自评失败，回落章数阈值: %s", exc)
        return None


async def _request_macro_invocation(host: "DaemonHostProtocol", novel: Novel, *, target_chapters: int) -> Any:
    from application.ai_invocation.autopilot.factory import get_or_create_autopilot_orchestrator
    from application.ai_invocation.autopilot.intents import AutopilotInvocationIntent
    from application.ai_invocation.autopilot.policy import AutopilotInvocationPolicyResolver
    from application.ai_invocation.contracts import ensure_invocation_contract
    from infrastructure.ai.generation_profiles import generation_config_from_profile
    from infrastructure.ai.prompt_keys import PLANNING_QUICK_MACRO
    from infrastructure.persistence.database.connection import get_database

    bible_context = host.planning_service._get_bible_context(novel.novel_id.value)
    # B-06：AI 自评「这本书的结构要铺到多细」（判不出来 ⇒ 章数阈值兜底）。
    ai_depth = await _assess_planning_depth(host, novel, target_chapters)
    variables = host.planning_service.build_quick_macro_variables(
        bible_context, target_chapters, ai_depth=ai_depth
    )
    runtime_variables = {
        alias: variables[alias]
        for alias in (
            "planning_depth",
            "rec_parts",
            "rec_volumes_per_part",
            "rec_acts_per_volume",
            "rec_chapters_per_act",
            "total_recommended_acts",
        )
        if alias in variables
    }
    # 故事上下文（角色 / 世界观 / 地点）**由调用方提供**：
    # 这几个 alias 在契约里是「调用方提供」口径，不绑中枢键
    # （中枢里根本没有它们 —— `novel.characters.list` 只有手动回填接口会写）。
    story_context_variables = _build_macro_story_context_variables(variables)
    ensure_invocation_contract("autopilot.macro.plan", PLANNING_QUICK_MACRO, get_database())
    _write_macro_variables(
        novel_id=novel.novel_id.value,
        variables=runtime_variables,
        node_key=PLANNING_QUICK_MACRO,
    )
    policy = AutopilotInvocationPolicyResolver().resolve(
        operation="autopilot.macro.plan",
        node_key=PLANNING_QUICK_MACRO,
        novel=novel,
        context={"novel_id": novel.novel_id.value, "target_chapters": target_chapters},
    )
    config = generation_config_from_profile("planning_macro")
    return await get_or_create_autopilot_orchestrator(host).request(
        AutopilotInvocationIntent(
            novel_id=novel.novel_id.value,
            stage="planning",
            operation="autopilot.macro.plan",
            node_key=PLANNING_QUICK_MACRO,
            context={"novel_id": novel.novel_id.value, "target_chapters": target_chapters},
            explicit_variables=story_context_variables,
            continuation_handler_key="autopilot_macro_plan",
            policy_hint=policy,
            metadata={"source": "macro_planning_delegate"},
            config=config,
        )
    )


async def run_macro_planning(host: "DaemonHostProtocol", novel: Novel) -> None:
    """处理宏观规划（规划部/卷/幕）- 使用极速模式让 AI 自主推断结构"""
    if not host._is_still_running(novel):
        return

    # H-05：上一次宏观结构冲突还没处理 ⇒ 不许再调 LLM。
    # 冲突时结构**没有落库**，若放任再次规划，每个 tick 都会重跑一次
    # 宏观规划（一次完整 LLM 调用），冲突还会再次发生 —— 白烧钱。
    # 用户在驾驶舱点「保留原结构，继续」时 /resume 会清掉暂停原因，这里自动解锁。
    from application.ai_invocation.autopilot.review_gate import (
        MACRO_STRUCTURE_CONFLICT_REASON,
    )

    if _read_shared_state(novel.novel_id.value).get("autopilot_pause_reason") == MACRO_STRUCTURE_CONFLICT_REASON:
        logger.info(
            "[%s] 宏观结构冲突未处理，跳过本轮宏观规划（不重复调用 LLM）",
            novel.novel_id.value,
        )
        return

    host._update_shared_state(
        novel.novel_id.value,
        writing_substep="macro_planning",
        writing_substep_label="宏观规划",
        macro_structure_ready=False,
    )

    # I-05：目标章数走**统一解析入口**（此前这里各写各的兜底，同一本书会按不同口径算结构预算）。
    from application.blueprint.services.target_budget import resolve_target_chapters

    target_chapters = resolve_target_chapters(novel.target_chapters)

    logger.info(
        "[%s] macro_planning start target_chapters=%s",
        novel.novel_id.value,
        target_chapters,
    )

    result = _consume_pending_macro_plan(
        host,
        novel_id=novel.novel_id.value,
        target_chapters=target_chapters,
    )
    if result is None:
        shared_state = _read_shared_state(novel.novel_id.value)
        if shared_state.get("active_invocation_session_id") and shared_state.get("has_active_invocation"):
            logger.info(
                "[%s] 宏观规划已有待处理 invocation session=%s，等待面板处理",
                novel.novel_id.value,
                shared_state.get("active_invocation_session_id"),
            )
            novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
            novel.autopilot_status = AutopilotStatus.RUNNING
            host._update_shared_state(
                novel.novel_id.value,
                macro_structure_ready=False,
                writing_substep="macro_planning",
                writing_substep_label="宏观规划 · AI 请求面板",
            )
            host._flush_novel(novel)
            return
        outcome = await _request_macro_invocation(host, novel, target_chapters=target_chapters)
        if outcome.status in {
            "awaiting_pre_call_review",
            "awaiting_acceptance",
            "awaiting_commit",
            "blocked",
            "failed",
            "cancelled",
        }:
            _pause_for_invocation(host, novel, outcome)
            return
        commit = outcome.payload.get("commit") if isinstance(outcome.payload, Mapping) else None
        commit_result = getattr(commit, "result", None) if commit is not None else None
        continuation = commit_result.get("continuation") if isinstance(commit_result, Mapping) else None
        result = dict((continuation or {}).get("macro_plan") or {})

    ok = bool(result.get("success"))
    n_parts = len(result.get("structure") or []) if isinstance(result.get("structure"), list) else -1
    logger.info(
        "[%s] macro_planning generate_macro_plan returned success=%s parts=%s",
        novel.novel_id.value,
        ok,
        n_parts,
    )

    if not host._is_still_running(novel):
        logger.info("[%s] 宏观规划 LLM 返回后检测到停止，不再落库", novel.novel_id)
        return

    try:
        apply_result = await host.planning_service.apply_macro_plan_from_llm_result(
            result,
            novel_id=novel.novel_id.value,
            target_chapters=target_chapters,
        )
    except Exception as exc:
        # H-05：结构性冲突（新结构会孤立已有正文的章节）——
        # **不落库、不推进阶段**，暂停待人工处理。
        # 之前这里没有兜：异常一路冒到 daemon 顶层（只打一条 error），
        # 小说停在 macro_planning ⇒ 下一 tick 又重新规划一次（每次都烧一次 LLM），
        # 而用户全程看不到「结构没落库」。
        from application.audit.services.macro_merge_engine import MergeConflictException

        if not isinstance(exc, MergeConflictException):
            raise
        logger.error(
            "[%s] 宏观结构冲突，已中止落库：%s",
            novel.novel_id.value,
            exc,
        )
        novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
        novel.autopilot_status = AutopilotStatus.RUNNING
        host._flush_novel(novel)
        host._update_shared_state(
            novel.novel_id.value,
            autopilot_pause_reason=MACRO_STRUCTURE_CONFLICT_REASON,
            macro_structure_ready=False,
            writing_substep="macro_planning",
            writing_substep_label="宏观规划 · 结构冲突待处理",
        )
        return

    # H-05：warning 档（旧节点仍承载未动笔的章节）不阻断落库，
    # 但必须有人看见 —— 旧节点已保留，等作者自己处理。
    merge_warnings = []
    if isinstance(apply_result, dict):
        merge_warnings = apply_result.get("warnings") or []
    if merge_warnings:
        logger.warning(
            "[%s] 宏观结构已落库，但有 %s 个旧节点仍承载未动笔的章节（已保留待人工处理）：%s",
            novel.novel_id.value,
            len(merge_warnings),
            [str(w.get("title") or w.get("node_id")) for w in merge_warnings],
        )

    host._update_shared_state(
        novel.novel_id.value,
        macro_structure_ready=True,
        writing_substep="macro_planning",
        writing_substep_label="宏观规划 · 结构已生成",
    )

    if getattr(novel, "auto_approve_mode", False):
        novel.current_stage = NovelStage.ACT_PLANNING
        host._flush_novel(novel)
        host._sync_storylines_to_shared_memory(novel.novel_id.value)
        logger.info("[%s] 全自动模式：宏观规划完成，直接进入幕级规划", novel.novel_id)
    else:
        novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
        host._flush_novel(novel)
        host._sync_storylines_to_shared_memory(novel.novel_id.value)
        logger.info("[%s] 宏观规划完成，进入审阅等待", novel.novel_id)
