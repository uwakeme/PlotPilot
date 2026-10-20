"""A-05b：Bible 的语义归类由模型给，代码不再用关键词猜。

改造前
------
- ``_infer_character_importance``：含「主角」→ primary，含「导师/宿敌/反派」→
  secondary，其余 minor —— **代码在猜模型已经说过的信息**（``bible-characters``
  的 schema 里 ``role`` 就是「主角/对立角色/盟友/次要角色」）
- ``_map_location_kind``：把中文 type 映射到英文枚举。生产库实测：4 个
  「特殊场所」（周队办公室、陆深工位与隔断走廊、宋扬画像室、梦境镜厅 ——
  全是室内）被判成 ``realm``（秘境/领域）
- 同一逻辑**写了两份**（``auto_bible_generator`` 与 ``bible_location_triple_sync``），
  两份内容还不一样 ⇒ 同一地点在两条路径上分类可能不同

改造后
------
模型直接给 ``importance`` / ``location_type``；不在合法词表里 ⇒ **未评估**
（空串），不用关键词表补。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from application.world.services.bible_classification import (
    CHARACTER_IMPORTANCE_VALUES,
    LOCATION_IMPORTANCE_VALUES,
    LOCATION_TYPE_VALUES,
    normalize_character_importance,
    normalize_location_importance,
    normalize_location_type,
)

REPO_ROOT = Path(__file__).resolve().parents[4]


# ======================================================================
#  A. 归一化：读模型给的字段，不猜
# ======================================================================


def test_character_importance_is_not_guessed_from_role_keywords():
    """只有 role 没有 importance ⇒ **未评估**，不许按「主角」猜成 primary。"""
    assert normalize_character_importance(None) == ""
    assert normalize_character_importance("") == ""
    assert normalize_character_importance("主角") == "", "不能用关键词猜"


def test_location_type_is_not_guessed_from_chinese_keywords():
    """中文 type 不再被映射 —— 「特殊场所」曾被判成 realm（秘境），而它是室内。"""
    for raw in ("特殊场所", "城市", "建筑", "区域", "秘境", "势力"):
        assert normalize_location_type(raw) == "", f"不该猜中文：{raw!r}"


def test_model_given_values_are_kept():
    assert normalize_character_importance("primary") == "primary"
    assert normalize_character_importance("Secondary") == "secondary"
    assert normalize_location_type("building") == "building"
    assert normalize_location_importance("core") == "core"


def test_illegal_values_are_unassessed():
    assert normalize_character_importance("main") == ""
    assert normalize_location_type("城堡") == ""
    assert normalize_location_importance("very important") == ""


def test_vocabularies_match_the_frontend():
    """词表必须与前端 LOCATION_TYPE_META / CHARACTER_IMPORTANCE_META 对齐。"""
    assert set(CHARACTER_IMPORTANCE_VALUES) == {"primary", "secondary", "minor"}
    assert set(LOCATION_IMPORTANCE_VALUES) == {"core", "important", "normal"}
    assert set(LOCATION_TYPE_VALUES) == {"city", "region", "building", "faction", "realm"}


# ======================================================================
#  B. 代码里不再有关键词猜测 / 不再有两份实现
# ======================================================================


def _module_source(rel: str) -> str:
    return (REPO_ROOT / rel).read_bytes().decode("utf-8")


def test_keyword_classifiers_are_gone():
    """三张关键词表必须已删除。"""
    for rel in (
        "application/world/services/auto_bible_generator.py",
        "application/world/services/bible_location_triple_sync.py",
    ):
        src = _module_source(rel)
        for name in ("_infer_character_importance", "_map_location_kind"):
            assert f"def {name}(" not in src, f"{rel} 里还留着 {name}"


def test_location_type_has_exactly_one_implementation():
    """同一逻辑只留一份 —— 之前两份内容还不一样。"""
    found = []
    for path in (REPO_ROOT / "application").rglob("*.py"):
        try:
            tree = ast.parse(path.read_bytes().decode("utf-8"))
        except Exception:
            continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "normalize_location_type":
                found.append(path.relative_to(REPO_ROOT).as_posix())
    assert found == ["application/world/services/bible_classification.py"], found


# ======================================================================
#  C. 落库：模型的英文枚举优先，缺则中文原文（前端显示不变）
# ======================================================================


def _prepare(locations):
    from application.world.services.auto_bible_generator import AutoBibleGenerator

    svc = AutoBibleGenerator.__new__(AutoBibleGenerator)
    return svc._prepare_locations_for_save("n-1", locations)


def test_location_record_prefers_the_model_enum():
    prepared = _prepare(
        [
            {
                "id": "loc-1",
                "name": "档案馆",
                "description": "d",
                "type": "建筑",
                "location_type": "building",
                "connections": [],
                "parent_id": None,
            }
        ]
    )
    assert prepared[0]["location_type"] == "building"


def test_location_record_falls_back_to_the_model_chinese_type():
    """模型没给英文枚举 ⇒ 落中文原文（前端原样显示，行为不变）。"""
    prepared = _prepare(
        [
            {
                "id": "loc-1",
                "name": "档案馆",
                "description": "d",
                "type": "建筑",
                "connections": [],
                "parent_id": None,
            }
        ]
    )
    assert prepared[0]["location_type"] == "建筑"


# ======================================================================
#  D. prompt 包确实在问模型要这些字段
# ======================================================================


def test_prompt_packages_ask_the_model_for_the_classification():
    chars = _module_source(
        "infrastructure/ai/prompt_packages/nodes/bible-characters/user.md"
    )
    locs = _module_source(
        "infrastructure/ai/prompt_packages/nodes/bible-locations/user.md"
    )
    assert '"importance"' in chars, "bible-characters 没要求模型给 importance"
    assert '"location_type"' in locs, "bible-locations 没要求模型给 location_type"
    assert '"importance"' in locs, "bible-locations 没要求模型给 importance"