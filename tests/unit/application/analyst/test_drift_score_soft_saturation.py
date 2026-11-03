# -*- coding: utf-8 -*-
"""第 86 批：漂移相似度改成**软饱和**（不再被硬截断量化）。

病根（生产实跑撞出来的）
------------------------
第 85 批真跑一本书时：连续 3 次定向修文，相似度**始终 0.4167** ⇒ 修文循环无从收敛
⇒ 后两次调用纯烧钱。

根因不在循环，而在**打分函数**：每维偏差被 ``min(1.0, rel_diff)`` **硬截断**
⇒ 「差 1 个容差」与「差 5 个容差」在**该维**完全同分
⇒ 已经饱和的那一维，**怎么改都看不出来**。

⚠ 别把它说成「总分被量化成 1/N 的整数倍」——**那是不对的**：
  截断只发生在**单维**上；各维偏差都不到 1 个容差时，总分仍然是连续的。
  真正的病灶是「**有几维已经饱和** ⇒ 那一维的改进被吃掉」。

⇒ 现在用指数软饱和 ``1 - e^(-x)``：**严格单调** ⇒ 任何一维动了，分数就动。

本文件钉住
----------
1. **单调**：任何一维偏离变大 ⇒ 相似度**严格变小**（不增）；
2. **不冻结**：一维已经「差很多」之后，继续变差 ⇒ 分数**还要再降**；
3. **阈值含义不变**：相似度 0.75 ⇔ 平均偏差 ≈ **0.288 个容差**
   ⇒ 0.75 / 0.68 两个阈值**不用重标定**（口径略放宽，方向=少误报）；
4. **边界**：分数仍在 0..1；无基准 ⇒ 1.0（既有行为）。
"""
from __future__ import annotations

import math
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from application.analyst.services.llm_voice_analysis_service import (  # noqa: E402
    STYLE_DIMENSIONS,
    LLMVoiceAnalysisService,
)


@pytest.fixture()
def service():
    return LLMVoiceAnalysisService(llm_service=None)


def _baseline(value: float = 0.5, tolerance: float = 0.2) -> dict:
    return {
        "baseline": {dim: value for dim in STYLE_DIMENSIONS},
        "tolerance": {dim: tolerance for dim in STYLE_DIMENSIONS},
    }


# ── 1. 单调 ────────────────────────────────────────────────────────────


def test_moving_away_from_the_baseline_always_lowers_the_score(service):
    """⚠ 任何一维偏离变大 ⇒ 相似度**严格**变小。"""
    baseline = _baseline()
    reference = {dim: 0.5 for dim in STYLE_DIMENSIONS}

    previous = service.compute_drift_score(reference, baseline)
    assert previous == pytest.approx(1.0), "完全一致时相似度应是 1.0"

    for step in (0.02, 0.05, 0.1, 0.2, 0.4):
        moved = dict(reference)
        moved[STYLE_DIMENSIONS[0]] = 0.5 + step
        score = service.compute_drift_score(moved, baseline)
        assert score < previous, (
            "偏离变大（+%s）相似度却没有变小（%s -> %s）⇒ 打分不单调"
            % (step, previous, score)
        )
        previous = score


def test_moving_back_towards_the_baseline_raises_the_score(service):
    """⚠ 反向也要单调（否则「改好了」反而分数更低 ⇒ 循环会越改越糟）。"""
    baseline = _baseline()
    far = {dim: 0.5 for dim in STYLE_DIMENSIONS}
    far[STYLE_DIMENSIONS[0]] = 0.9
    near = dict(far)
    near[STYLE_DIMENSIONS[0]] = 0.55

    assert service.compute_drift_score(near, baseline) > service.compute_drift_score(
        far, baseline
    )


# ── 2. 不量化（这一条正是硬截断的病根）────────────────────────────────


def test_a_tiny_change_moves_the_score_by_a_tiny_amount(service):
    """⚠ 一维动 0.001 ⇒ 分数要**动一点点**（不是纹丝不动）。

    ⚠ 硬截断口径下：偏差被截在 1.0 ⇒ 这一维动多动少都算「差很多」
    ⇒ 分数**完全不变** ⇒ 修文循环无从收敛（生产实测：始终 0.4167）。
    """
    baseline = _baseline()
    base_vector = {dim: 0.5 for dim in STYLE_DIMENSIONS}
    moved = dict(base_vector)
    moved[STYLE_DIMENSIONS[0]] = 0.9

    before = service.compute_drift_score(base_vector, baseline)
    after = service.compute_drift_score(moved, baseline)
    assert after < before, "这一维偏了 0.4 分却没掉相似度"

    # 再看「差很多」的那一维：把 0.9 挪到 0.95（仍远大于容差）
    farther = dict(moved)
    farther[STYLE_DIMENSIONS[0]] = 0.95
    assert service.compute_drift_score(farther, baseline) < after, (
        "已经「差很多」之后再差更多，分数却没变 ⇒ 仍被硬截断量化"
    )


def test_the_score_is_not_frozen_when_a_dimension_stays_saturated(service):
    """⚠ 一维已经「差很多」之后，再差更多 ⇒ 分数**还要再降**。

    ⚠ 硬截断口径下：偏差被截在 1.0 ⇒ 这一维动多动少都算「差很多」
    ⇒ 分数**完全不变** ⇒ 修文循环无从收敛（生产实测：始终 0.4167）。
    ⚠ 注意：这不是「总分被量化成 1/N 整数倍」——**单维**饱和才会这样。
    """
    baseline = _baseline()
    base_vector = {dim: 0.5 for dim in STYLE_DIMENSIONS}
    moved = dict(base_vector)
    moved[STYLE_DIMENSIONS[0]] = 0.9

    before = service.compute_drift_score(base_vector, baseline)
    after = service.compute_drift_score(moved, baseline)
    assert after < before, "这一维偏了 0.4 分却没掉相似度"

    # 再看「差很多」的那一维：把 0.9 挪到 0.95（仍远大于容差）
    farther = dict(moved)
    farther[STYLE_DIMENSIONS[0]] = 0.95
    assert service.compute_drift_score(farther, baseline) < after, (
        "已经「差很多」之后再差更多，分数却没变 ⇒ 仍被硬截断量化"
    )


def test_different_deviations_give_different_scores(service):
    """⚠ 不同偏离幅度 ⇒ 不同分数（不许出现「同分」。"""
    baseline = _baseline()
    scores = set()
    for delta in (0.03, 0.07, 0.11, 0.13, 0.17):
        vector = {dim: 0.5 for dim in STYLE_DIMENSIONS}
        vector[STYLE_DIMENSIONS[0]] = 0.5 + delta
        scores.add(service.compute_drift_score(vector, baseline))
    assert len(scores) >= 4, (
        "不同偏离幅度只落到 %d 个不同分数 ⇒ 仍被量化" % len(scores)
    )


# ── 3. 阈值含义不变 ────────────────────────────────────────────────────


def test_the_warning_threshold_keeps_its_tolerance_meaning(service):
    """⚠ 相似度 0.75 ⇒ 平均偏差 ≈ **0.288 个容差**（软饱和口径）。

    ⚠ 线性（硬截断）口径下是 0.25 个容差 ⇒ 阈值**略微放宽**（约 15%）
    ⇒ 方向是「**少误报**」，与本批要解决的问题（每章都被误报「严重偏离」然后白改 3 次）一致。
    ⚠ 0.75 / 0.68 两个阈值**不用重标定**（口径变了，但「相似度低于 X = 偏离」这个含义没变）。

    ⚠ 口径换算：``1 - e^(-x) = 0.25`` ⇒ ``x = -ln(0.75) = 0.2877``。
    """
    x = -math.log(0.75)   # 0.28768… 个容差
    baseline = _baseline(tolerance=0.2)
    vector = {dim: 0.5 + x * 0.2 for dim in STYLE_DIMENSIONS}

    score = service.compute_drift_score(vector, baseline)
    assert score == pytest.approx(0.75, abs=0.01), (
        "相似度 0.75 应当对应「平均差 0.288 个容差」，实得 %s" % score
    )


def test_the_soft_saturation_is_slightly_more_permissive_than_linear(service):
    """⚠ 显式钉住「比线性口径**略放宽**」这件事（口径变了要看得见，不是悄悄变）。

    ⇒ 线性口径下「平均差 0.25 个容差」⇒ 相似度 0.75（正好在告警线上）
    ⇒ 软饱和口径下同一个偏离量给的是 **> 0.75**（不再被误报）
    ⇒ 这就是「阈值含义不变、但同样偏离不再被一刀切判死」的来源。
    """
    baseline = _baseline(tolerance=0.2)
    vector = {dim: 0.5 + 0.25 * 0.2 for dim in STYLE_DIMENSIONS}

    score = service.compute_drift_score(vector, baseline)
    assert score > 0.75, (
        "同等的偏离量却仍被判到告警线以下 ⇒ 口径没真正放宽（等于没改）"
    )
    assert score < 0.80, "放宽幅度失控 ⇒ 口径改动过大"


# ── 4. 边界与既有行为 ──────────────────────────────────────────────────


def test_the_score_stays_in_the_unit_range(service):
    """⚠ 分数始终在 0..1。"""
    baseline = _baseline()
    assert service.compute_drift_score({dim: 1.0 for dim in STYLE_DIMENSIONS}, baseline) >= 0.0
    assert service.compute_drift_score({dim: 0.0 for dim in STYLE_DIMENSIONS}, baseline) <= 1.0
    assert service.compute_drift_score({dim: 0.0 for dim in STYLE_DIMENSIONS}, baseline) >= 0.0


def test_no_baseline_means_no_drift(service):
    """⚠ 没有基准 ⇒ 1.0（不判漂移）—— 既有行为，不能被改掉。"""
    assert service.compute_drift_score({dim: 0.0 for dim in STYLE_DIMENSIONS}, {}) == 1.0
    assert service.compute_drift_score({dim: 0.0 for dim in STYLE_DIMENSIONS}, None) == 1.0


def test_the_exponential_saturation_matches_the_documented_formula(service):
    """⚠ 口径写死：``1 - e^(-x)``（守卫钉住实现，别被悄悄改回去）。"""
    baseline = _baseline(tolerance=0.2)
    vector = {dim: 0.5 for dim in STYLE_DIMENSIONS}
    vector[STYLE_DIMENSIONS[0]] = 0.5 + 0.2 * 2.0   # 差 2 个容差

    expected_dim = 1.0 - math.exp(-2.0)
    expected = 1.0 - expected_dim / len(STYLE_DIMENSIONS)
    assert service.compute_drift_score(vector, baseline) == pytest.approx(expected, abs=1e-3)
