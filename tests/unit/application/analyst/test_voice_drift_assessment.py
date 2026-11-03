# -*- coding: utf-8 -*-
"""第 89 批（N-24）：漂移判定改**书内相对**的守卫。

为什么要改
----------
绝对阈值对长书「几乎每章都命中」：实测（600 章那本）开局章节 **0.5364~0.6589**，
五百多章之后 **0.4034~0.5320** ⇒ 常态本身就在 0.5 附近
⇒ 阈值 0.50 对后期章节**命中率接近 100%** ⇒ 假告警 + 每次都触发定向修文 ⇒ 白烧钱。

⇒ 改成「比本书**近期常态**低多少」（常态 = 最近 N 章相似度的**中位数**）。

本文件钉住
----------
1. 常态高时，低分是异常；常态低时，**同样的低分不是异常**（长书场景）；
2. 修文的判据比告警**更严**（它要烧调用）；
3. **历史不足** ⇒ 回落绝对阈值（绝对口径是「开局健康」的标定）；
4. 判当前章时**排除自己**（否则异常值会把自己的中位数拉下来 ⇒ 把自己洗白）；
5. 中位数**抗离群**（几章异常值不该把常态拽走）；
6. 服务层与引擎层**共用同一份判定**（不许写两份）；
7. 相对阈值**可配置**（config → settings → 判定）。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from application.analyst.services.voice_drift_assessment import (  # noqa: E402
    DRIFT_RELATIVE_DROP_ALERT,
    DRIFT_RELATIVE_DROP_REWRITE,
    RECENT_MEDIAN_MIN_SAMPLES,
    assess,
    recent_similarity_median,
    relative_drop,
)


# ── 1. 常态决定「什么算异常」 ──────────────────────────────────────────


def test_a_low_score_is_an_outlier_when_the_book_norm_is_high():
    """常态 0.55 ⇒ 0.45（低 18%）⇒ 报异常。"""
    judgment = assess(0.45, 0.55, 0.50, 0.40)
    assert judgment["basis"] == "relative"
    assert judgment["alert"] is True
    assert judgment["rewrite"] is False, "低 18% 还不够烧一次定向修文"


def test_the_same_low_score_is_normal_when_the_book_norm_is_itself_low():
    """⚠ **长书场景**：常态 0.45 ⇒ 0.42 只是低 6.7% ⇒ **不报**。

    ⚠ 绝对阈值 0.50 在这里会**误报**（0.42 < 0.50）⇒ 这就是改动要解决的病。
    """
    judgment = assess(0.42, 0.45, 0.50, 0.40)
    assert judgment["basis"] == "relative"
    assert judgment["alert"] is False, "常态本身就在 0.45 ⇒ 0.42 不是异常"
    assert judgment["rewrite"] is False


def test_a_deep_drop_triggers_the_rewrite():
    """常态 0.55 ⇒ 0.38（低 31%）⇒ 值得花钱改写。"""
    judgment = assess(0.38, 0.55, 0.50, 0.40)
    assert judgment["alert"] is True
    assert judgment["rewrite"] is True


def test_the_rewrite_bar_is_stricter_than_the_alert_bar():
    """⚠ 修文要**烧调用** ⇒ 必须比告警更严（否则告警一亮就花钱）。"""
    assert DRIFT_RELATIVE_DROP_REWRITE > DRIFT_RELATIVE_DROP_ALERT


# ── 2. 历史不足 ⇒ 回落绝对阈值 ────────────────────────────────────────


def test_without_enough_history_it_falls_back_to_the_absolute_threshold():
    """常态样本不足 ⇒ 用绝对口径（标定口径 = 「开局健康」）。"""
    judgment = assess(0.45, None, 0.50, 0.40)
    assert judgment["basis"] == "absolute"
    assert judgment["alert"] is True, "0.45 < 0.50 ⇒ 绝对口径要报"
    assert judgment["rewrite"] is False, "0.45 > 0.40 ⇒ 绝对口径不该改写"


def test_the_median_needs_enough_samples():
    """样本不足 ⇒ 没有常态（⇒ 回落绝对）。"""
    assert RECENT_MEDIAN_MIN_SAMPLES >= 6, (
        "常态最少样本必须 > 告警窗口(5) ⇒ 否则「绝对兜底」在告警路径上永远走不到"
    )
    assert recent_similarity_median([0.5] * (RECENT_MEDIAN_MIN_SAMPLES - 1)) is None
    assert recent_similarity_median([0.5] * RECENT_MEDIAN_MIN_SAMPLES) is not None


# ── 3. 判当前章时排除自己 ──────────────────────────────────────────────


def test_the_chapter_being_judged_is_excluded_from_its_own_baseline():
    """⚠ 被判的章**不参与**自己的常态。

    ⚠ **用例要能区分**：中位数本来就抗单个离群值 ⇒ 奇数个样本时
      「排除自己」与「不排除」常常**同一个数** ⇒ 测不出差别。
      ⇒ 这里用 **7 个样本**（排除后剩 6 个，仍够最少样本数）
        且被排除的正是**最低的那一个**（当前章）⇒ 排除后常态被抬高。

    ⚠ 老实说：中位数**已经**很抗离群了（这就是选它而不是均值的原因），
      排除自己是**再加一道保险**（在**偶数**样本时它会把常态抬高一点点）。
    """
    scores = [0.30, 0.55, 0.56, 0.57, 0.58, 0.59, 0.60]
    chapters = [1, 2, 3, 4, 5, 6, 7]

    with_exclusion = recent_similarity_median(
        scores, exclude_chapter_number=1, chapters=chapters
    )
    without_exclusion = recent_similarity_median(scores, chapters=chapters)

    assert with_exclusion == pytest.approx(0.575), (
        "排除被判章之后常态应当抬高 ⇒ 它没参与自己的常态"
    )
    assert without_exclusion == pytest.approx(0.57), (
        "前提不成立：不排除时中位数应当不同（否则这条用例测不出差别）"
    )


def test_the_median_resists_outliers():
    """⚠ 中位数抗离群：几章异常值不该把常态拽走（均值会）。"""
    scores = [0.55, 0.56, 0.54, 0.55, 0.56, 0.55, 0.30]
    median = recent_similarity_median(scores)
    assert median == pytest.approx(0.55)


def test_relative_drop_is_never_negative():
    assert relative_drop(0.6, 0.5) == 0.0
    assert relative_drop(0.4, 0.5) == pytest.approx(0.2)


# ── 4. 服务层与引擎层共用同一份判定 ────────────────────────────────────


class _ScoreRepo:
    def __init__(self, rows):
        self.rows = list(rows)

    def upsert(self, **kw):
        self.rows = [r for r in self.rows if r.get("chapter_number") != kw.get("chapter_number")]
        self.rows.append(dict(kw))
        return "id"

    def list_by_novel(self, novel_id, limit=50):
        return list(self.rows)[:limit]

    def list_recent_by_novel(self, novel_id, limit=50):
        return list(self.rows)[-limit:]


def _service(rows):
    from application.analyst.services.voice_analysis_settings import (
        VoiceAnalysisRuntimeSettings,
    )
    from application.analyst.services.voice_drift_service import VoiceDriftService

    class _Fingerprint:
        def get_by_novel(self, novel_id, pov=None):
            return None

    return VoiceDriftService(
        _ScoreRepo(rows),
        _Fingerprint(),
        runtime_settings=VoiceAnalysisRuntimeSettings(),
    )


def test_a_book_whose_norm_is_low_does_not_raise_a_drift_alert():
    """⚠ **长书场景**（行为级）：常态 0.45 的一本书，低分章**不该**连成告警。

    ⚠ 旧口径（绝对 0.50）在这里**每章都命中** ⇒ 告警常亮。

    ⚠ 数据要 **11 章**：告警窗口 5 章 + 常态所需的历史 6 章
      ⇒ 相对判定才生效（历史不足会**回落绝对**口径 ⇒ 那正是新书的情形）。
    """
    history = [0.46, 0.44, 0.47, 0.45, 0.46, 0.44]   # 常态 ≈ 0.45
    window = [0.45, 0.43, 0.46, 0.44, 0.45]          # 最近 5 章 = 常态附近
    rows = [
        {"chapter_number": i, "similarity_score": s}
        for i, s in enumerate(history + window, start=1)
    ]
    svc = _service(rows)
    assert svc._check_drift_alert("n1") is False, (
        "常态本身在 0.45 ⇒ 这 5 章不是异常 ⇒ 不该告警"
    )


def test_a_real_outlier_run_still_raises_a_drift_alert():
    """⚠ 反向：常态 0.55 的一本书，连续 5 章掉到 0.40 ⇒ **必须**告警。"""
    rows = [
        {"chapter_number": i, "similarity_score": s}
        for i, s in enumerate(
            [0.55, 0.56, 0.54, 0.55, 0.56, 0.40, 0.39, 0.41, 0.38, 0.40, 0.39], start=1
        )
    ]
    svc = _service(rows)
    assert svc._check_drift_alert("n1") is True, (
        "连续 5 章明显低于常态 ⇒ 必须告警（否则这套判定就是瞎的）"
    )


def test_the_service_exposes_one_shared_assessment():
    """⚠ 服务层给引擎层的判定是**一份**（引擎不许自己再比一遍绝对阈值）。"""
    rows = [
        {"chapter_number": i, "similarity_score": s}
        for i, s in enumerate([0.55, 0.56, 0.54, 0.55, 0.56, 0.55, 0.42], start=1)
    ]
    svc = _service(rows)
    judgment = svc.assess_chapter("n1", 0.42, chapter_number=7)
    assert judgment["basis"] == "relative"
    assert judgment["rewrite"] is False, "低 23% 还不够烧一次修文"


def test_the_daemon_chain_prefers_the_shared_assessment():
    """⚠ 引擎那条链必须**优先**用服务给的判定（而不是自己比绝对阈值）。"""
    from engine.runtime.daemon_host import DaemonHostMixin

    host = object.__new__(DaemonHostMixin)
    fn = DaemonHostMixin._should_attempt_voice_rewrite

    # 绝对阈值（0.40）会判「要改写」，但服务说「不用」⇒ 以服务为准
    assert fn(host, {"similarity_score": 0.45,
                     "drift_assessment": {"rewrite": False}}) is False
    # 服务说「要改写」，即便绝对值还不低 ⇒ 也以服务为准
    assert fn(host, {"similarity_score": 0.52,
                     "drift_assessment": {"rewrite": True}}) is True
    # 服务没给判定 ⇒ 回落到绝对阈值（老路径）
    assert fn(host, {"similarity_score": 0.30}) is True
    assert fn(host, {"similarity_score": 0.60}) is False


def test_the_pipeline_chain_prefers_the_shared_assessment():
    """⚠ StoryPipeline 那条链同口径（两条链不许各写一份）。"""
    from engine.pipeline.base import BaseStoryPipeline
    from engine.pipeline.context import PipelineContext

    pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)
    ctx = PipelineContext(novel_id="n1", chapter_number=1, chapter_content="x")
    ctx.similarity_score = 0.45
    ctx.drift_rewrite_recommended = False
    assert pipeline._should_rewrite_voice(ctx) is False
    ctx.drift_rewrite_recommended = True
    assert pipeline._should_rewrite_voice(ctx) is True
    # 没给判定 ⇒ 回落绝对阈值
    ctx.drift_rewrite_recommended = None
    ctx.similarity_score = 0.30
    assert pipeline._should_rewrite_voice(ctx) is True
    ctx.similarity_score = 0.60
    assert pipeline._should_rewrite_voice(ctx) is False


# ── 5. 相对阈值可配置 ──────────────────────────────────────────────────


def test_the_relative_thresholds_are_configurable():
    """⚠ 相对阈值必须能配（config → settings → 判定）。"""
    import yaml

    from application.analyst.services.voice_analysis_settings import (
        get_voice_analysis_runtime_settings,
    )

    config = yaml.safe_load(
        (ROOT / "config" / "performance.yaml").read_text(encoding="utf-8-sig")
    ) or {}
    section = (config.get("analyst") or {}).get("voice_analysis") or {}
    assert "drift_relative_drop_alert" in section, "配置里缺相对告警阈值"
    assert "voice_rewrite_relative_drop" in section, "配置里缺相对修文阈值"

    settings = get_voice_analysis_runtime_settings()
    assert settings.drift_relative_drop_alert == pytest.approx(0.15)
    assert settings.voice_rewrite_relative_drop == pytest.approx(0.25)
