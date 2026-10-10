"""视角判据与伏笔绑定判据必须按各自的前置条件分别门控。

背景：quality_guardrail.check 曾把 foreshadows 作为参数传给 viewpoint.check，
而那次调用整体被包在 `if scene_info:` 里。两个判据的前置条件其实互相独立
（视角要 cast，绑定只要伏笔表），被同一个守卫绑在一起的后果是：
没有 cast 的书会连带跳过伏笔绑定校验——那条校验就此静默失效。

本测试锁住修复后的语义：
- scene_info=None + foreshadows 非空 → 绑定校验仍执行，且违规进 vp_score
- 视角分不得因为"没数据"而高于保守基准
- API 路径（只传 scene_info、从不传 foreshadows）行为不变
"""
import pytest

from engine.runtime.quality_guardrails.quality_guardrail import QualityGuardrail

TEXT = "林渊握紧了剑，陈长老看着他。剑锋在雨里闪了一下，林渊退了半步。" * 3

# 必然触发 weak_binding 的核心伏笔：权重 > 0.8 但只绑定到角色层级
CORE_FORESHADOW = [{"description": "断魂崖下的残剑", "emotional_weight": 0.95, "binding_level": 1}]
CLEAN_FORESHADOW = [{"description": "断魂崖下的残剑", "emotional_weight": 0.95, "binding_level": 3}]

SCENE = {
    "viewpoint_character": "林渊",
    "characters_present": ["林渊", "陈长老"],
    "key_event": "遭遇伏击",
    "information_asymmetry": {},
}

CONSERVATIVE_BASELINE = 0.77


def _viewpoint_violations(report):
    """只按 dimension 过滤，不依赖违规文案——护栏改名时不该误红。

    scene_info=None 时视角类判据全部无法触发（没有 viewpoint_character 就早退），
    因此该组合下出现任何 viewpoint 违规，只可能来自伏笔绑定判据。
    """
    return [v for v in report.all_violations if v.get("dimension") == "viewpoint"]


class TestForeshadowBindingIndependentOfSceneInfo:
    def test_binding_check_runs_without_scene_info(self):
        """核心场景：scene_info 缺失时绑定校验仍必须执行。"""
        report = QualityGuardrail().advise(
            text=TEXT, scene_info=None, foreshadows=CORE_FORESHADOW
        )

        assert _viewpoint_violations(report), "无 cast 时伏笔绑定校验被静默跳过了"

    def test_binding_violation_lowers_score_without_scene_info(self):
        """回答语义问题 1：违规必须同时反映在分数上。

        若只产违规不扣分，enforce() 的阈值与 MIN_PASS_SCORE 就看不到它，
        违规退化成纯建议。
        """
        with_violation = QualityGuardrail().advise(
            text=TEXT, scene_info=None, foreshadows=CORE_FORESHADOW
        )
        without = QualityGuardrail().advise(text=TEXT, scene_info=None)

        assert with_violation.viewpoint_score < without.viewpoint_score

    def test_no_data_never_scores_above_conservative_baseline(self):
        """没数据时保持保守分 0.77，不得因为「没查出违规」而报满分。"""
        report = QualityGuardrail().advise(
            text=TEXT, scene_info=None, foreshadows=CLEAN_FORESHADOW
        )

        assert report.viewpoint_score == pytest.approx(CONSERVATIVE_BASELINE)

    def test_violation_never_inflates_score_above_baseline(self):
        """发现违规也只应低于保守基准，不得高于它。"""
        report = QualityGuardrail().advise(
            text=TEXT, scene_info=None, foreshadows=CORE_FORESHADOW
        )

        assert report.viewpoint_score < CONSERVATIVE_BASELINE


class TestSceneInfoPresentBehaviourUnchanged:
    """scene_info 存在时的行为必须与解耦前逐值一致（无回归）。"""

    def test_clean_scores_full_credit(self):
        report = QualityGuardrail().advise(
            text=TEXT, scene_info=SCENE, foreshadows=CLEAN_FORESHADOW
        )

        assert report.viewpoint_score == pytest.approx(1.0)
        assert not _viewpoint_violations(report)

    def test_binding_violation_score_matches_legacy_formula(self):
        """0.7 severity → 1.0 - 0.7*0.15 = 0.895，与解耦前同值。"""
        report = QualityGuardrail().advise(
            text=TEXT, scene_info=SCENE, foreshadows=CORE_FORESHADOW
        )

        assert report.viewpoint_score == pytest.approx(0.895)
        assert len(_viewpoint_violations(report)) == 1


class TestApiPathUnaffected:
    """API 路径从不传 foreshadows，解耦后行为必须逐值不变。"""

    def test_api_shape_scene_info_only_keeps_conservative_score(self):
        report = QualityGuardrail().advise(text=TEXT, scene_info=None)

        assert report.viewpoint_score == pytest.approx(CONSERVATIVE_BASELINE)
        assert not _viewpoint_violations(report)

    def test_api_shape_with_scene_info_still_scores_one(self):
        report = QualityGuardrail().advise(text=TEXT, scene_info=SCENE)

        assert report.viewpoint_score == pytest.approx(1.0)
