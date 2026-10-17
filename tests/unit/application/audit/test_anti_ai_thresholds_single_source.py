"""F-05：anti_ai 权重/阈值必须只有一份定义。

背景
----
``AntiAIMetricsService`` 与 ``AntiAIAuditor`` 各有一份**逐键相同**的
``SEVERITY_WEIGHTS`` / ``ASSESSMENT_THRESHOLDS``，归一化公式是数学等价的两种写法。
今天一致，但没有任何机制保证它们**继续**一致 —— 改一边忘了另一边，
就会让「章指标快照」与「章节审计报告」对同一段文字给出不同分数，且不报错。

核心守卫是**身份断言**（``is``）而不是相等断言：
相等断言在有人重新抄一份时仍会通过，而那正是我们要防的退化。
"""
import pytest

from application.audit.services import anti_ai_thresholds as src
from application.audit.services.anti_ai_audit import AntiAIAuditor
from application.audit.services.anti_ai_metrics import AntiAIMetricsService


# ── 单一事实源：身份而非相等 ────────────────────────────────────────────

def test_both_classes_point_at_the_same_objects():
    """**必须 is 同一对象**，不是 == 相等。

    == 相等的断言在「有人把常量抄回类里」时仍会通过 —— 那正是本条要防的退化。
    """
    assert AntiAIAuditor.SEVERITY_WEIGHTS is src.SEVERITY_WEIGHTS
    assert AntiAIMetricsService.SEVERITY_WEIGHTS is src.SEVERITY_WEIGHTS
    assert AntiAIAuditor.ASSESSMENT_THRESHOLDS is src.ASSESSMENT_THRESHOLDS
    assert AntiAIMetricsService.ASSESSMENT_THRESHOLDS is src.ASSESSMENT_THRESHOLDS


def test_no_literal_threshold_definitions_remain_in_either_module():
    """源码里不得再出现字面量定义 —— 防止有人「顺手又抄一份」。"""
    from pathlib import Path

    import application.audit.services.anti_ai_audit as audit_mod
    import application.audit.services.anti_ai_metrics as metrics_mod

    for mod in (audit_mod, metrics_mod):
        src_text = Path(mod.__file__).read_text(encoding="utf-8")
        assert '"critical": 3.0' not in src_text, (
            f"{mod.__name__} 里又出现了字面量权重定义"
        )
        assert "(5.0," not in src_text, (
            f"{mod.__name__} 里又出现了字面量阈值定义"
        )


# ── 常量本身 ────────────────────────────────────────────────────────────

def test_weights_and_thresholds_values_unchanged():
    """抽取后取值必须与原先两份**逐键一致**。"""
    assert src.SEVERITY_WEIGHTS == {"critical": 3.0, "warning": 1.0, "info": 0.3}
    assert src.ASSESSMENT_THRESHOLDS == [
        (5.0, "严重"), (3.0, "中等"), (1.0, "轻微"), (0.0, "纯净"),
    ]


def test_unknown_severity_weight_is_not_zero():
    """未知严重性兜底权重刻意不等于 0。

    0 会让「严重性读不出来」被算成「没问题」—— 那是本项目反复清理的那类谎报。
    """
    assert src.DEFAULT_SEVERITY_WEIGHT != 0
    assert src.DEFAULT_SEVERITY_WEIGHT == 0.5


# ── 归一化：两种旧写法必须仍然等价 ──────────────────────────────────────

@pytest.mark.parametrize(
    "raw_score,chars",
    [(0.0, 1000), (3.0, 1000), (12.5, 2000), (0.7, 350), (100.0, 500)],
)
def test_normalisation_keeps_both_legacy_formulas_equivalent(raw_score, chars):
    """旧两处写法：(raw/(chars/1000))*10 与 (raw/chars)*1000*10。

    抽取后统一走一个函数，必须与**两种旧写法都**给出相同结果。
    """
    chars_per_k = max(chars / 1000.0, 0.001)
    legacy_metrics = min(100.0, (raw_score / chars_per_k) * 10)
    legacy_audit = min(100.0, (raw_score / max(chars, 1)) * 1000 * 10)
    shared = src.normalize_severity_score(raw_score, chars)
    assert shared == pytest.approx(legacy_audit, abs=1e-9)
    assert shared == pytest.approx(legacy_metrics, abs=1e-9)


def test_normalisation_is_capped_at_100():
    assert src.normalize_severity_score(999999.0, 100) == 100.0


def test_normalisation_handles_zero_length_content():
    """空正文不得除零。"""
    assert src.normalize_severity_score(1.0, 0) > 0


# ── 分级 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "score,expected",
    [
        (0.0, "纯净"),
        (0.9, "纯净"),
        (1.0, "轻微"),
        (2.9, "轻微"),
        (3.0, "中等"),
        (4.9, "中等"),
        (5.0, "严重"),
        (99.0, "严重"),
    ],
)
def test_classification_boundaries(score, expected):
    assert src.classify_assessment(score) == expected


def test_threshold_table_is_descending():
    """阈值必须降序 —— 否则首次命中即返回的逻辑会取错档。"""
    vals = [t for t, _ in src.ASSESSMENT_THRESHOLDS]
    assert vals == sorted(vals, reverse=True)


# ── raw_score ───────────────────────────────────────────────────────────

def test_raw_score_uses_weights():
    assert src.raw_severity_score(["critical", "critical", "warning"]) == pytest.approx(7.0)
    assert src.raw_severity_score([]) == 0.0


def test_raw_score_unknown_severity_uses_default():
    got = src.raw_severity_score(["???", "critical"])
    assert got == pytest.approx(3.0 + src.DEFAULT_SEVERITY_WEIGHT)


def test_raw_score_accepts_a_generator():
    """两个调用点都传的是生成器，函数必须能吃。"""
    assert src.raw_severity_score(s for s in ["info", "info"]) == pytest.approx(0.6)
