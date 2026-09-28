"""目标章数达成 → 故事走向决策。

目标章数是软目标：达成时不再一刀切置 COMPLETED，而是按书目策略
（generation_prefs.target_reached_policy）分流：

- ask：暂停待人（调用方置 PAUSED_FOR_REVIEW + autopilot_pause_reason='target_reached'），
  由用户在驾驶舱选择 写终局 / 继续写 / 就此完结；
- finale：首次达标自动加写终局预算（finale_chapter_budget）并置 finale_mode 继续写；
  预算写完再次达标 → 真正完结并复位标记；
- continue：自动上调目标续写，永不自动完结（由用户手动停止/完结）；
- complete：保持旧行为，直接完结。

供 audit_delegate（章末审计权威判定）与 legacy_writing_delegate（写作前门槛）共用；
本模块只改偏好/目标并落库，阶段与状态的赋值由调用方完成。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from domain.novel.entities.novel import Novel
from domain.novel.value_objects.generation_preferences import GenerationPreferences

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注，避免运行时循环导入
    from engine.runtime.daemon_host import DaemonHost

logger = logging.getLogger(__name__)

# 暂停原因键值：驾驶舱据此渲染「已达目标章数 · 请决定故事走向」决策卡片
TARGET_REACHED_PAUSE_REASON = "target_reached"

# 终局预算钳制范围（与 GenerationPreferences.from_dict 的钳制一致）
_FINALE_BUDGET_MIN = 5
_FINALE_BUDGET_MAX = 200


def _clamp_finale_budget(budget: int) -> int:
    return max(_FINALE_BUDGET_MIN, min(_FINALE_BUDGET_MAX, int(budget)))


def resolve_target_reached(
    host: "DaemonHost",
    novel: Novel,
    completed_count: int | None = None,
) -> Literal["complete", "pause", "extended"]:
    """处理「完成章数已达目标章数」，返回调用方应执行的动作。

    - 'complete'：调用方置 COMPLETED（终止态）；
    - 'pause'：调用方置 PAUSED_FOR_REVIEW 并写
      autopilot_pause_reason=TARGET_REACHED_PAUSE_REASON；
    - 'extended'：目标已自动上调（终局预算/续写块），按继续写作处理。

    偏好变更与 novel（含新 target_chapters）落库在本函数内完成。
    """
    prefs: GenerationPreferences = (
        getattr(novel, "generation_prefs", None) or GenerationPreferences()
    )
    target = int(novel.target_chapters or 0)
    policy = (prefs.target_reached_policy or "ask").strip().lower()

    # 仅需要加写预算的策略才查询真实完成数（ask/complete 无需额外 DB 开销）
    if policy in ("finale", "continue") and completed_count is None:
        completed_count = int(host._count_completed_chapters(novel.novel_id))
    completed_count = max(completed_count or 0, 0)

    if policy == "finale":
        if prefs.finale_mode:
            # 终局预算已写完：真正完结，复位标记
            novel.generation_prefs = GenerationPreferences.merge_patch(
                prefs, {"finale_mode": False}
            )
            host._save_novel_state(novel)
            logger.info(
                "[%s] 终局预算写完（完成 %s 章），全书收束完结",
                novel.novel_id,
                completed_count,
            )
            return "complete"
        budget = _clamp_finale_budget(prefs.finale_chapter_budget or 20)
        novel.generation_prefs = GenerationPreferences.merge_patch(
            prefs, {"finale_mode": True}
        )
        novel.target_chapters = max(target, completed_count) + budget
        host._save_novel_state(novel)
        logger.info(
            "[%s] 目标章数 %s 达成（完成 %s）：写终局，加写 %s 章收束（新目标 %s）",
            novel.novel_id,
            target,
            completed_count,
            budget,
            novel.target_chapters,
        )
        return "extended"

    if policy == "continue":
        block = max(10, round(target * 0.1)) if target > 0 else 10
        novel.target_chapters = max(target, completed_count) + block
        host._save_novel_state(novel)
        logger.info(
            "[%s] 目标章数 %s 达成（完成 %s）：软目标续写，上调 %s 章（新目标 %s）",
            novel.novel_id,
            target,
            completed_count,
            block,
            novel.target_chapters,
        )
        return "extended"

    if policy == "complete":
        return "complete"

    return "pause"
