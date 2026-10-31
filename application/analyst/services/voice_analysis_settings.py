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
DRIFT_ALERT_THRESHOLD_DEFAULT = 0.75


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


def get_voice_analysis_runtime_settings() -> VoiceAnalysisRuntimeSettings:
    analyst = getattr(get_config(), "analyst", None)
    section = getattr(analyst, "voice_analysis", None)
    defaults = VoiceAnalysisRuntimeSettings()
    # ⚠ 第 74 批（Q-1）：走**统一读取层**（env 级可用）。
    #   ⚠ 这一项的 novel 级**够不到**：文风服务是**进程级单例**（DI 工厂构造一次）
    #     ⇒ 「这本书」的覆盖在那里没有上下文 ⇒ 只有 env 级与默认级生效。
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
    )
