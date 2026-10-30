"""第 68 批守卫：S-1（T0 护栏）+ S-2（权重非法值 fail-closed）。

来源：独立审查报告 ``review-briefs/2026-10-30-review-report-85c0f726-closeout.md`` 的 S-1 / S-2。

S-1
    ① T0 保护线的**注释口径过期 7600**（写 8320→14320，真实合计 21920）
       ⇒ 再抬任一 T0 槽位 ≥ 2080 tokens 就会命中保护线；
    ② 超线时「**按 dict 插入顺序**整块舍弃槽位」⇒ 牺牲谁取决于代码顺序，
       而不是取决于槽位的价值。

S-2
    ① D-02 权重非法值是「**钳制**」不是 fail-closed：负数被钳到 0
       ⇒ 权重 0 ⇒ 整个槽位被**清空**且模型侧**无痕**；
    ② ``cap == 0`` 时连占位说明都没有 ⇒ 模型无法分辨「被裁掉」与「本来就没有」。

本文件钉住
----------
1. T0 合计**从生产代码重算**（不做手工副本）⇒ 注释过期会红；
2. T0 合计必须**低于** 40% 保护线；
3. 舍弃顺序由**价值**（priority）决定，不由插入顺序决定；
4. 非法权重**不生效**（不许钳制、不许清空内容）；
5. 权重 0 ⇒ 留**模型可见**的占位说明。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
ALLOCATOR_PATH = ROOT / "application" / "engine" / "services" / "context_budget_allocator.py"


def _t0_slot_caps() -> dict[str, int]:
    """从**生产代码**里解析出 T0 槽位及其上限（不做手工副本 ⇒ 不会漂）。

    ⚠ 为什么要 AST 而不是读常量：T0 槽位的上限**一半是类常量、一半是内联字面量**
      （600 / 300 / 420 / 800 / 1200 / 600 是内联的）⇒ 手工列一份必然会漂。
    """
    from application.engine.services.context_budget_allocator import ContextBudgetAllocator

    tree = ast.parse(ALLOCATOR_PATH.read_text(encoding="utf-8"))
    caps: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if getattr(call.func, "id", "") != "ContextSlot":
            continue
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        tier = kwargs.get("tier")
        if tier is None or ast.unparse(tier) != "PriorityTier.T0_CRITICAL":
            continue
        cap = kwargs.get("max_tokens")
        if isinstance(cap, ast.Constant):
            value = int(cap.value)
        elif isinstance(cap, ast.Attribute):
            value = int(getattr(ContextBudgetAllocator, cap.attr))
        else:
            continue
        # dict 键（``slots["fact_lock"] = ...``）才是槽位的稳定标识
        target = node.targets[0]
        if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
            caps[str(target.slice.value)] = value
    return caps


def _t0_max_ratio() -> float:
    """从**生产代码**里读出 T0 的保护比例（``T0_MAX_RATIO`` 是方法内的局部变量）。"""
    tree = ast.parse(ALLOCATOR_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and target.id == "T0_MAX_RATIO"
                and isinstance(node.value, ast.Constant)
            ):
                return float(node.value.value)
    raise AssertionError("T0_MAX_RATIO 没在生产代码里找到")


# ─────────────────────── 1) T0 合计（从生产代码重算） ───────────────────────


def test_the_t0_total_is_below_the_protection_line():
    """⚠ T0 合计上限必须**低于** 40% 保护线（否则每次都要走截断路径）。"""
    from engine.core.services.context_budget_limits import DEFAULT_CONTEXT_MAX_TOKENS

    caps = _t0_slot_caps()
    assert len(caps) >= 9, f"T0 槽位没解析全（只找到 {len(caps)} 个）：{caps}"

    total = sum(caps.values())
    protection = int(DEFAULT_CONTEXT_MAX_TOKENS * _t0_max_ratio())

    assert total < protection, (
        f"T0 合计 {total} 已经 ≥ 保护线 {protection} ⇒ 每次分配都要走截断路径"
        f"（槽位上限：{caps}）"
    )


def test_the_t0_total_comment_is_not_stale():
    """⚠ 注释里的 T0 合计必须与**生产代码重算**的一致（不许再写拍脑袋的数）。

    ⚠ 这条守卫存在的理由：原文写「8320 → 14320 ⇒ < 24000」，
      而真实合计是 21920（**过期 7600**）⇒ 注释让人以为余量还很厚。
    """
    from engine.core.services.context_budget_limits import DEFAULT_CONTEXT_MAX_TOKENS

    caps = _t0_slot_caps()
    total = sum(caps.values())
    protection = int(DEFAULT_CONTEXT_MAX_TOKENS * _t0_max_ratio())
    margin = protection - total

    src = ALLOCATOR_PATH.read_text(encoding="utf-8")

    assert f"T0 合计上限 = {total}" in src, (
        f"注释里的 T0 合计不是 {total}（真实合计 {total}，余量 {margin}）"
        f"⇒ 注释口径过期 ⇒ 下一个人会按错的口径抬上限"
    )
    assert f"余量只剩 {margin} tokens" in src, (
        f"注释里的余量不是 {margin}（真实余量 {margin}）⇒ 口径过期"
    )


# ─────────────────────── 2) 舍弃顺序由价值决定 ───────────────────────


def test_the_t0_drop_order_is_by_value_not_insertion_order():
    """⚠ 超线时牺牲**低价值**槽位（priority 低的那个），不是「字典里靠后的那个」。

    ⚠ 样本刻意把**低价值的槽位放在最前面**插入 ⇒ 按插入顺序会先牺牲它，
      按价值顺序则应该牺牲 priority 最低的那个。
    """
    from application.engine.services.context_budget_models import ContextSlot, PriorityTier
    from application.engine.services.context_budget_policy import truncate_t0_slots

    def _slot(name: str, tokens: int, priority: int) -> ContextSlot:
        return ContextSlot(
            name=name,
            tier=PriorityTier.T0_CRITICAL,
            content="x" * tokens,
            tokens=tokens,
            max_tokens=0,
            priority=priority,
        )

    slots = {
        # ⚠ 插入顺序：**低价值在前**（按插入顺序会先牺牲它 ⇒ 错）
        "low_value": _slot("低价值", 100, priority=10),
        "high_value": _slot("高价值", 100, priority=90),
    }
    log: list[str] = []

    truncate_t0_slots(slots, 100, chars_per_token_zh=1.0, compression_log=log)

    assert slots["low_value"].tokens == 0, "低价值的槽位没有被牺牲"
    assert slots["high_value"].tokens == 100, "高价值的槽位被牺牲了 ⇒ 舍弃顺序按插入顺序走的"
    assert log and "低价值" in log[0], log


# ─────────────────────── 3) 非法权重 fail-closed ───────────────────────


@pytest.mark.parametrize(
    "bad",
    [-3.0, 5.0, "abc", None, True, False],
)
def test_an_invalid_weight_is_rejected_instead_of_clamped(bad):
    """⚠ 非法权重**不生效**（不许钳制）⇒ 那一节的内容**不许消失**。

    ⚠ 以前负数被钳到 0 ⇒ 权重 0 ⇒ 整块清空 ⇒ AI 一个错值就能抹掉一节。
    """
    from application.engine.services.context_weight_catalog import sanitize_weights

    cleaned = sanitize_weights({"recent_chapters": bad})

    assert "recent_chapters" not in cleaned, (
        f"非法权重 {bad!r} 被当成了有效权重：{cleaned}"
    )


def test_a_valid_weight_still_works():
    """⚠ 回归：合法权重**照常生效**（fail-closed 不能把功能也关掉）。"""
    from application.engine.services.context_weight_catalog import sanitize_weights

    assert sanitize_weights({"recent_chapters": 0.25}) == {"recent_chapters": 0.25}
    assert sanitize_weights({"recent_chapters": 0.0}) == {"recent_chapters": 0.0}
    assert sanitize_weights({"recent_chapters": 1.0}) == {"recent_chapters": 1.0}


def test_an_invalid_weight_keeps_the_hard_cap():
    """⚠ 策略层同样 fail-closed：非法权重 ⇒ 保留**完整**上限（内容不丢）。"""
    from application.engine.services.context_budget_models import ContextSlot, PriorityTier
    from application.engine.services.context_budget_policy import allocate_tier

    slot = ContextSlot(
        name="s",
        tier=PriorityTier.T1_COMPRESSIBLE,
        content="x" * 300,
        tokens=300,
        max_tokens=500,
        priority=50,
    )
    log: list[str] = []

    allocate_tier(
        {"s": slot},
        100000,
        log,
        chars_per_token_zh=1.0,
        slot_weights={"s": -3.0},
    )

    assert slot.tokens == 300, (
        f"非法权重把槽位压到了 {slot.tokens} ⇒ 内容被清空了（应保留完整上限）"
    )


# ─────────────────────── 4) 权重 0 ⇒ 模型可见的占位说明 ───────────────────────


def test_a_zero_weight_leaves_a_model_visible_placeholder():
    """⚠ 权重 0 ⇒ 内容不给，但**必须留一句占位说明**。

    ⚠ 以前是 ``content = ""`` ⇒ 这一节从上下文里彻底消失
    ⇒ 模型无法分辨「被裁掉」与「本来就没有」。
    """
    from application.engine.services.context_budget_models import ContextSlot, PriorityTier
    from application.engine.services.context_budget_policy import (
        WEIGHT_ZERO_PLACEHOLDER,
        allocate_tier,
    )

    slot = ContextSlot(
        name="recent_chapters",
        tier=PriorityTier.T2_DYNAMIC,
        content="上一章的正文" * 100,
        tokens=800,
        max_tokens=8000,
        priority=50,
    )
    log: list[str] = []

    allocate_tier(
        {"recent_chapters": slot},
        100000,
        log,
        chars_per_token_zh=1.5,
        slot_weights={"recent_chapters": 0.0},
    )

    assert slot.content, "权重 0 把槽位清空了 ⇒ 模型看不到这一节存在过"
    assert WEIGHT_ZERO_PLACEHOLDER in slot.content, (
        f"占位说明不对：{slot.content[:120]!r}"
    )
    assert any("按权重压缩" in line for line in log), log
