"""D-02 前置：上下文槽位权重的外部注入入口。

登记表对 D-02 的指控原文是「构造器只收 `phase_thresholds`，**无任何外部注入权重的入口**，
全仓无覆写」—— 本文件钉住这个入口存在，且**不破坏任何既有不变量**。

权重语义（Q2 已拍板）
----------------------
AI 输出**权重(0-1)**，不是绝对 token 数。因为 7 个槽位硬上限
（伏笔 2000 / 锚点 1500 / 图谱 1500 / 幕摘要 1500 / 最近章节 8000 /
向量召回 5000 / 契约 1400）在登记表里明确归代码不变量；让 AI 直接给绝对 token
等于让它与硬上限打架，越界处理又退化成「静默截断」。

权重的两个作用
--------------
1. **分配顺序**：按权重降序，无权重的槽位排在有权重的之后，同组按静态 priority
2. **有效硬上限**：`effective_max = min(max_tokens, max_tokens * w)` ——
   权重只能**压低**上限，**绝不能抬高**

红线
----
权重是「本章需要什么记忆」的表达，**不是**「可以突破上限」的通行证。
`test_weight_can_never_raise_the_hard_cap` 专门钉这一条。
"""
import pytest

from application.engine.services.context_budget_allocator import (
    ContextBudgetAllocator,
)
from application.engine.services.context_budget_models import (
    ContextSlot,
    PriorityTier,
)
from application.engine.services.context_budget_policy import allocate_tier

CPT = 2.0  # chars_per_token_zh，简化取值


def _slot(name: str, tokens: int, *, priority: int, max_tokens: int = 0,
          tier=PriorityTier.T2_DYNAMIC) -> ContextSlot:
    return ContextSlot(
        name=name,
        tier=tier,
        content="字" * (tokens * 2),
        tokens=tokens,
        max_tokens=max_tokens,
        priority=priority,
    )


def _run(slots, budget, weights=None):
    log: list[str] = []
    used = allocate_tier(
        slots, budget, log, chars_per_token_zh=CPT, slot_weights=weights
    )
    return used, log


# ── 不传权重时行为不变 ──────────────────────────────────────────────────

def test_without_weights_allocation_is_identical_to_before():
    """回归守卫：改造前就是纯按 priority 排，不传权重必须逐位一致。"""
    slots = {
        "high": _slot("high", 100, priority=90, max_tokens=500),
        "mid": _slot("mid", 100, priority=50, max_tokens=500),
        "low": _slot("low", 100, priority=10, max_tokens=500),
    }
    used, log = _run(slots, 1000)

    assert used == 300, log
    assert log == [], "预算充裕时不应产生任何压缩日志"
    assert {k: s.tokens for k, s in slots.items()} == {
        "high": 100, "mid": 100, "low": 100,
    }


def test_none_weights_is_same_as_omitting():
    """None 与不传必须等价 —— 接线不能靠「传了个 None 就换条路」。"""
    slots = {
        "a": _slot("a", 100, priority=90, max_tokens=500),
        "b": _slot("b", 100, priority=10, max_tokens=500),
    }
    used_none, _ = _run(slots, 150, weights=None)
    assert used_none == 150


# ── 权重压低上限 ────────────────────────────────────────────────────────

def test_weight_caps_slot_even_when_budget_is_ample():
    """核心行为：预算装得下时，权重也必须把该槽位压到上限。

    这条专门防一种「看起来接上了」的假实现 —— 如果权重只在预算不足时才生效，
    那么绝大多数正常章节（预算充裕）里权重完全不起作用。
    """
    slots = {"foreshadowing": _slot("foreshadowing", 800, priority=90,
                                    max_tokens=2000)}
    used, log = _run(slots, budget=100000, weights={"foreshadowing": 0.25})

    assert slots["foreshadowing"].tokens == 500, (
        "权重 0.25 × 上限 2000 = 500；预算充裕也必须生效"
    )
    assert used == 500
    assert any("按权重压缩" in line for line in log), log


def test_weight_of_one_leaves_the_cap_untouched():
    slots = {"s": _slot("s", 800, priority=50, max_tokens=2000)}
    used, log = _run(slots, 100000, weights={"s": 1.0})

    assert slots["s"].tokens == 800, "权重 1.0 不应压缩任何东西"
    assert log == []


def test_weight_can_never_raise_the_hard_cap():
    """红线：权重是「本章需要什么」的表达，不是突破上限的通行证。"""
    slots = {"s": _slot("s", 300, priority=50, max_tokens=500)}
    _run(slots, 100000, weights={"s": 5.0})

    assert slots["s"].tokens <= 500, (
        f"权重 5.0 把槽位撑到 {slots['s'].tokens}，突破了硬上限 500 —— "
        f"token 硬上限是代码不变量，权重只能压低"
    )


def test_weight_is_clamped_into_zero_one():
    """越界权重钳到 [0,1]，不得变成负数或异常放大。"""
    for raw, expect_le_zero in [(-3.0, True), (0.0, True)]:
        slots = {"s": _slot("s", 300, priority=50, max_tokens=500)}
        _run(slots, 100000, weights={"s": raw})
        assert slots["s"].tokens >= 0
        if expect_le_zero:
            assert slots["s"].tokens == 0, f"权重 {raw} 应把槽位压到 0"


def test_slot_without_max_tokens_is_not_capped_by_weight():
    """没有硬上限的槽位不该被权重凭空造出一个上限。"""
    slots = {"s": _slot("s", 300, priority=50, max_tokens=0)}
    _run(slots, 100000, weights={"s": 0.1})
    assert slots["s"].tokens == 300


# ── 权重改变分配顺序 ────────────────────────────────────────────────────

def test_weight_overrides_priority_for_allocation_order():
    """预算不够时，先服务权重高的那个 —— 即使它的静态 priority 更低。"""
    slots = {
        "important": _slot("important", 100, priority=10, max_tokens=500),
        "trivial": _slot("trivial", 100, priority=90, max_tokens=500),
    }
    used, log = _run(slots, budget=100, weights={"important": 0.9, "trivial": 0.1})

    assert slots["important"].tokens == 100, "权重高者应完整保留"
    assert slots["trivial"].tokens == 0, "权重低者应被舍弃"
    assert used == 100
    assert any("trivial" in line for line in log), log


def test_slots_without_weights_keep_priority_order_among_themselves():
    """混合场景：有权重的先按权重，无权重的按原 priority 次序在后。"""
    slots = {
        "weighted": _slot("weighted", 100, priority=1, max_tokens=500),
        "plain_hi": _slot("plain_hi", 100, priority=90, max_tokens=500),
        "plain_lo": _slot("plain_lo", 100, priority=20, max_tokens=500),
    }
    used, log = _run(slots, budget=200, weights={"weighted": 0.9})

    assert slots["weighted"].tokens == 100
    assert slots["plain_hi"].tokens == 100, "无权重者之间应保持 priority 降序"
    assert slots["plain_lo"].tokens == 0
    assert used == 200


def test_bad_weight_value_falls_back_to_priority_instead_of_crashing():
    """AI 可能输出脏值；不得因此崩，也不得把该槽位排到最前。"""
    slots = {
        "good": _slot("good", 100, priority=90, max_tokens=500),
        "junk": _slot("junk", 100, priority=10, max_tokens=500),
    }
    used, _log = _run(slots, budget=100, weights={"junk": "不是数字"})

    assert used == 100
    assert slots["good"].tokens == 100, "脏权重不应把 junk 顶到前面"


def test_weight_for_unknown_slot_name_is_ignored():
    slots = {"real": _slot("real", 100, priority=50, max_tokens=500)}
    used, log = _run(slots, 1000, weights={"不存在的槽位": 0.1})

    assert used == 100
    assert log == []


# ── 接线守卫 ────────────────────────────────────────────────────────────

def test_weight_is_matched_by_dict_key_not_by_slot_name():
    """**回归守卫**：真实槽位里 dict key ≠ ``ContextSlot.name``。

    例：``slots["vector_recall"]`` 的 ``name`` 是 ``"向量召回"``。
    早期实现里排序按 dict key 查、封顶按 ``slot.name`` 查 —— 两边查的不是同一个
    东西，权重只生效一半。本文件的其它用例里 key 与 name 恰好同名，所以照样全绿，
    这正是「替身形状与真实形状恰好重合」把测试骗过去的典型。

    这里让两者**故意不同**，任何一边查错都会露出来。
    """
    slots = {
        "vector_recall": _slot("向量召回", 800, priority=90, max_tokens=2000),
    }
    used, log = _run(slots, budget=100000, weights={"vector_recall": 0.25})

    assert slots["vector_recall"].tokens == 500, (
        f"权重按 dict key 给的，但封顶似乎没生效（tokens="
        f"{slots['vector_recall'].tokens}）—— 排序与封顶用了不同的键"
    )
    assert used == 500
    assert any("按权重压缩" in line for line in log), log


def test_weight_given_by_display_name_does_not_silently_apply():
    """反向守卫：用展示名当键必须**不生效**（而不是碰巧命中）。

    展示名带括号后缀（如 ``绝对事实边界(FACT_LOCK)``），不适合当契约键；
    若哪天有人改成接受展示名，这条会红并指出契约已变。
    """
    slots = {
        "vector_recall": _slot("向量召回", 800, priority=90, max_tokens=2000),
    }
    _used, log = _run(slots, budget=100000, weights={"向量召回": 0.25})

    assert slots["vector_recall"].tokens == 800, "展示名不该被当作权重键"
    assert log == []


def test_allocator_exposes_the_slot_weights_injection_point():
    """登记表指控「无任何外部注入权重的入口」—— 这条钉住入口存在。

    建出入口是 D-02 的前置；上游规划调用在后续切片填。不传则行为不变。
    """
    assert ContextBudgetAllocator(slot_weights={"s": 0.5}).slot_weights == {"s": 0.5}
    assert ContextBudgetAllocator().slot_weights is None
    assert ContextBudgetAllocator(slot_weights={}).slot_weights is None


def test_weights_actually_flow_from_allocator_into_allocation():
    """端到端接线守卫：权重必须真的走到 `allocate_tier`，而不是只被存下。

    只断言「属性被赋值」是空洞的 —— 把 allocator 里那行
    `slot_weights=self.slot_weights` 删掉，本文件其它用例仍会全绿。
    这条从 allocator 的 `_allocate_tier` 出发，验证端到端效果。
    """
    alloc = ContextBudgetAllocator(slot_weights={"foreshadowing": 0.25})
    slots = {"foreshadowing": _slot("foreshadowing", 800, priority=90,
                                    max_tokens=2000)}
    log: list[str] = []

    used = alloc._allocate_tier(slots, budget=100000, compression_log=log)

    assert slots["foreshadowing"].tokens == 500, (
        f"权重没有传到 allocate_tier（结果 tokens="
        f"{slots['foreshadowing'].tokens}，期望 500）—— "
        f"allocator 侧的接线断了"
    )
    assert used == 500
    assert any("按权重压缩" in line for line in log), log


def test_allocator_without_weights_keeps_old_behaviour():
    """反向守卫：不注入时端到端行为不变。"""
    alloc = ContextBudgetAllocator()
    slots = {"foreshadowing": _slot("foreshadowing", 800, priority=90,
                                    max_tokens=2000)}
    log: list[str] = []

    used = alloc._allocate_tier(slots, budget=100000, compression_log=log)

    assert slots["foreshadowing"].tokens == 800
    assert used == 800
    assert log == []
