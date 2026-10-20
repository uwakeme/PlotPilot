# -*- coding: utf-8 -*-
"""D-03：核心伏笔不得遗忘 + 伏笔只有一个注入出口。

登记表 D-03 把「关键伏笔(emotional_weight>0.8)不得遗忘」列为
**代码保留的不变量** —— 这条不变量此前没有被任何代码守住：

1. ``ForeshadowingRegistry.get_t0_eligible_foreshadowings`` 直接按
   ``max_items`` 截断，核心伏笔与普通伏笔一起被挤出；
2. 被挤出的条目在「冗余伏笔参考」档（``get_deferred_foreshadowings``）
   里也进不去 —— 那一档只收 ``importance <= MEDIUM`` 且陈旧超窗的条目；
3. ⇒ 核心伏笔被**彻底遗忘**，既不在待回收伏笔档，也不在冗余参考档。

生产库实测（data/plotpilot.db，test-novel-6）：第 30 章有 3 条
emotional_weight > 0.8 的伏笔被挤出 T0（其中 1 条 CRITICAL / ew=0.95），
第 100 章后有 4 条被挤出；且该书 importance 分布为 LOW:0 / MEDIUM:0
⇒ 冗余参考档恒空 ⇒ 这些关键伏笔在上下文里彻底消失。

本文件守住两件事：
  A. 核心伏笔不会被 T0 截断挤出，且排在最前（token 截断保留头部）；
  B. 伏笔只有一个注入出口 —— ``engine/pipeline/base.py`` 里那段重复注入已删除，
     窗口口径统一到 +3。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from domain.novel.entities.foreshadowing_registry import ForeshadowingRegistry
from domain.novel.value_objects.foreshadowing import (
    CORE_EMOTIONAL_WEIGHT_THRESHOLD,
    Foreshadowing,
    ImportanceLevel,
    ForeshadowingStatus,
)
from domain.novel.value_objects.novel_id import NovelId

REPO_ROOT = Path(__file__).resolve().parents[4]


# ────────────────────────────── 夹具 ──────────────────────────────

def _make_registry(items):
    registry = ForeshadowingRegistry(id="fr-1", novel_id=NovelId("n-1"))
    for item in items:
        registry._foreshadowings.append(item)
    return registry


def _fs(fid, *, core=False, importance=ImportanceLevel.HIGH, planted=1, due=51):
    return Foreshadowing(
        id=fid,
        description=f"伏笔-{fid}",
        importance=importance,
        status=ForeshadowingStatus.PLANTED,
        planted_in_chapter=planted,
        suggested_resolve_chapter=due,
        emotional_weight=0.95 if core else 0.5,
    )


class _FakeRepo:
    def __init__(self, registry):
        self._registry = registry

    def get_by_novel_id(self, novel_id):
        return self._registry


# ───────────────── A. 核心伏笔不得遗忘 ─────────────────

def test_core_foreshadowing_survives_when_core_count_exceeds_quota():
    """核心伏笔数量超过 max_items 时，一条都不能被挤掉。

    场景按生产库复现：普通伏笔里有**已过期**的（排序在前：过期 > 即将到期），
    核心伏笔是「即将到期」（排序在后）⇒ 单纯按 max_items 截断会把核心伏笔
    整批切掉，且它们在「冗余伏笔参考」档里也进不去 ⇒ 彻底遗忘。
    """
    items = [_fs(f"plain-{i}", due=10) for i in range(9)]        # 已过期 ⇒ 排在前
    items += [_fs(f"core-{i}", core=True, due=51) for i in range(5)]  # 即将到期 ⇒ 排在后
    registry = _make_registry(items)

    selected = registry.get_t0_eligible_foreshadowings(current_chapter=50, max_items=8)

    selected_ids = {f.id for f in selected}
    for i in range(5):
        assert f"core-{i}" in selected_ids, "核心伏笔被 T0 截断挤掉了"


def test_core_foreshadowing_precedes_non_core():
    """核心伏笔必须排在最前 —— token 截断保留头部，只有排前面才活得下来。"""
    items = [_fs("plain-0"), _fs("core-0", core=True), _fs("plain-1")]
    registry = _make_registry(items)

    selected = registry.get_t0_eligible_foreshadowings(current_chapter=50, max_items=8)

    ids = [f.id for f in selected]
    assert ids.index("core-0") < ids.index("plain-0")
    assert ids.index("core-0") < ids.index("plain-1")


def test_core_foreshadowing_does_not_consume_non_core_quota():
    """核心伏笔**不占用**普通伏笔的名额。

    若让核心伏笔挤占名额，过期伏笔的强制约束会被一起挤掉 ——
    那是另一条不变量，不该被牺牲。
    """
    items = [_fs(f"core-{i}", core=True) for i in range(3)]
    items += [_fs(f"plain-{i}") for i in range(10)]
    registry = _make_registry(items)

    selected = registry.get_t0_eligible_foreshadowings(current_chapter=50, max_items=8)

    plain_count = sum(1 for f in selected if not f.is_core())
    assert plain_count == 8, f"普通伏笔名额被核心伏笔挤占了：实得 {plain_count}"


def test_overdue_non_core_survives_when_core_count_is_high():
    """已过期的普通伏笔必须仍在名单里（它的「已过期」行会生成强制约束）。"""
    items = [_fs(f"core-{i}", core=True) for i in range(4)]
    items.append(_fs("overdue-plain", due=10))  # current=50 ⇒ 已过期
    registry = _make_registry(items)

    selected = registry.get_t0_eligible_foreshadowings(current_chapter=50, max_items=8)

    assert "overdue-plain" in {f.id for f in selected}


def test_core_threshold_boundary():
    """阈值边界：0.8 不算核心，0.81 算。"""
    assert CORE_EMOTIONAL_WEIGHT_THRESHOLD == 0.8
    base = dict(
        id="f", description="x", importance=ImportanceLevel.HIGH,
        status=ForeshadowingStatus.PLANTED, planted_in_chapter=1,
    )
    assert Foreshadowing(emotional_weight=0.8, **base).is_core() is False
    assert Foreshadowing(emotional_weight=0.81, **base).is_core() is True
    assert Foreshadowing(**base).is_core() is False  # 默认 0.5


def test_allocator_slot_keeps_every_core_foreshadowing():
    """端到端到槽位：allocator 渲染出的内容里必须含全部核心伏笔。

    这条专门堵「allocator 里还有一层 [:MAX_T0_FORESHADOWING_ITEMS] 二次截断」
    —— 核心伏笔排在最前，二次截断会把它们切掉，等于把刚守住的不变量又推翻。
    """
    from application.engine.services.context_budget_allocator import ContextBudgetAllocator

    items = [_fs(f"core-{i}", core=True) for i in range(12)]
    items += [_fs(f"plain-{i}") for i in range(3)]
    allocator = ContextBudgetAllocator(
        foreshadowing_repository=_FakeRepo(_make_registry(items))
    )

    content = allocator._get_pending_foreshadowings("n-1", 50)

    for i in range(12):
        assert f"伏笔-core-{i}" in content, f"核心伏笔 core-{i} 没进上下文"


# ───────────────── B. 伏笔只有一个注入出口 ─────────────────

def _parse(rel):
    return ast.parse((REPO_ROOT / rel).read_bytes().decode("utf-8"))


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _attrs(node):
    return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


def test_base_pipeline_has_no_second_foreshadowing_injection():
    """engine/pipeline/base.py 的 _step_build_context 不得再读伏笔账本。

    用 AST 判定而不是文本匹配 —— 注释里为了说明历史必然提到这些名字，
    文本匹配会把说明当成代码。
    """
    tree = _parse("engine/pipeline/base.py")
    func = _find_func(tree, "_step_build_context")
    assert func is not None, "找不到 _step_build_context"

    used = _attrs(func)
    assert "subtext_entries" not in used, "base.py 仍在读 subtext_entries（重复注入路径）"
    assert "foreshadowing_repository" not in used, "base.py 仍在读 foreshadowing_repository"


def test_base_pipeline_has_no_suggested_resolve_window():
    """base.py 里那个 +2 窗口必须彻底消失（窗口口径统一为 +3）。"""
    tree = _parse("engine/pipeline/base.py")
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "suggested_resolve_chapter" not in attrs, "base.py 还在按 suggested_resolve_chapter 取窗口"


def _window_add_constants(tree):
    """所有「拿 suggested_resolve_chapter 与 X+N 比较」里的 N。"""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if "suggested_resolve_chapter" not in _attrs(node):
            continue
        for binop in ast.walk(node):
            if isinstance(binop, ast.BinOp) and isinstance(binop.op, ast.Add):
                for const in ast.walk(binop):
                    if isinstance(const, ast.Constant) and isinstance(const.value, int):
                        found.add(const.value)
    return found


@pytest.mark.parametrize(
    "rel",
    [
        "application/engine/services/context_budget_allocator.py",
        "domain/novel/entities/foreshadowing_registry.py",
    ],
)
def test_foreshadowing_window_is_plus_three_not_two(rel):
    """窗口口径必须是 +3，不得再有 +2。"""
    found = _window_add_constants(_parse(rel))
    assert 2 not in found, f"{rel}: 伏笔窗口仍有 +2（口径不一致）：{found}"


def test_registry_upcoming_window_default_is_three():
    """领域模型的「即将到期」窗口默认值必须是 3。"""
    tree = _parse("domain/novel/entities/foreshadowing_registry.py")
    for name in ("get_upcoming_foreshadowings", "get_upcoming_subtext_entries"):
        func = _find_func(tree, name)
        assert func is not None, f"找不到 {name}"
        pairs = list(
            zip(func.args.args[-len(func.args.defaults):], func.args.defaults)
        )
        defaults = {arg.arg: default for arg, default in pairs}
        window = defaults.get("window")
        assert isinstance(window, ast.Constant) and window.value == 3, (
            f"{name} 的 window 默认值不是 3"
        )


def _mentions_emotional_weight(node):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "emotional_weight":
            return True
        if isinstance(sub, ast.Subscript):
            for c in ast.walk(sub.slice):
                if isinstance(c, ast.Constant) and c.value == "emotional_weight":
                    return True
    return False


@pytest.mark.parametrize(
    "rel",
    [
        "engine/runtime/quality_guardrails/viewpoint_guardrail.py",
        "engine/runtime/plot_state_machine/state_machine.py",
        "engine/core/entities/foreshadow.py",
    ],
)
def test_core_threshold_has_single_source(rel):
    """核心伏笔阈值必须只有一个真源，不许再散落硬编码 0.8。"""
    tree = _parse(rel)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not _mentions_emotional_weight(node):
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and comparator.value == 0.8:
                pytest.fail(f"{rel}: emotional_weight 又跟硬编码 0.8 比了（应走领域常量）")