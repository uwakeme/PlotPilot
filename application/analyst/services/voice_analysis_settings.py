"""Runtime settings for voice analysis caches."""

from __future__ import annotations

from dataclasses import dataclass

from application.core.config.config_loader import get_config
from application.core.config.runtime_settings_utils import (
    non_negative_float,
    non_negative_int,
    section_value,
)

#: 相似度告警阈值默认值。
#: 与 ``voice_drift_service.DRIFT_ALERT_THRESHOLD`` 是同一个数值，但**故意各写一份
#: 字面量** —— 反向 import 会造成循环依赖（voice_drift_service 已 import 本模块）。
#: 一致性由 ``test_voice_drift_llm_mode.test_default_threshold_matches_module_constant``
#: 钉住；一旦两边漂移，那条测试会红。
#:
#: ⚠ 第 86 批：0.75 → **0.50**（口径重标定，理由见 ``voice_drift_service`` 同名常量）。
DRIFT_ALERT_THRESHOLD_DEFAULT = 0.50

#: 定向修文触发阈值默认值。
#: ⚠ 第 89 批（N-24）：这个阈值**只有一个出处**（本设置 + 配置）——
#:   服务层用它算出「这一章值不值得花钱改写」，engine 层的两个常量只是**兜底**
#:   （服务给不出判定时用）⇒ 一致性由 ``test_voice_threshold_calibration`` 钉住。
#:
#: ⚠ 第 86 批：0.68 → **0.40**（口径重标定：健康章节的相似度在 0.5 附近，
#:   0.68 对健康章节命中率 100% ⇒ 每章都触发修文 ⇒ 白烧钱）。
VOICE_REWRITE_THRESHOLD_DEFAULT = 0.40


@dataclass(frozen=True)
class VoiceAnalysisRuntimeSettings:
    style_cache_ttl_seconds: float = 300.0
    style_cache_max_size: int = 512
    baseline_cache_ttl_seconds: float = 600.0
    baseline_cache_max_size: int = 256
    #: 文风漂移是否走 LLM 模式。默认 False = 统计模式（0 额外 token）。
    #: F-04：生产装配原本不传此参数，导致 LLM 模式从未启用。
    #: 开启后每章多一次 LLM 调用（约 500 token），以抓深层风格特征。
    use_llm_mode: bool = False
    #: 相似度告警阈值。模块常量 ``DRIFT_ALERT_THRESHOLD`` 作为默认值保留，
    #: 保留它是为了不破坏 ``daemon_host._similarity_below_warning_threshold``。
    drift_alert_threshold: float = DRIFT_ALERT_THRESHOLD_DEFAULT
    #: 定向修文触发阈值（第 89 批 · N-24：判定改**书内相对**后，
    #: 这个绝对阈值只在「历史不足、拿不到本书近期常态」时兜底）。
    voice_rewrite_threshold: float = VOICE_REWRITE_THRESHOLD_DEFAULT
    #: ⚠ 第 89 批（N-24）：**书内相对**判据 —— 低于本书近期常态多少算异常。
    #: 绝对阈值对长书「几乎每章都命中」（常态本身就在 0.5 附近）⇒ 改相对。
    drift_relative_drop_alert: float = 0.15
    #: 低于本书近期常态多少 ⇒ 值得花钱做定向修文（比告警更严，因为它要**烧调用**）。
    voice_rewrite_relative_drop: float = 0.25


def get_voice_analysis_runtime_settings() -> VoiceAnalysisRuntimeSettings:
    analyst = getattr(get_config(), "analyst", None)
    section = getattr(analyst, "voice_analysis", None)
    defaults = VoiceAnalysisRuntimeSettings()
    # ⚠ 第 74 批（Q-1）：走**统一读取层**（env 级可用）。
    #   ⚠ 第 79 批：**novel 级也生效了** —— 文风服务改成「每次读都解析」
    #     （``VoiceDriftService.use_llm_mode`` 变成属性）＋「LLM 文风服务懒创建」
    #     ⇒ 本书把 voice_llm_mode 设成 true 时，这本书的文风审计真的走 LLM。
    #     ⚠ env 级名 = ``PLOTPILOT_AI_VOICE_LLM_MODE``（按 ``PLOTPILOT_AI_<KEY>`` 推导）。
    from application.engine.services.ai_switch_layer import resolve_switch

    return VoiceAnalysisRuntimeSettings(
        style_cache_ttl_seconds=non_negative_float(
            section_value(
                section,
                "style_cache_ttl_seconds",
                defaults.style_cache_ttl_seconds,
            ),
            defaults.style_cache_ttl_seconds,
        ),
        style_cache_max_size=non_negative_int(
            section_value(section, "style_cache_max_size", defaults.style_cache_max_size),
            defaults.style_cache_max_size,
        ),
        baseline_cache_ttl_seconds=non_negative_float(
            section_value(
                section,
                "baseline_cache_ttl_seconds",
                defaults.baseline_cache_ttl_seconds,
            ),
            defaults.baseline_cache_ttl_seconds,
        ),
        baseline_cache_max_size=non_negative_int(
            section_value(
                section,
                "baseline_cache_max_size",
                defaults.baseline_cache_max_size,
            ),
            defaults.baseline_cache_max_size,
        ),
        use_llm_mode=resolve_switch(
            "voice_llm_mode",
            default=bool(section_value(section, "use_llm_mode", defaults.use_llm_mode)),
        ),
        drift_alert_threshold=non_negative_float(
            section_value(
                section, "drift_alert_threshold", defaults.drift_alert_threshold
            ),
            defaults.drift_alert_threshold,
        ),
        voice_rewrite_threshold=non_negative_float(
            section_value(
                section, "voice_rewrite_threshold", defaults.voice_rewrite_threshold
            ),
            defaults.voice_rewrite_threshold,
        ),
        drift_relative_drop_alert=non_negative_float(
            section_value(
                section,
                "drift_relative_drop_alert",
                defaults.drift_relative_drop_alert,
            ),
            defaults.drift_relative_drop_alert,
        ),
        voice_rewrite_relative_drop=non_negative_float(
            section_value(
                section,
                "voice_rewrite_relative_drop",
                defaults.voice_rewrite_relative_drop,
            ),
            defaults.voice_rewrite_relative_drop,
        ),
    )
