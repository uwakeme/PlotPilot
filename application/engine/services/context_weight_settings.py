"""D-02：上下文权重规划的运行时开关。

默认值全部保守：**开关默认关闭**。设计文档「默认值渐进切换」的要求是
先 shadow 观察 → 再默认 AI → 保留回退；关闭状态下分配器拿不到任何权重，
行为与改造前逐位一致。

配置位置：`config/performance.yaml` 的 `context_weights:` 段。
"""
from __future__ import annotations

from dataclasses import dataclass

from application.core.config.config_loader import get_config
from application.core.config.runtime_settings_utils import (
    non_negative_float,
    section_value,
)


@dataclass(frozen=True)
class ContextWeightRuntimeSettings:
    #: 总开关。关闭 ⇒ 不发起规划调用 ⇒ 分配器无权重（改造前行为）
    enabled: bool = False
    #: 是否只记录不生效（shadow 观察期）
    shadow: bool = True
    #: 权重结果的缓存时长（秒）。同一章短期内不重复调用
    cache_ttl_seconds: float = 900.0
    #: 规划失败后多久内不再重试（秒），避免每章都打一次上游
    failure_backoff_seconds: float = 300.0


def get_context_weight_runtime_settings() -> ContextWeightRuntimeSettings:
    section = getattr(get_config(), "context_weights", None)
    defaults = ContextWeightRuntimeSettings()
    return ContextWeightRuntimeSettings(
        enabled=bool(
            section_value(section, "enabled", defaults.enabled)
        ),
        shadow=bool(
            section_value(section, "shadow", defaults.shadow)
        ),
        cache_ttl_seconds=non_negative_float(
            section_value(
                section, "cache_ttl_seconds", defaults.cache_ttl_seconds
            ),
            defaults.cache_ttl_seconds,
        ),
        failure_backoff_seconds=non_negative_float(
            section_value(
                section, "failure_backoff_seconds", defaults.failure_backoff_seconds
            ),
            defaults.failure_backoff_seconds,
        ),
    )
