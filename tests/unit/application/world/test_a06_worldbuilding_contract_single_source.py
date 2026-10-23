"""A-06：世界观字段契约必须是**唯一真相**。

背景
----
契约的 SSOT 是 ``shared/taxonomy/worldbuilding_contract_cn_v1.yaml``，
由 ``application/world/worldbuilding_contract.py`` 读取。

但全仓曾有 **7 处**手写副本（字段名/维度键集/中文标签）：

1. ``application/ai_invocation/variable_hub.py`` —— 维度键集
2. ``application/ai_invocation/variable_backfill.py`` —— 维度键集 + 中文标签表
3. ``application/engine/dag/nodes/world_nodes.py`` —— 同一个元组**写了两遍**
4. ``application/world/services/bible_setup_continuation.py`` —— 维度键集
5. ``interfaces/api/v1/world/worldbuilding_routes.py`` —— 5 个 DTO 硬编码 16 个字段名
6. ``frontend/src/domain/worldbuilding/contract.ts`` —— 16 个字段短标签
7. ``frontend/src/onboarding/bibleSetupModel.ts`` —— 第 2 份五维数组

⇒ **没有任何测试 import 过那几个常量** ⇒ 契约一改、副本不会跟着改
⇒ 「同一逻辑写两份」在本仓反复出现的那一类。

本笔的处置（**分层**，不是一律动态化）
--------------------------------------
**消除纯数据副本**（1/2/3/4/6/7）—— 它们只是数据，动态化零风险：

- 1~4 改从 ``worldbuilding_contract`` 读；
- 6 的 16 个短标签**下沉到契约 YAML**（``field_labels`` 段），前端从 bundle 读；
- 7 的五维数组改为从 bundle 派生。

**保留形状声明但用守卫钉住**（5 与前端表单）——
它们是 **API 契约 / 表单形状**，动态化会丢 IDE 补全、且前端零测试设施 ⇒
改成「显式声明 + 守卫断言与契约一致」⇒ 契约一改而它们没改，**守卫会红**。

守卫要点
--------
- 每处「仍保留的形状声明」都必须与契约**逐字段一致**；
- 被消除的副本**不许**再出现手写字面量；
- 前端是**文件内容级**断言（前端零测试设施，局限写在文件头）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from application.world.worldbuilding_contract import (
    get_worldbuilding_contract,
    worldbuilding_dimension_keys,
)

ROOT = Path(__file__).resolve().parents[4]

CONTRACT_YAML = ROOT / "shared/taxonomy/worldbuilding_contract_cn_v1.yaml"
FRONTEND_PANEL = ROOT / "frontend/src/components/workbench/WorldbuildingPanel.vue"
FRONTEND_CONTRACT = ROOT / "frontend/src/domain/worldbuilding/contract.ts"
FRONTEND_MODEL = ROOT / "frontend/src/onboarding/bibleSetupModel.ts"
ROUTES = ROOT / "interfaces/api/v1/world/worldbuilding_routes.py"


def _contract_fields() -> dict[str, list[str]]:
    contract = get_worldbuilding_contract()
    return {
        dim: list((cfg.get("fields") or {}).keys())
        for dim, cfg in contract.dimensions.items()
    }


def _strip_comments(source: str) -> str:
    text = re.sub(r'"""(?:.|\n)*?"""', "", source)
    text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"#[^\n]*", "", text)
    return text


# ── 1) 契约本身是自洽的 ────────────────────────────────────────────

def test_contract_has_five_dimensions_and_sixteen_fields():
    fields = _contract_fields()
    assert len(fields) == 5, f"维度数变了：{list(fields)}"
    assert sum(len(v) for v in fields.values()) == 16, (
        f"字段总数变了：{sum(len(v) for v in fields.values())}"
    )


def test_contract_yaml_declares_the_field_labels():
    """A-06：16 个字段短标签已**下沉到契约**（此前只在前端手写）。"""
    contract = get_worldbuilding_contract()
    labels = contract.field_labels

    fields = _contract_fields()
    for dim, keys in fields.items():
        for key in keys:
            assert key in labels, f"{dim}.{key} 没有短标签"
            assert labels[key].strip(), f"{dim}.{key} 的短标签是空的"


# ── 2) Python 侧：副本已消除 ────────────────────────────────────────

def test_python_copies_are_gone():
    """4 处 Python 副本必须**已从契约读**（不再手写元组）。"""
    for rel in (
        "application/ai_invocation/variable_hub.py",
        "application/ai_invocation/variable_backfill.py",
        "application/engine/dag/nodes/world_nodes.py",
        "application/world/services/bible_setup_continuation.py",
    ):
        code = _strip_comments((ROOT / rel).read_text(encoding="utf-8"))
        assert '("core_rules", "geography", "society", "culture", "daily_life")' not in code, (
            f"{rel} 里还有手写的五维元组"
        )
        assert "['core_rules', 'geography', 'society', 'culture', 'daily_life']" not in code, (
            f"{rel} 里还有手写的五维列表"
        )


def test_python_dimension_keys_equal_the_contract():
    """各处读出来的维度键集必须**等于**契约。"""
    from application.ai_invocation.variable_hub import WORLD_BUILDING_DIMENSION_KEYS
    from application.world.services.bible_setup_continuation import (
        WORLDBUILDING_DIMENSION_KEYS,
    )

    expected = worldbuilding_dimension_keys()
    assert tuple(WORLD_BUILDING_DIMENSION_KEYS) == expected
    assert tuple(WORLDBUILDING_DIMENSION_KEYS) == expected


def test_backfill_labels_come_from_the_contract():
    """`variable_backfill` 的中文标签必须来自契约（此前是另一份手写表）。"""
    from application.ai_invocation import variable_backfill as module

    code = _strip_comments(
        (ROOT / "application/ai_invocation/variable_backfill.py").read_text(encoding="utf-8")
    )
    assert "核心法则" not in code, "维度中文标签还手写在这里"
    assert "worldbuilding_dimension_label" in code, "没有用契约的维度标签"


# ── 3) API DTO：保留显式声明，但必须与契约逐字段一致 ─────────────────

def test_api_dto_fields_match_the_contract():
    """5 个 DTO 的字段必须与契约**逐字段一致**。

    ⚠ DTO 是 **API 契约** ⇒ 保留显式声明（动态 pydantic 会丢 IDE 补全、
    也会改契约形状）⇒ 改为用守卫**钉住**：契约一改而 DTO 没改，这里就红。
    """
    contract_fields = _contract_fields()
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))

    dto_by_dim = {
        "core_rules": "CoreRulesDTO",
        "geography": "GeographyDTO",
        "society": "SocietyDTO",
        "culture": "CultureDTO",
        "daily_life": "DailyLifeDTO",
    }
    found: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name not in dto_by_dim.values():
            continue
        found[node.name] = [
            stmt.target.id
            for stmt in node.body
            if isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
        ]

    for dim, dto_name in dto_by_dim.items():
        assert dto_name in found, f"找不到 {dto_name}"
        assert found[dto_name] == contract_fields[dim], (
            f"{dto_name} 的字段与契约不一致：\n"
            f"  DTO   : {found[dto_name]}\n"
            f"  契约  : {contract_fields[dim]}"
        )


# ── 4) 前端：文件内容级断言 ────────────────────────────────────────

def test_frontend_short_labels_come_from_the_bundle():
    """前端不再手写 16 个短标签（改为从 bundle 读）。"""
    source = _strip_comments(FRONTEND_CONTRACT.read_text(encoding="utf-8"))

    assert "FIELD_SHORT_LABELS" not in source, "前端还在手写字段短标签"
    assert "contract.field_labels" in source, "前端没有从 bundle 读短标签"


def test_frontend_dimension_keys_come_from_the_bundle():
    """`bibleSetupModel` 的五维数组改为从 bundle 派生。"""
    source = _strip_comments(FRONTEND_MODEL.read_text(encoding="utf-8"))

    assert (
        "['core_rules', 'geography', 'society', 'culture', 'daily_life']" not in source
    ), "前端还有手写的五维数组"
    assert "getWorldbuildingDimensionKeys()" in source, "前端没有从 bundle 派生维度键"


def _strip_vue_comments(source: str) -> str:
    """剥掉 Vue 模板里的 HTML 注释（``<!-- ... -->``）。

    ⚠ 复审 S4：此前前端那条守卫直接对**原始未剥注释**的文件做正则
    ⇒ 「把绑定注释掉」这种逃逸**照样绿**（注释里还留着 ``v-model``）。
    """
    return re.sub(r"<!--(?:.|\n)*?-->", "", source)


def test_frontend_panel_field_bindings_match_the_contract():
    """表单模板里绑定的字段必须与契约**逐字段一致**。

    ⚠ 这是**文件内容级**断言（前端零测试设施，只有 ``npm run build`` 门禁）：
    它挡得住「契约加了字段而表单没加」，挡不住「字段顺序变了」。

    ⚠ 复审 S4：必须**先剥 HTML 注释**再抽绑定 ——
    否则「把绑定注释掉」这种逃逸照样绿。
    """
    source = _strip_vue_comments(FRONTEND_PANEL.read_text(encoding="utf-8"))
    bound = re.findall(r'v-model:value="formData\.(\w+)\.(\w+)"', source)

    contract_fields = _contract_fields()
    expected = [
        (dim, key) for dim, keys in contract_fields.items() for key in keys
    ]
    assert sorted(bound) == sorted(expected), (
        "表单绑定的字段与契约不一致：\n"
        f"  表单: {sorted(bound)}\n"
        f"  契约: {sorted(expected)}"
    )


def test_the_frontend_panel_has_no_dead_bindings():
    """⚠ 复审 S4：另一条逃逸是「把绑定包进 ``v-if="false"``」。

    那种情况下绑定**文本还在** ⇒ 上一条照样绿 ⇒ 表单实际不渲染这些字段
    ⇒ 契约与表单**形状**对不上，却没有任何断言能发现。
    ⇒ 直接禁止这种写法（当前该文件里一处都没有）。
    """
    source = FRONTEND_PANEL.read_text(encoding="utf-8")

    assert 'v-if="false"' not in source, "表单里有被 v-if=\"false\" 关掉的绑定"
    assert 'v-show="false"' not in source, "表单里有被 v-show=\"false\" 关掉的绑定"


def test_frontend_panel_dimension_count_is_not_hardcoded():
    """完成度分母不许写死 5（契约加一维就会算错）。"""
    source = _strip_comments(FRONTEND_PANEL.read_text(encoding="utf-8"))

    assert "/ 5) * 100" not in source, "完成度分母还写死 5"
    assert "SECTION_KEYS.length" in source, "完成度分母没有用维度数"
