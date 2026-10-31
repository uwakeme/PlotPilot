"""Runtime settings for MemoryEngine state cache governance."""

from __future__ import annotations

from dataclasses import dataclass

from application.core.config.config_loader import get_config
from application.core.config.runtime_settings_utils import (
    non_negative_float,
    non_negative_int,
    section_value,
)


@dataclass(frozen=True)
class MemoryEngineRuntimeSettings:
    state_cache_ttl_seconds: float = 300.0
    state_cache_max_size: int = 256
    #: 第 71 批（R-6）：**落库回读确认**时等待写队列排空的超时（秒）。
    #:
    #: ⚠ 为什么要有：原值写死 2.0s ⇒ 队列繁忙时排不空 ⇒ 报「未确认」
    #:   ⇒ 但此时记忆**可能已经落库** ⇒ 这是**假失败**（方向比「假成功」安全，
    #:   但会在高负载下产生虚假的「未更新」条目）⇒ 放宽 + 可配。
    persist_verify_flush_timeout_seconds: float = 5.0


def get_memory_engine_runtime_settings() -> MemoryEngineRuntimeSettings:
    section = getattr(get_config(), "memory_engine", None)
    defaults = MemoryEngineRuntimeSettings()
    return MemoryEngineRuntimeSettings(
        state_cache_ttl_seconds=non_negative_float(
            section_value(
                section,
                "state_cache_ttl_seconds",
                defaults.state_cache_ttl_seconds,
            ),
            defaults.state_cache_ttl_seconds,
        ),
        state_cache_max_size=non_negative_int(
            section_value(
                section,
                "state_cache_max_size",
                defaults.state_cache_max_size,
            ),
            defaults.state_cache_max_size,
        ),
        persist_verify_flush_timeout_seconds=non_negative_float(
            section_value(
                section,
                "persist_verify_flush_timeout_seconds",
                defaults.persist_verify_flush_timeout_seconds,
            ),
            defaults.persist_verify_flush_timeout_seconds,
        ),
    )
