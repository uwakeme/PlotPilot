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


def truncate_t0_slots(
    t0_slots: Dict[str, ContextSlot],
    budget: int,
    *,
    chars_per_token_zh: float,
) -> int:
    """Keep T0 slots in insertion order and truncate the first overflowing slot."""
    total = 0
    for slot in t0_slots.values():
        if total + slot.tokens <= budget:
            total += slot.tokens
            continue

        remaining = budget - total
        if remaining > 0:
            target_chars = int(remaining * chars_per_token_zh)
            slot.content = _mark_truncated(slot.content, target_chars)
            slot.tokens = remaining
            total += remaining
        break
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
