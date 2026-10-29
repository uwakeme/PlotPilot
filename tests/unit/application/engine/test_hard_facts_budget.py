"""第 36 批：让**硬事实整条送达**（小书全量送到，用户 2026-10-28 拍板）。

背景（生产实测）
----------------
生产书 ``prodrealtest-001`` 第 5 章的事实锁 **74 条 / 4040 字 / 2296 tokens**，
但**真正送达模型的只有 2000 字（50%）** —— 被**两道我们自己的上限**削掉：

* 分配器里事实锁槽位的 ``max_tokens=1500``（**代码常量**）⇒ 丢 796 tokens
* ``prose_context.max_chars_per_slot=2000``（配置）⇒ 再丢 250 字

⇒ 死亡锁 / 白名单 / 身份锁 / 时间线这些**硬事实**有近一半**没进模型**
⇒ 而用户日常用的模型有 **50 万上下文** ⇒ 真正该抬的是**我们自己的上限**。

本批（用户拍板「小书全量送到」）
------------------------------
* 事实锁槽位上限：1500 → **4000** tokens（``MAX_FACT_LOCK_TOKENS``）
* 每块字符上限：2000 → **6000**（``prose_context.max_chars_per_slot``）
⇒ ⇒ 生产书的事实锁 **100% 送达**（实测：产出 4066 字、**无截断标记**）
⇒ 代价：硬事实包 +2040 字 ≈ **+1.4k token/章**（输入）

⚠ **长书**（529 章那本事实锁 ~13.5k tokens）仍会被削 ⇒ 那是**另一档**，本批不做。
"""
from __future__ import annotations

import pathlib

import yaml

from application.engine.services.context_budget_allocator import ContextBudgetAllocator
from application.engine.services.context_builder import ContextBuilder
from application.engine.services.context_budget_models import ContextSlot, PriorityTier
from domain.shared.token_estimation import estimate_tokens
from engine.core.services.context_budget_limits import DEFAULT_CONTEXT_MAX_TOKENS

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
PERFORMANCE_YAML = REPO_ROOT / "config" / "performance.yaml"

#: 生产书实测的事实锁规模（第 5 章）：74 条 / 4040 字 / 2296 tokens。
PRODUCTION_FACT_LOCK_CHARS = 4040
PRODUCTION_FACT_LOCK_TOKENS = 2296

#: 口径值：保证的**下限**（事实锁槽位 / 每块字符）。
#: ⚠ 第 41 批：4000 → **10000** / 5000 → **12000**（用户拍板「真正吃 1M 窗口」）
#:   ⇒ 这两个 floor 现在**钉住**新额度 ⇒ 退回旧值必红。
FACT_LOCK_TOKEN_FLOOR = 10000
PROSE_SLOT_CHAR_FLOOR = 12000


class _Slot:
    def __init__(self, content: str):
        self.content = content
        self.name = "绝对事实边界(FACT_LOCK)"


class _Allocation:
    def __init__(self, content: str):
        self.slots = {"fact_lock": _Slot(content)}


# ── ① 两处上限不许退回旧值 ─────────────────────────────────────────────


def test_the_fact_lock_slot_cap_is_at_least_the_floor():
    """⚠ 事实锁槽位的上限**不许**退回 1500（那会把生产书的事实锁砍掉 35%）。"""
    assert ContextBudgetAllocator.MAX_FACT_LOCK_TOKENS >= FACT_LOCK_TOKEN_FLOOR


def test_the_prose_slot_char_cap_is_at_least_the_floor():
    """⚠ 每块字符上限**不许**退回 2000（那会让硬事实包只送一半）。"""
    data = yaml.safe_load(PERFORMANCE_YAML.read_text(encoding="utf-8")) or {}
    cap = int(((data.get("prose_context") or {}).get("max_chars_per_slot")) or 0)

    assert cap >= PROSE_SLOT_CHAR_FLOOR


# ── ② 端到端：生产规模的事实锁 **整条送达** ─────────────────────────────


def test_a_production_sized_fact_lock_is_delivered_whole():
    """⚠ 生产书规模的事实锁（4040 字）⇒ 真实构建路径**整条送达**，**无截断标记**。"""
    fact_lock = "\n".join(f"- 事实条目 {i}：某某设定" for i in range(74))
    fact_lock = fact_lock + "补" * (PRODUCTION_FACT_LOCK_CHARS - len(fact_lock))

    text = ContextBuilder._build_hard_facts(_Allocation(fact_lock))

    assert "已按上限截断" not in text, "硬事实被截断了（上限没抬到位？）"
    assert len(text) >= len(fact_lock)


def test_the_fact_lock_slot_is_not_capped_by_its_own_limit():
    """⚠ 分配器那层也不许再砍它一刀（生产规模 2296 tokens < 上限）。"""
    allocator = ContextBudgetAllocator()
    slot = ContextSlot(
        name="绝对事实边界(FACT_LOCK)",
        tier=PriorityTier.T0_CRITICAL,
        content="事实" * 3000,
        tokens=PRODUCTION_FACT_LOCK_TOKENS,
        max_tokens=ContextBudgetAllocator.MAX_FACT_LOCK_TOKENS,
        priority=120,
    )

    capped = allocator._cap_t0_slots_by_own_limit({"fact_lock": slot})

    assert capped == 0, "事实锁被自己的上限砍了"
    assert slot.tokens == PRODUCTION_FACT_LOCK_TOKENS


# ── ③ 抬上限**不许**把 T0 的 40% 保护顶破 ────────────────────────────


def test_the_fact_lock_cap_stays_inside_the_t0_budget():
    """⚠ T0 最多占 40% 总预算 ⇒ 事实锁**自己**就吃不掉它。

    ⚠ 抬高上限的同时必须确认**不会**触发 ``_truncate_t0_slots``
    （那会把刚抬上来的内容又砍回去 ⇒ 抬了等于没抬）。
    """
    t0_budget = int(DEFAULT_CONTEXT_MAX_TOKENS * 0.40)

    assert ContextBudgetAllocator.MAX_FACT_LOCK_TOKENS < t0_budget


def test_the_production_fact_lock_fits_under_the_floor():
    """⚠ 生产书的事实锁规模必须**低于**本批设的下限（否则抬了也不够）。"""
    assert PRODUCTION_FACT_LOCK_TOKENS <= FACT_LOCK_TOKEN_FLOOR
    assert PRODUCTION_FACT_LOCK_CHARS <= PROSE_SLOT_CHAR_FLOOR


def test_the_prose_slot_cap_stays_below_the_allocator_cap():
    """⚠ **两处上限不许相等** —— 相等 ⇒ 「分配器保住、builder 切掉」那个**夹缝**消失
    ⇒ ⇒ 第 24 批「**被切掉的小节必须被点名**」那条守卫的场景**无法复现**
    ⇒ ⇒ 它会「因样本前提失效而**空过**」（绿，但什么都没验）。

    ⚠ 这就是本批把每块上限取 **5000** 而不是 6000 的原因：
      分配器给事实锁 4000 tokens × 1.5 = **6000 字** ⇒ 夹缝 = (5000, 6000]。
    """
    from application.engine.services.prose_context_settings import (
        get_prose_context_runtime_settings,
    )

    builder_cap = get_prose_context_runtime_settings().max_chars_per_slot
    allocator_cap_chars = int(ContextBudgetAllocator.MAX_FACT_LOCK_TOKENS * 1.5)

    assert builder_cap < allocator_cap_chars, (
        "builder 每块上限与分配器槽位上限**相等** ⇒ 夹缝消失 ⇒ "
        "「被截掉的小节必须被点名」那条守卫会空过"
    )
