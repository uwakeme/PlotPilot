"""A-06（第 56 批）：「五维键集 / 中文标签」只有**契约**一个来源。

背景
----
契约（``shared/taxonomy/worldbuilding_contract_cn_v1.yaml``）里**已经有** 5 个维度的
键与中文 label ⇒ 但此前全仓有 **4 处手写副本**：

- ``application/ai_invocation/variable_projection.py`` —— 5 维 → 中文标签 的整张字典；
- ``application/blueprint/services/setup_context_builder.py``（``_worldview_summary``）—— 同上；
- ``application/blueprint/services/setup_context_builder.py``（``_load_variable_context``）
  —— ``worldbuilding.<dim>`` → 局部名 的键集表；
- ``application/world/worldbuilding_merge.py`` —— 5 维的 raw dict。

⇒ 契约一改（改名 / 加维度），副本**不会跟着改** ⇒ 静默漂。

⚠ 守卫的判据要**分清**「五维映射表」与「顺带提到五维的载荷」
（载荷 / 模板变量 / 实体自身形状的键集合**远大于**五维 ⇒ 不算副本）。

本文件钉住
----------
1. ``worldbuilding_dimension_labels()`` 与契约**逐项、逐序**一致；
2. **通用守卫**：全仓**不许**再出现「五维映射表」字面量
   （键集合**恰好**是五维 ⇒ 允许名单两处并写明理由）；
3. 行为守卫：两个消费点的输出标签 == 契约标签（逐维、顺序一致）；
4. ``worldbuilding_entity_to_slices`` 的键集合 == 契约维度键集合；
5. **domain 实体的五维** == 契约维度键（domain 与契约不许漂）；
6. **字段展示顺序**里每维的字段集合 == 契约该维的字段集合（不许漂）。
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from application.world.worldbuilding_contract import (
    get_worldbuilding_contract,
    worldbuilding_dimension_keys,
    worldbuilding_dimension_labels,
)

REPO_ROOT = Path(__file__).resolve().parents[4]

#: 这些目录里**不许**再出现手写的五维映射表
SCAN_DIRS = ("application", "domain", "engine", "infrastructure", "interfaces")

#: 允许保留「五维映射表字面量」的地方（**每处都要写明理由**）
ALLOWED_SITES = {
    # domain 实体的**自身形状**：Worldbuilding 这个领域对象本来就有这五个维度
    #   ⇒ 它不是「契约的副本」，而是契约的**对端** ⇒ 两者不许漂（见守卫 5）。
    "domain/worldbuilding/worldbuilding.py": "domain 实体的自身形状（与契约互锁）",
    # Mock provider 的固定回包：测试替身，不参与生产口径
    "infrastructure/ai/providers/mock_provider.py": "测试替身的固定回包",
    # 字段**展示顺序**：它比契约多一层信息（按硬度重排，不是契约的字段顺序）
    #   ⇒ 允许留字面量，但字段集合必须与契约一致（见守卫 6）。
    "application/world/services/narrative_contract_text.py": "字段展示顺序（多一层「按硬度排序」信息，由守卫 6 交叉校验）",
}


def _contract_labels() -> Dict[str, str]:
    contract = get_worldbuilding_contract()
    return {
        dim: str(cfg.get("label") or dim).strip() or dim
        for dim, cfg in contract.dimensions.items()
    }


def _tracked_py_files() -> List[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"], capture_output=True, text=True, encoding="utf-8"
    ).stdout
    return [REPO_ROOT / p for p in out.splitlines() if p]


# ─────────────────────── 1) 访问器与契约一致 ───────────────────────


def test_the_dimension_labels_accessor_matches_the_contract():
    """访问器返回的标签必须与契约**逐项、逐序**一致。"""
    expected = _contract_labels()

    assert worldbuilding_dimension_labels() == expected
    assert list(worldbuilding_dimension_labels().keys()) == list(expected.keys())


# ─────────────────────── 2) 通用：不许再手写五维映射表 ───────────────────────


def test_no_module_hand_writes_the_five_dimension_map():
    """⚠ 全仓**不许**再出现手写的五维映射表。

    判据（形状）：一个**字典字面量**，其键集合**恰好**等于契约的五个维度键
    ⇒ 那就是「五维映射表」的手写副本。
    ⚠ 载荷字典 / 模板变量字典 / 实体 to_dict 的键集合**远大于**五维 ⇒ 不算。
    """
    dim_keys = set(worldbuilding_dimension_keys())
    offenders: List[str] = []

    for path in _tracked_py_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.split("/")[0] not in SCAN_DIRS:
            continue
        if "tests" in rel.split("/"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {
                k.value
                for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            if keys == dim_keys and rel not in ALLOWED_SITES:
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        "这些地方又手写了五维映射表（契约一改就会漂）: %s；"
        "请改用 worldbuilding_dimension_labels() / worldbuilding_dimension_keys()" % offenders
    )


# ─────────────────────── 3) 行为守卫：两个消费点 ───────────────────────


def test_the_projection_renders_the_contract_labels():
    """``variable_projection`` 的世界观块必须用契约标签（逐维、顺序一致）。"""
    from application.ai_invocation.variable_projection import _worldbuilding_context

    labels = _contract_labels()
    payload: Dict[str, Any] = {dim: f"内容-{dim}" for dim in labels}

    text = _worldbuilding_context(payload)

    positions = []
    for dim, label in labels.items():
        assert f"【{label}】" in text, f"维度 {dim} 的标题不是契约标签 {label!r}"
        positions.append(text.index(f"【{label}】"))
    assert positions == sorted(positions), "维度的输出顺序与契约不一致"


def test_the_setup_context_summary_uses_the_contract_labels():
    """``setup_context_builder._worldview_summary`` 必须用契约标签。"""
    from application.blueprint.services.setup_context_builder import (
        SetupContextBuilder,
    )

    labels = _contract_labels()
    payload: Dict[str, Any] = {dim: {"f": "v"} for dim in labels}

    lines = SetupContextBuilder._worldview_summary(payload)

    assert lines, "摘要为空 ⇒ 样本失效"
    for dim, label in labels.items():
        assert any(line.startswith(f"{label}:") for line in lines), (
            f"摘要里没有维度 {dim} 的契约标签 {label!r}"
        )


# ─────────────────────── 4) 切片键集合 == 契约维度 ───────────────────────


def test_the_entity_slices_use_the_contract_dimensions():
    """``worldbuilding_entity_to_slices`` 的键集合必须等于契约维度键集合。"""
    from application.world.worldbuilding_merge import (
        WORLD_BUILDING_FIELD_KEYS_BY_DIMENSION,
        worldbuilding_entity_to_slices,
    )

    class _FakeWB:
        def __init__(self):
            for dim in worldbuilding_dimension_keys():
                field = (WORLD_BUILDING_FIELD_KEYS_BY_DIMENSION.get(dim) or ("x",))[0]
                setattr(self, dim, {field: "值"})

    slices = worldbuilding_entity_to_slices(_FakeWB())

    assert set(slices.keys()) == set(worldbuilding_dimension_keys())
    for dim, fields in slices.items():
        assert fields, f"维度 {dim} 的切片为空 ⇒ 投影丢了内容"


# ─────────────────────── 5) domain 实体 ↔ 契约不许漂 ───────────────────────


def test_the_domain_entity_dimensions_match_the_contract():
    """domain 实体的五维必须与契约的维度键一致（互锁）。

    ⚠ 量的是 **`normalized_dimensions()` 返回的键**（消费方真正拿到的东西），
    顺带查实体的属性集 —— 只查属性集会漏掉「方法多吐了一个假维度」。
    """
    from domain.worldbuilding.worldbuilding import Worldbuilding

    class _Stub:
        core_rules = {}
        geography = {}
        society = {}
        culture = {}
        daily_life = {}

    dims = Worldbuilding.normalized_dimensions(_Stub())
    assert set(dims.keys()) == set(worldbuilding_dimension_keys()), (
        f"实体 normalized_dimensions 的维度 {sorted(dims)} "
        f"与契约 {sorted(worldbuilding_dimension_keys())} 不一致"
    )

    entity_dims = {
        name
        for name in dir(Worldbuilding)
        if not name.startswith("_")
        and isinstance(getattr(Worldbuilding, name, None), property)
        and name in set(worldbuilding_dimension_keys())
    }
    assert entity_dims == set(worldbuilding_dimension_keys()), (
        f"domain 实体的维度属性 {sorted(entity_dims)} 与契约不一致"
    )


# ─────────────────────── 6) 字段展示顺序 ↔ 契约不许漂 ───────────────────────


def test_the_field_display_order_matches_the_contract_fields():
    """字段展示顺序里每维的字段**集合**必须 == 契约该维的字段集合（不许漂）。

    ⚠ 比**集合**不比顺序：展示顺序**故意**按硬度重排（禁忌在前），
    与契约里的字段顺序不同 ⇒ 那正是它多出来的那一层信息。
    """
    from application.world.services.narrative_contract_text import (
        WORLDBUILDING_FIELD_DISPLAY_ORDER,
    )

    contract = get_worldbuilding_contract()
    for dim, fields in WORLDBUILDING_FIELD_DISPLAY_ORDER.items():
        expected = tuple((contract.dimensions.get(dim) or {}).get("fields", {}).keys())
        assert set(fields) == set(expected), (
            f"维度 {dim} 的展示字段 {fields} 与契约字段 {expected} 不是同一个集合"
        )
