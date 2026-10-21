"""F-08：剧情点标签与描述交回 AI（阈值表 + 代写描述不该留在代码里）。

红线（本文件负责钉死）：

1. **阈值表与代写描述必须消失** —— 那是「创作判断被写成了常量」的残留。
2. **没有判定 ⇒ 不生成剧情点** —— 不拿阈值表兜底（兜底等于代码又替 AI 贴了标签）。
3. **标签枚举封闭** —— AI 给了枚举外的标签 ⇒ 未评估，**不回落成 rising**。
4. **描述由 AI 写** —— 代码不写「高潮：张力从 40 跃升至 85」。
5. **「不是剧情点」与「未评估」可分辨**（前者是判定，后者是没判）。
6. **张力权重的死实现已删** —— 活的口径只有 `TensionDimensions` 一份。
"""
from __future__ import annotations

import ast
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.world.services.plot_point_classifier import PlotPointClassifier
from application.world.services.plot_point_classifier_schema import (
    PLOT_POINT_TYPES,
    SOURCE_AI,
    SOURCE_NONE,
    UNASSESSED,
    normalize_verdict,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
SYNC_PATH = REPO_ROOT / "application/world/services/chapter_narrative_sync.py"


def _plot_arc():
    from domain.novel.entities.plot_arc import PlotArc
    from domain.novel.value_objects.novel_id import NovelId

    return PlotArc(
        id="arc-1", novel_id=NovelId("n1"), slug="default", display_name="主情节弧"
    )


class _FakePlotArcRepo:
    def __init__(self, arc=None):
        self.arc = arc
        self.saved = []

    def get_by_novel_id(self, novel_id):
        return self.arc

    def save(self, arc):
        self.saved.append(arc)


# ── 1. 阈值表与代写描述已删除 ───────────────────────────────────────


def _code_string_literals(tree):
    """收集**代码里**的字符串字面量（不含 docstring）。

    docstring 里会引用旧文案（说明「旧实现是这么写的」），那不算残留；
    但代码里的 f-string 片段（例如 ``"持续低张："``）算。
    """
    docstring_ids = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstring_ids.add(id(first.value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_ids
    ]


def test_legacy_threshold_descriptions_are_gone():
    """AST 扫代码里的字符串字面量 —— 代写描述的模板文字必须不在代码里。

    判据是**子串包含**：旧文案写在 f-string 里，ast 拆出来的片段带标点
    （``"持续低张："`` / ``"（需注入冲突）"``），精确相等匹配抓不到 ——
    那样这条守卫会一直是绿的，却从没真判过任何东西。
    """
    tree = ast.parse(SYNC_PATH.read_bytes().decode("utf-8"))
    literals = _code_string_literals(tree)
    for phrase in ("需注入冲突", "跃升至", "持续低张", "张力达到", "上升至", "提升至"):
        assert not any(phrase in lit for lit in literals), (
            f"旧的代写描述还在代码里：{phrase}"
        )


# ── 2. 判定口径：三态 + 枚举封闭 ────────────────────────────────────


def test_unassessed_is_not_a_verdict():
    assert UNASSESSED.source == SOURCE_NONE
    assert UNASSESSED.is_usable is False
    assert UNASSESSED.is_plot_point is False


def test_not_a_plot_point_is_still_a_verdict():
    """AI 明确说「不是剧情点」—— 这是判定，与「未评估」可分辨。"""
    verdict = normalize_verdict({"is_plot_point": False, "point_type": "", "description": ""})

    assert verdict.source == SOURCE_AI
    assert verdict.is_usable is True
    assert verdict.is_plot_point is False


def test_illegal_point_type_is_unassessed_not_falling_back_to_rising():
    """枚举封闭：非法标签 ⇒ 未评估（不回落成 rising）。"""
    verdict = normalize_verdict({
        "is_plot_point": True, "point_type": "explosion", "description": "炸了",
    })

    assert verdict.source == SOURCE_NONE
    assert verdict.is_usable is False


def test_missing_description_is_unassessed():
    verdict = normalize_verdict({"is_plot_point": True, "point_type": "climax", "description": ""})

    assert verdict.source == SOURCE_NONE


def test_non_bool_is_plot_point_is_unassessed():
    assert normalize_verdict({"is_plot_point": "yes"}).source == SOURCE_NONE
    assert normalize_verdict(None).source == SOURCE_NONE


def test_plot_point_types_match_the_domain_enum():
    """封闭枚举必须与领域枚举一致（否则写库会炸）。"""
    from domain.novel.value_objects.plot_point import PlotPointType

    assert set(PLOT_POINT_TYPES) == {t.value for t in PlotPointType}


# ── 3. 调用链：失败即未评估 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_classifier_returns_unassessed_when_the_llm_fails():
    class _Boom:
        async def generate(self, prompt, config=None):
            raise RuntimeError("endpoint down")

    verdict = await PlotPointClassifier(_Boom()).classify(
        chapter_number=3, content="正文", tension_score=70.0, prev_tension_score=50.0,
    )

    assert verdict.source == SOURCE_NONE


@pytest.mark.asyncio
async def test_classifier_passes_the_evidence_to_the_model():
    captured = {}

    class _LLM:
        async def generate(self, prompt, config=None):
            captured["prompt"] = prompt
            return SimpleNamespace(content=json.dumps({
                "is_plot_point": True, "point_type": "climax", "description": "沈砚当堂翻案",
            }))

    verdict = await PlotPointClassifier(_LLM()).classify(
        chapter_number=3,
        chapter_summary="沈砚当堂翻案",
        content="沈砚把卷宗摔在案上",
        tension_score=85.0,
        prev_tension_score=40.0,
        novel_title="临渊",
    )

    assert verdict.is_plot_point is True
    assert verdict.point_type == "climax"
    assert verdict.description == "沈砚当堂翻案"
    text = captured["prompt"].user
    assert "沈砚当堂翻案" in text
    assert "85" in text and "40" in text


# ── 4. 落库：没有判定就不生成；有判定就用 AI 的标签与描述 ───────────


def test_no_verdict_means_no_plot_point():
    """未评估 ⇒ 不生成剧情点（不用阈值表兜底）。"""
    from application.world.services.chapter_narrative_sync import _auto_generate_plot_point

    repo = _FakePlotArcRepo(_plot_arc())

    _auto_generate_plot_point("n1", 3, 85.0, repo, verdict=None)
    _auto_generate_plot_point("n1", 3, 85.0, repo, verdict=UNASSESSED)

    assert repo.saved == []


def test_verdict_saying_not_a_plot_point_writes_nothing():
    from application.world.services.chapter_narrative_sync import _auto_generate_plot_point

    repo = _FakePlotArcRepo(_plot_arc())
    verdict = normalize_verdict({"is_plot_point": False, "point_type": "", "description": ""})

    _auto_generate_plot_point("n1", 3, 85.0, repo, verdict=verdict)

    assert repo.saved == []


def test_ai_label_and_description_are_what_get_written():
    from application.world.services.chapter_narrative_sync import _auto_generate_plot_point

    repo = _FakePlotArcRepo(_plot_arc())
    verdict = normalize_verdict({
        "is_plot_point": True, "point_type": "climax", "description": "沈砚当堂翻案",
    })

    _auto_generate_plot_point("n1", 3, 85.0, repo, verdict=verdict)

    assert len(repo.saved) == 1
    point = repo.saved[0].key_points[-1]
    assert point.point_type.value == "climax"
    assert point.description == "沈砚当堂翻案"
    # 代码不写「高潮：张力从 40 跃升至 85」这种代写文案
    assert "跃升" not in point.description


def test_unevaluated_tension_still_blocks_the_write():
    from application.world.services.chapter_narrative_sync import _auto_generate_plot_point
    from domain.novel.value_objects.tension_dimensions import UNEVALUATED

    repo = _FakePlotArcRepo(_plot_arc())
    verdict = normalize_verdict({
        "is_plot_point": True, "point_type": "climax", "description": "沈砚当堂翻案",
    })

    _auto_generate_plot_point("n1", 3, UNEVALUATED, repo, verdict=verdict)

    assert repo.saved == []


# ── 5. 张力权重：死实现已删，活的口径只有一份 ─────────────────────


def test_chapter_entity_no_longer_carries_a_second_weight_formula():
    """``Chapter.update_tension`` 零调用，且权重口径与 TensionDimensions 分裂。"""
    from engine.core.entities.chapter import Chapter

    assert not hasattr(Chapter, "update_tension")


def test_the_live_weight_formula_is_the_domain_one():
    from domain.novel.value_objects.tension_dimensions import TensionDimensions

    dims = TensionDimensions.from_raw_scores(plot=100.0, emotional=0.0, pacing=0.0)

    assert dims.composite_score == 40.0
