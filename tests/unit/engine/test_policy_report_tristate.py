"""护栏降级路径不得返回「看起来正常」的分数。

背景（登记表 F-03 / F-09）：
- policy_validator.py 在 QualityGuardrail.check 抛异常时返回 overall_score=0.85 + passed=True；
- quality_guardrail.py 在缺 character_masks / character_names / scene_info 时返回
  常量分 0.73 / 0.82 / 0.77，而 MIN_PASS_SCORE=0.6，于是「什么都没评」照样通过。
两者都让下游无法区分「检查通过」与「根本没跑成」。

本文件的语义（逐维诚实 + 显式覆盖率）：
- 缺输入的维度记 None 且**不参与加权平均**，而不是写死一个"看起来正常"的常量；
- 仍可评估的维度照常评分，overall 按剩余权重归一化；
- 一个维度都评不出来时整体 UNASSESSED，overall_score 为 None；
- 部分可评估时 state 仍是 DECIDED，但 unevaluated_dimensions 必须显式列出缺口，
  下游靠它判断置信度，不得把部分覆盖伪装成完整覆盖。
"""
import pytest

from domain.ai.value_objects.assessment_state import AssessmentState
from engine.runtime.policy_validator import (
    PolicyReport,
    PolicyUnavailableError,
    PolicyValidator,
)
from engine.runtime.quality_guardrails.quality_guardrail import (
    GuardrailUnavailableError,
    QualityGuardrail,
)


def _mask(name="林渊"):
    return {
        name: type("M", (), {
            "name": name, "core_belief": "绝不信任他人", "voice_style": "惜字如金",
            "sentence_pattern": "陈述", "moral_taboos": [], "active_wounds": [],
        })()
    }


SCENE = {"era": "ancient", "scene_type": "combat", "viewpoint_character": "林渊"}

# 旧常量分：F-09 修掉的就是这三个值，缺输入时不得再出现
LEGACY_CONSTANTS = (0.73, 0.82, 0.77)


def test_report_defaults_to_unassessed_not_passed():
    r = PolicyReport()
    # 默认不得声称通过
    assert r.state is AssessmentState.UNASSESSED
    assert r.passed is None


def test_validator_degrades_to_unassessed_when_guardrail_raises(monkeypatch):
    v = PolicyValidator()

    def boom(*a, **k):
        raise RuntimeError("护栏炸了")

    monkeypatch.setattr(v, "_guardrail", type("G", (), {"check": boom})())

    report = v.advise(text="正文", character_masks=None)

    assert report.state is AssessmentState.UNASSESSED
    # 关键：不得再出现 0.85 这个「假通过分」
    assert report.overall_score is None
    assert report.passed is None


def test_missing_inputs_yield_none_scores_not_constants():
    """缺三项输入时，那三维是 None 而不是 0.73 / 0.82 / 0.77。

    其余四维（语言风格/情节密度/节奏/宏观节奏）只依赖正文，照常评分，
    overall 因此仍可计算——但只由真正评估过的维度加权得出。
    """
    g = QualityGuardrail()
    report = g.check(text="正文", character_masks=None, character_names=None, scene_info=None)

    assert report.character_consistency_score is None
    assert report.naming_score is None
    assert report.viewpoint_score is None

    for constant in LEGACY_CONSTANTS:
        for score in (
            report.character_consistency_score,
            report.naming_score,
            report.viewpoint_score,
            report.overall_score,
        ):
            assert score != pytest.approx(constant), f"仍在使用常量分 {constant}"

    assert report.unevaluated_dimensions == [
        "character_consistency", "naming", "viewpoint",
    ]
    # 部分覆盖：能评的维度确实评了，因此不是整体 UNASSESSED
    assert report.state is AssessmentState.DECIDED
    assert report.overall_score is not None


def test_real_evaluation_is_marked_decided():
    g = QualityGuardrail()
    report = g.check(
        text="林渊握紧了剑。* 40",
        character_masks=_mask(),
        character_names=["林渊"],
        scene_info=SCENE,
    )
    assert report.state is AssessmentState.DECIDED
    assert report.overall_score is not None
    assert report.unevaluated_dimensions == []


def test_overall_uses_only_evaluated_dimensions_with_renormalized_weights():
    """加权平均只吃非 None 维度，且按剩余权重归一化。"""
    g = QualityGuardrail()
    report = g.check(text="林渊握紧了剑。* 40", character_masks=None,
                     character_names=None, scene_info=None)

    weights = {
        "language_style": 0.23, "character_consistency": 0.23, "plot_density": 0.18,
        "naming": 0.05, "viewpoint": 0.10, "rhythm": 0.13, "macro_pacing": 0.08,
    }
    scores = {
        "language_style": report.language_style_score,
        "character_consistency": report.character_consistency_score,
        "plot_density": report.plot_density_score,
        "naming": report.naming_score,
        "viewpoint": report.viewpoint_score,
        "rhythm": report.rhythm_score,
        "macro_pacing": report.macro_pacing_score,
    }
    evaluated = [k for k, v in scores.items() if v is not None]

    assert set(report.evaluated_dimensions) == set(evaluated)
    expected = sum(scores[k] * weights[k] for k in evaluated) / sum(
        weights[k] for k in evaluated
    )
    assert report.overall_score == pytest.approx(expected)


def test_partial_coverage_is_reported_explicitly():
    """只给 masks：命名与视角仍不可评估，但缺口必须显式列出。"""
    g = QualityGuardrail()
    report = g.check(text="林渊握紧了剑。* 40", character_masks=_mask(),
                     character_names=None, scene_info=None)

    assert report.state is AssessmentState.DECIDED
    assert report.unevaluated_dimensions == ["naming", "viewpoint"]
    assert "character_consistency" in report.evaluated_dimensions
    assert report.overall_score is not None


def test_no_dimension_evaluable_yields_unassessed_and_no_score(monkeypatch):
    """一个维度都评不出来：整体 UNASSESSED，overall_score 与 passed 都是 None。"""
    g = QualityGuardrail()

    def _nothing(*a, **k):
        return None, []

    for attr in ("_language_style", "_plot_density", "_rhythm", "_macro_pacing"):
        monkeypatch.setattr(g, attr, type("G", (), {"check": _nothing})())

    report = g.check(text="正文")

    assert report.state is AssessmentState.UNASSESSED
    assert report.overall_score is None
    assert report.passed is None
    assert report.evaluated_dimensions == []
    assert len(report.unevaluated_dimensions) == 7


def test_enforce_raises_when_nothing_was_evaluated(monkeypatch):
    """UNASSESSED 时 enforce 必须抛错，不得静默放行。

    只 catch QualityViolationError 的调用方会把静默 return 读成「通过」。
    「没评成」既不是不合格也不是通过，因此走专门的 GuardrailUnavailableError。
    """
    g = QualityGuardrail()

    def _nothing(*a, **k):
        return None, []

    for attr in ("_language_style", "_plot_density", "_rhythm", "_macro_pacing"):
        monkeypatch.setattr(g, attr, type("G", (), {"check": _nothing})())

    with pytest.raises(GuardrailUnavailableError):
        g.enforce(text="正文")


def test_policy_enforce_raises_when_guardrail_unavailable(monkeypatch):
    """PolicyValidator.enforce 同样不得把无结论当放行。"""
    v = PolicyValidator()

    def boom(*a, **k):
        raise RuntimeError("护栏炸了")

    monkeypatch.setattr(v, "_guardrail", type("G", (), {"check": boom})())

    with pytest.raises(PolicyUnavailableError):
        v.enforce(text="正文")


def test_policy_report_propagates_guardrail_state(monkeypatch):
    """护栏说 UNASSESSED，薄封装不得把它洗成 DECIDED。"""
    v = PolicyValidator()
    stub = type("G", (), {"check": staticmethod(lambda **k: _unassessed_quality_report())})()
    monkeypatch.setattr(v, "_guardrail", stub)

    report = v.check(text="正文")

    assert report.state is AssessmentState.UNASSESSED
    assert report.overall_score is None
    assert report.passed is None
    assert report.character_consistency_score is None


def _unassessed_quality_report():
    from engine.runtime.quality_guardrails.quality_guardrail import QualityReport

    return QualityReport(
        overall_score=None,
        language_style_score=None,
        character_consistency_score=None,
        plot_density_score=None,
        naming_score=None,
        viewpoint_score=None,
        rhythm_score=None,
        macro_pacing_score=None,
        all_violations=[],
        passed=None,
        state=AssessmentState.UNASSESSED,
        evaluated_dimensions=[],
        unevaluated_dimensions=[
            "language_style", "character_consistency", "plot_density", "naming",
            "viewpoint", "rhythm", "macro_pacing",
        ],
    )


def test_to_dict_is_none_safe():
    """to_dict 会进 JSON 落库，overall_score 为 None 时不得抛 TypeError。"""
    d = PolicyReport(state=AssessmentState.UNASSESSED).to_dict()

    assert d["overall_score"] is None
    assert d["state"] == "unassessed"
    assert d["passed"] is None


def test_novelist_assessment_survives_unevaluated_dimensions():
    """聚合端不得把 None 当 0 累加——那正是"没评"伪装成"零分"。

    登记表 F-09 的下游受害者之一：WritingOrchestrator 逐维求和时
    遇到 None 会 TypeError，改成 `or 0` 又会静默拉低均分。
    """
    from engine.runtime.quality_guardrails.quality_guardrail import QualityReport
    from engine.runtime.writing_orchestrator import ChapterResult, WritingOrchestrator

    unassessed = QualityReport(
        overall_score=None,
        language_style_score=None,
        character_consistency_score=None,
        plot_density_score=None,
        naming_score=None,
        viewpoint_score=None,
        rhythm_score=None,
        all_violations=[],
        passed=None,
    )
    results = [ChapterResult(chapter_number=1, quality_report=unassessed)]

    out = WritingOrchestrator.__new__(WritingOrchestrator).generate_novelist_assessment(results)

    assert out.get("overall_score") is None, "一维都没评出来时不得报 0 分"
    assert out.get("grade") == "未评估"
