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

def test_weights_and_thresholds_values_are_the_calibrated_ones():
    """权重没变；**阈值按「每千字」口径重标定过**（第 16 批，生产库实测后）。

    ⚠ 旧阈值（5.0/3.0/1.0）是按「每万字」定的 ⇒ 对 2000 字的章节，
    5 个命中就判「严重」⇒ 分档没有区分力。新的见 ``ASSESSMENT_THRESHOLDS`` 注释里的实测表。
    """
    assert src.SEVERITY_WEIGHTS == {"critical": 3.0, "warning": 1.0, "info": 0.3}
    assert src.ASSESSMENT_THRESHOLDS == [
        (4.0, "严重"), (2.0, "中等"), (0.8, "轻微"), (0.0, "纯净"),
    ]


def test_unknown_severity_weight_is_not_zero():
    """未知严重性兜底权重刻意不等于 0。

    0 会让「严重性读不出来」被算成「没问题」—— 那是本项目反复清理的那类谎报。
    """
    assert src.DEFAULT_SEVERITY_WEIGHT != 0
    assert src.DEFAULT_SEVERITY_WEIGHT == 0.5


# ── 归一化：口径 = 「每千字加权命中数」 ─────────────────────────────────

@pytest.mark.parametrize(
    "raw_score,chars",
    [(0.0, 1000), (3.0, 1000), (12.5, 2000), (0.7, 350), (100.0, 500)],
)
def test_normalisation_is_per_thousand_chars(raw_score, chars):
    """归一化 = ``raw * 1000 / chars``（**每千字**加权命中数）。

    ⚠ 旧口径是「每万字」（``raw*10000/chars``）⇒ 对章节惩罚极重 ⇒ 已换。
    """
    expected = min(100.0, (raw_score / max(chars, 1)) * 1000)
    assert src.normalize_severity_score(raw_score, chars) == pytest.approx(expected, abs=1e-9)


def test_normalisation_does_not_penalise_short_chapters():
    """**同样的命中数，章节长短不该让分数差一个数量级。**

    ⚠ 旧口径（每万字）下：3 个命中在 2000 字 ⇒ 15 分（「严重」），
    在 20000 字 ⇒ 1.5 分（「轻微」）⇒ **同一段文字，评分差 10 倍**。
    """
    short = src.normalize_severity_score(3.0, 2000)
    long = src.normalize_severity_score(3.0, 20000)

    assert short == pytest.approx(1.5, abs=1e-9)
    assert long == pytest.approx(0.15, abs=1e-9)
    # 密度口径下，差异只来自「每千字的命中数」本身（10 倍长度 ⇒ 1/10 密度）
    assert short / long == pytest.approx(10.0, rel=1e-6)


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
        (0.79, "纯净"),
        (0.8, "轻微"),
        (1.9, "轻微"),
        (2.0, "中等"),
        (3.9, "中等"),
        (4.0, "严重"),
        (99.0, "严重"),
    ],
)
def test_classification_boundaries(score, expected):
    assert src.classify_assessment(score) == expected


@pytest.mark.parametrize(
    "raw,chars,expected",
    [
        # ↓ 生产库实测的真实数字（raw 由命中的严重性加权得到）
        (1.2, 2030, "纯净"),   # 一章人设准确、推理扎实，只有 4 处行内破折号
        (3.9, 2476, "轻微"),   # 含 1 处「指尖泛白」（critical）
        (0.9, 1656, "纯净"),   # 只有 3 处行内破折号
        (5.0, 2160, "中等"),   # AI 味重的一章：5 处情绪套路
    ],
)
def test_the_calibration_separates_good_chapters_from_ai_slop(raw, chars, expected):
    """**校准守卫**：生产库实测的好章节**不许**被判「严重」。

    ⚠ 这条是第 16 批改口径的**根据**本身 ⇒ 必须钉住，
    否则哪天有人把阈值调回旧值，好章节又会「严重」而没人发现。
    """
    score = src.normalize_severity_score(raw, chars)

    assert src.classify_assessment(score) == expected, (
        f"raw={raw} chars={chars} ⇒ score={score:.2f}，评级应为「{expected}」"
    )


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
