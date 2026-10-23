"""Budget compression policy for context slots."""
from __future__ import annotations

from typing import Dict, List, Mapping, Optional

from application.engine.services.context_budget_models import ContextSlot


#: S-06：截断标记（**会进模型可见内容** ⇒ 写成「给模型看的话」）。
#:
#: 改造前这里是裸的 ``"..."`` ⇒ 模型看到的是「内容被砍掉了」，
#: 但不知道砍的是什么、也不知道该不该担心（而 T0 被砍的通常是
#: ``fact_lock`` —— **不得违反的既成事实**那块）。
#:
#: ⇒ 统一成本仓既有口径（``context_builder.py`` 的「（本块已按上限截断）」、
#: ``prose_composer.py`` 的「（硬事实块已按总长上限截断）」、
#: ``context_budget_allocator.py`` 的「（叙事校准注入已按上限截断）」）。
TRUNCATION_MARKER = "\n（此段已按上下文预算截断，仅为部分内容）"


def _mark_truncated(content: str, target_chars: int) -> str:
    """按上限截断，并**附上说明**（不许裸 ``...``）。"""
    if target_chars <= 0:
        return ""
    return content[:target_chars].rstrip() + TRUNCATION_MARKER


def cap_slots_by_own_limit(
    slots: Dict[str, ContextSlot],
    *,
    chars_per_token_zh: float,
) -> int:
    """把每个槽位裁到**它自己的** ``max_tokens``；返回被裁的槽位数。

    S-06a：T0 路径此前**从不查** ``max_tokens`` ⇒ V9 给每个 T0 槽位写的上限
    （fact_lock 1500 / character_anchors 1500 / context_brief 800 …，合计 8320）
    **形同虚设** ⇒ T0 能涨到 6 万 token。

    ⇒ 这里把那个上限**真正生效**（T1/T2/T3 走 ``allocate_tier`` 时本来就会用
    ``max_tokens``，只有 T0 漏了 ⇒ 两套口径 ⇒ **同一逻辑写两份**）。

    ⚠ 裁剪后**必须带说明**（S-06）—— 这些内容会进模型可见上下文。
    """
    capped = 0
    for slot in slots.values():
        if not slot.max_tokens or slot.max_tokens <= 0:
            continue
        if slot.tokens <= slot.max_tokens:
            continue
        target_chars = int(slot.max_tokens * chars_per_token_zh)
        slot.content = _mark_truncated(slot.content, target_chars)
        slot.tokens = slot.max_tokens
        capped += 1
    return capped


def truncate_t0_slots(
    t0_slots: Dict[str, ContextSlot],
    budget: int,
    *,
    chars_per_token_zh: float,
) -> int:
    """把 T0 槽位裁进预算，**按插入顺序**。

    ⚠ S-06（记账修正）：本函数此前**只**裁剪**第一个**溢出的槽位就 ``break``，
    其后的 T0 槽位**原封不动且不计入 total** ⇒
    ① 账面 ``total`` 严重低估 T0 真实用量；
    ② 最终上下文可大幅超预算（实测：budget=200 / a=100 / b=5000 / c=3000
    ⇒ 返回 200，实际内容 100 + 100 + **3000**）。

    ⇒ 现在分两步：
    1. **每个槽位先按自己的 ``max_tokens`` 裁**（V9 给每个槽位都写了上限：
       lifecycle_directive 600 / story_anchor 300 / narrative_promise 420 /
       narrative_contract 1400 / fact_lock 1500 / character_anchors 1500 /
       context_brief 800 / evolution_presenter 1200 / act_summary 600
       ⇒ T0 合计上限 **8320 tokens**）——
       ⚠ 这些上限此前**从未生效**（T0 路径不查 ``max_tokens``），
       这是「T0 无限膨胀」的根因；
    2. 若裁完仍超 ``budget``，再按插入顺序裁到装得下（**且如实计入 total**）。

    ⇒ 正常情况下第 1 步就够了（T0 上限 8320 < 40% 总预算 14000）
    ⇒ 40% 动态阈值保护退化为**兜底**，而不再是唯一防线。
    """
    total = 0
    # ── 第 1 步：按槽位自己的上限裁（幂等，重复裁无副作用） ──────────
    # ⚠ 放在这里而不是只在调用方做 ⇒ **谁调用本函数都先按各自上限裁**
    #   （否则直接调用它的路径会漏掉这一步）。
    cap_slots_by_own_limit(t0_slots, chars_per_token_zh=chars_per_token_zh)

    for slot in t0_slots.values():
        # ── 第 2 步：按总量预算裁（插入顺序，装得下就装） ───────────
        if total + slot.tokens <= budget:
            total += slot.tokens
            continue

        remaining = budget - total
        if remaining > 0:
            target_chars = int(remaining * chars_per_token_zh)
            slot.content = _mark_truncated(slot.content, target_chars)
            slot.tokens = remaining
            total += remaining
        else:
            # 装不下了 ⇒ 舍弃（与 allocate_tier 的口径一致），**并计入 total**。
            slot.content = ""
            slot.tokens = 0
    return total


def allocate_tier(
    tier_slots: Dict[str, ContextSlot],
    budget: int,
    compression_log: List[str],
    *,
    chars_per_token_zh: float,
    slot_weights: Optional[Mapping[str, float]] = None,
) -> int:
    """Allocate a budget for one tier by priority, mutating overflowing slots.

    Args:
        slot_weights: 槽位权重（0-1），由「本章需要什么记忆」规划调用给出。
            提供时**同时**决定两件事：

            1. **分配顺序** —— 按权重降序，没有权重的槽位排在有权重的之后，
               同组内保持静态 ``priority`` 次序。
            2. **有效硬上限** —— ``effective_max = min(max_tokens, max_tokens * w)``。
               权重只能**压低**上限，绝不能抬高，所以 token 硬上限这个不变量不受影响。

    不提供时行为与改造前完全一致（纯按静态 priority）。
    """
    if slot_weights:
        def _sort_key(item):
            name, slot = item
            raw = slot_weights.get(name)
            if raw is None:
                # 无权重 → 排在有权重之后，保持原有 priority 次序
                return (0, float(slot.priority))
            try:
                return (1, float(raw))
            except (TypeError, ValueError):
                return (0, float(slot.priority))

        sorted_slots = sorted(tier_slots.items(), key=_sort_key, reverse=True)
    else:
        sorted_slots = sorted(
            tier_slots.items(),
            key=lambda item: item[1].priority,
            reverse=True,
        )

    def _effective_max(slot: ContextSlot, slot_key: str) -> Optional[int]:
        """权重只压低硬上限，不抬高。

        返回 ``None`` 表示「这个槽位本来就没有硬上限」；
        返回 ``0`` 表示「有权重且权重为 0」—— 两者含义不同，必须区分，
        否则权重 0 会被当成「无上限」而完全失效。

        ⚠️ 查权重的键是 **dict key**（如 ``vector_recall``），不是 ``slot.name``
        （如 ``向量召回``）。真实槽位里两者**从不相同**，且 name 带括号后缀
        （``绝对事实边界(FACT_LOCK)``），不适合当 JSON 键。排序与封顶必须用同一个键。
        """
        if not slot.max_tokens or slot.max_tokens <= 0:
            return None
        raw = (slot_weights or {}).get(slot_key)
        if raw is None:
            return int(slot.max_tokens)
        try:
            weight = float(raw)
        except (TypeError, ValueError):
            return int(slot.max_tokens)
        weight = max(0.0, min(1.0, weight))
        return int(min(int(slot.max_tokens), int(slot.max_tokens * weight)))

    total_used = 0
    for name, slot in sorted_slots:
        # 权重要在「装得下」判断**之前**生效：否则内容装得下时权重不起作用，
        # 权重就只是个排序键而已，压不出任何东西。
        cap = _effective_max(slot, name)
        if cap is not None and slot.tokens > cap:
            original = slot.tokens
            target_chars = int(cap * chars_per_token_zh)
            # S-06：权重压低上限时也**不许裸 ...** ⇒ 要说明。
            slot.content = _mark_truncated(slot.content, target_chars) if cap > 0 else ""
            slot.tokens = cap
            compression_log.append(
                f"按权重压缩 {name}: {original} → {cap} tokens"
            )

        if total_used + slot.tokens <= budget:
            total_used += slot.tokens
            continue

        remaining = budget - total_used
        original_tokens = slot.tokens
        if slot.max_tokens and slot.max_tokens > 0:
            if remaining > slot.min_tokens:
                target_chars = int(remaining * chars_per_token_zh)
                slot.content = _mark_truncated(slot.content, target_chars)
                slot.tokens = remaining
                total_used += remaining
                compression_log.append(f"压缩 {name}: {original_tokens} → {remaining} tokens")
            else:
                slot.content = ""
                slot.tokens = 0
                compression_log.append(f"舍弃 {name}（预算不足）")
            continue

        if remaining > 0:
            target_chars = int(remaining * chars_per_token_zh)
            slot.content = _mark_truncated(slot.content, target_chars)
            slot.tokens = remaining
            total_used += remaining
            compression_log.append(f"截断 {name}: {original_tokens} → {remaining} tokens")
        else:
            slot.content = ""
            slot.tokens = 0

    return total_used
