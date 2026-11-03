# -*- coding: utf-8 -*-
"""第 86 批（续）：文风漂移**阈值口径**重标定的守卫。

为什么要重标定
--------------
相似度 = ``1 - 平均(每维「偏差 ÷ 容差」)``，而容差 = ``max(0.1, 标准差×1.5)``
⇒ **健康章节的典型得分天然落在 0.5 附近**（因为「差 1 个标准差」是常态）。

真实分布（两本书 12 个**健康**章节，**真模型**跑出来的，
报告见 ``review-briefs/_t86_threshold_calibration.txt``）：

* min = **0.468** / 中位 = **0.554** / 均值 = **0.569** / max = **0.671**

而原来的两个阈值是照着**坏掉的指标**定的：

* 漂移告警 0.75 ⇒ 对健康章节**命中率 100%**（每章都误报偏离）；
* 修文触发 0.68 ⇒ 对健康章节**命中率 100%** ⇒ **每章都触发 3 轮定向修文** ⇒ 白烧钱。

⇒ 用户拍板：**漂移 0.50 / 修文 0.40**
（0.50 落在健康分布下沿之外 ⇒ 命中率 8%；0.40 低于全部健康样本 ⇒ 零误触发）。

本文件钉住
----------
1. 四个阈值常量的**字面量**（期望值写死，不从被测模块 import 回来比较）；
2. 「修文阈值 < 漂移阈值」（修文是更贵、更该慎重的动作 ⇒ 必须更严）；
3. **展示层与判定层同口径**：面板的 warning/critical 阈值
   必须正好等于 ``1 - 漂移阈值`` / ``1 - 修文阈值``
   （drift = 1 - similarity ⇒ 两边换算必须一致，否则面板与后端各说各话）；
4. **配置驱动运行时**：``config/performance.yaml`` 的值必须与模块常量一致；
5. **标定依据**：阈值必须仍然待在实测健康区间的正确位置
   （改阈值的人得先重新量一遍分布，否则这条会红）。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

# ⚠ 实测得到的健康分布（真模型，12 个样本）—— 期望值**写死**
HEALTHY_MIN = 0.468
HEALTHY_MEDIAN = 0.554
HEALTHY_MAX = 0.671

DRIFT_ALERT_EXPECTED = 0.50
VOICE_REWRITE_EXPECTED = 0.40


def _config() -> dict:
    import yaml

    path = ROOT / "config" / "performance.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}


# ── 1. 四个常量的字面量 ────────────────────────────────────────────────


def test_the_drift_alert_threshold_is_the_calibrated_value():
    from application.analyst.services.voice_drift_service import DRIFT_ALERT_THRESHOLD

    assert DRIFT_ALERT_THRESHOLD == DRIFT_ALERT_EXPECTED


def test_the_dataclass_default_matches_the_module_constant():
    """⚠ 两份字面量（故意各写一份，避免循环依赖）必须一致。"""
    from application.analyst.services.voice_analysis_settings import (
        DRIFT_ALERT_THRESHOLD_DEFAULT,
    )
    from application.analyst.services.voice_drift_service import DRIFT_ALERT_THRESHOLD

    assert DRIFT_ALERT_THRESHOLD_DEFAULT == DRIFT_ALERT_EXPECTED
    assert DRIFT_ALERT_THRESHOLD_DEFAULT == DRIFT_ALERT_THRESHOLD


def test_the_rewrite_thresholds_are_the_calibrated_value():
    """⚠ **两条**改写链各有一份阈值常量（daemon 那条 + StoryPipeline 那条）
    ⇒ 两边都必须落在标定值上（否则一条链照旧白烧钱）。"""
    from engine.pipeline.base import BaseStoryPipeline
    from engine.runtime.daemon_host import VOICE_REWRITE_THRESHOLD

    assert VOICE_REWRITE_THRESHOLD == VOICE_REWRITE_EXPECTED
    assert BaseStoryPipeline.VOICE_REWRITE_THRESHOLD == VOICE_REWRITE_EXPECTED


def test_the_display_fallback_thresholds_are_the_calibrated_value():
    from engine.pipeline.base import BaseStoryPipeline
    from engine.runtime.daemon_host import VOICE_WARNING_DEFAULT_THRESHOLD

    assert VOICE_WARNING_DEFAULT_THRESHOLD == DRIFT_ALERT_EXPECTED
    assert BaseStoryPipeline.VOICE_WARNING_DEFAULT_THRESHOLD == DRIFT_ALERT_EXPECTED


# ── 2. 修文必须比告警更严 ──────────────────────────────────────────────


def test_the_rewrite_threshold_is_stricter_than_the_alert_threshold():
    """⚠ 修文要**花钱**（每轮一次模型调用）⇒ 必须比「只是提示一下」更严。"""
    from application.analyst.services.voice_drift_service import DRIFT_ALERT_THRESHOLD
    from engine.runtime.daemon_host import VOICE_REWRITE_THRESHOLD

    assert VOICE_REWRITE_THRESHOLD < DRIFT_ALERT_THRESHOLD, (
        "修文阈值 %s 不比告警阈值 %s 严 ⇒ 告警亮了就会去改写 ⇒ 白烧钱"
        % (VOICE_REWRITE_THRESHOLD, DRIFT_ALERT_THRESHOLD)
    )


# ── 3. 展示层与判定层同口径 ────────────────────────────────────────────


def test_the_panel_thresholds_are_the_complement_of_the_judgement_thresholds():
    """⚠ 面板用的是 **drift = 1 - similarity** 刻度 ⇒ 必须正好互补。

    ⚠ 不同口径 ⇒ 面板显示「正常」而后台在改写（或反过来）⇒ 用户看到的与
      实际发生的**对不上**。
    """
    from application.analyst.services.voice_drift_service import DRIFT_ALERT_THRESHOLD
    from engine.runtime.daemon_host import VOICE_REWRITE_THRESHOLD
    from interfaces.api.v1.workbench.monitor import DRIFT_CRITICAL, DRIFT_WARNING

    assert DRIFT_WARNING == pytest.approx(1.0 - DRIFT_ALERT_THRESHOLD, abs=1e-9), (
        "面板的 warning 阈值 %s 与漂移阈值 %s 不是互补 ⇒ 两边口径不一致"
        % (DRIFT_WARNING, DRIFT_ALERT_THRESHOLD)
    )
    assert DRIFT_CRITICAL == pytest.approx(1.0 - VOICE_REWRITE_THRESHOLD, abs=1e-9), (
        "面板的 critical 阈值 %s 与修文阈值 %s 不是互补 ⇒ 两边口径不一致"
        % (DRIFT_CRITICAL, VOICE_REWRITE_THRESHOLD)
    )


# ── 4. 配置驱动运行时 ──────────────────────────────────────────────────


def test_the_config_value_matches_the_module_constant():
    """⚠ 配置是运行时真正读的那份 ⇒ 必须与模块常量一致（否则两边结论不同）。"""
    from application.analyst.services.voice_drift_service import DRIFT_ALERT_THRESHOLD

    section = (_config().get("analyst") or {}).get("voice_analysis") or {}
    assert "drift_alert_threshold" in section, "配置里缺这一项"
    assert float(section["drift_alert_threshold"]) == pytest.approx(
        DRIFT_ALERT_THRESHOLD, abs=1e-9
    ), "配置值与模块常量漂了 ⇒ 用户改配置后两边结论不同"


def test_the_runtime_settings_read_the_configured_value():
    from application.analyst.services.voice_analysis_settings import (
        get_voice_analysis_runtime_settings,
    )

    settings = get_voice_analysis_runtime_settings()
    assert settings.drift_alert_threshold == pytest.approx(
        DRIFT_ALERT_EXPECTED, abs=1e-9
    )


# ── 5. 标定依据：阈值必须待在实测健康区间的正确位置 ────────────────────


def test_the_rewrite_threshold_never_fires_on_a_healthy_chapter():
    """⚠ 修文阈值必须**低于全部健康样本** ⇒ 健康书**零**误触发（= 不白烧钱）。"""
    from engine.runtime.daemon_host import VOICE_REWRITE_THRESHOLD

    assert VOICE_REWRITE_THRESHOLD < HEALTHY_MIN, (
        "修文阈值 %s ≥ 实测健康最低分 %s ⇒ 健康章节也会触发修文 ⇒ 白烧钱"
        % (VOICE_REWRITE_THRESHOLD, HEALTHY_MIN)
    )


def test_the_alert_threshold_does_not_flag_half_of_the_healthy_range():
    """⚠ 漂移告警阈值必须落在健康分布的**中位数以下** ⇒ 不会把大半健康章判成偏离。"""
    from application.analyst.services.voice_drift_service import DRIFT_ALERT_THRESHOLD

    assert DRIFT_ALERT_THRESHOLD < HEALTHY_MEDIAN, (
        "漂移阈值 %s ≥ 实测健康中位数 %s ⇒ 超过一半的健康章会被判成偏离"
        % (DRIFT_ALERT_THRESHOLD, HEALTHY_MEDIAN)
    )


def test_the_alert_threshold_still_catches_the_worst_healthy_sample():
    """⚠ 反向：阈值不能低到**什么都报不出来**（那等于把功能关掉）。"""
    from application.analyst.services.voice_drift_service import DRIFT_ALERT_THRESHOLD

    assert DRIFT_ALERT_THRESHOLD > HEALTHY_MIN, (
        "漂移阈值 %s ≤ 实测健康最低分 %s ⇒ 连最差的那章都报不出来 ⇒ 形同虚设"
        % (DRIFT_ALERT_THRESHOLD, HEALTHY_MIN)
    )
