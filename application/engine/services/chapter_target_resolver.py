"""D-05：每章字数目标的裁决规则。

背景
----
登记表 D-05 的指控成立：**全链路没有任何 AI 输出通道**能设定本章字数。
``PlanningEnvelope.target_chapter_words`` 是**输入**（来自
``novel.target_words_per_chapter``），``PlanAtomSpec.weight`` 只是相对权重。
用户在向导里设的章长会一路进正文 prompt，但**AI 在任何环节都无权参与**。

本模块给出裁决：AI 可以提议，代码负责不越界。

三条不变量（登记表 D-05「代码保留的不变量」列）
----------------------------------------------
1. **500-20000 钳制** —— 复用既有 ``clamp_chapter_target_words``，
   不另立一套上限。
2. **不得偏离用户设定太远** —— 用户在向导里明确选过章长。AI 可以为某一章
   调整篇幅（比如余韵章短、爆点章长），但不能把整本书的节奏规则改掉。
   偏离超过 ``max_deviation_ratio`` 一律拒绝，沿用用户设定。
3. **卷/部总量上限**由幕级/部级规划负责，本模块不越权；它只看单章。

三态
----
``proposed`` 为 None（AI 没给 / 解析失败）⇒ 沿用用户设定，note 说明原因。
**不得**因为「AI 没说话」就编一个数出来。
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from application.core.chapter_target_limits import clamp_chapter_target_words

logger = logging.getLogger(__name__)

#: 默认允许的偏离比例。±30%：单章可因内容伸缩，但改不了全书节奏。
DEFAULT_MAX_DEVIATION_RATIO = 0.30


def resolve_chapter_target_words(
    user_target: int,
    proposed: Optional[int],
    *,
    max_deviation_ratio: float = DEFAULT_MAX_DEVIATION_RATIO,
) -> Tuple[int, str]:
    """裁决本章字数目标。

    Args:
        user_target: 用户设定的每章字数（``novel.target_words_per_chapter``）。
        proposed: AI 的提议；``None`` 表示没提议或不可用。
        max_deviation_ratio: 允许偏离用户设定的最大比例（0.30 = ±30%）。

    Returns:
        ``(最终值, 说明)``。最终值必定落在 [500, 20000]。

    注意：**AI 没说话不等于「按平均来」**。``proposed is None`` 时返回的就是
    用户设定本身，并附上说明 —— 调用方据此知道这不是 AI 的判断。
    """
    try:
        raw_user = int(user_target)
    except (TypeError, ValueError):
        raw_user = 0

    if raw_user <= 0:
        # 用户设定本身非法。注意必须判断**原始值** —— clamp 会把 0 抬到 500，
        # 判钳位后的值会让这个分支永远不可达（真 bug，已经踩过一次）。
        if proposed is None:
            return clamp_chapter_target_words(0), "用户设定非法且 AI 未给出，沿用下限"
        try:
            candidate = clamp_chapter_target_words(int(proposed))
        except (TypeError, ValueError):
            return clamp_chapter_target_words(0), "用户设定与 AI 提议均不可用，沿用下限"
        return candidate, "用户设定非法，已改为仅按 AI 提议"

    base = clamp_chapter_target_words(raw_user)

    if proposed is None:
        return base, "AI 未给出字数目标，沿用用户设定"

    try:
        raw = int(proposed)
    except (TypeError, ValueError):
        return base, f"AI 提议的 target_words 不可解析（{proposed!r}），沿用用户设定"

    candidate = clamp_chapter_target_words(raw)

    deviation = abs(candidate - base) / base
    if deviation > max_deviation_ratio:
        logger.warning(
            "AI 提议的每章字数 %s 偏离用户设定 %s 达 %.0f%%，超过上限 %.0f%%，已拒绝",
            candidate, base, deviation * 100, max_deviation_ratio * 100,
        )
        return base, (
            f"AI 提议 {candidate} 字偏离用户设定 {base} 字达 {deviation:.0%}，"
            f"超过 {max_deviation_ratio:.0%} 上限，已沿用用户设定"
        )

    note = f"采用 AI 提议 {candidate} 字（用户设定 {base} 字，偏离 {deviation:.0%}）"
    logger.info(note)
    return candidate, note
