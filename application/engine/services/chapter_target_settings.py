"""D-05：每章字数目标的运行时开关。

默认关闭 ⇒ ``_apply_chapter_target_words`` 直接 return，
``ctx.target_word_count`` 完全不动（等于用户设定）。

配置位置：`config/performance.yaml` 的 `chapter_target:` 段。
"""
from __future__ import annotations

from dataclasses import dataclass

from application.core.config.config_loader import get_config
from application.core.config.runtime_settings_utils import (
    non_negative_float,
    section_value,
)

#: 默认允许 AI 提议偏离用户设定的比例
DEFAULT_MAX_DEVIATION_RATIO = 0.30


@dataclass(frozen=True)
class ChapterTargetRuntimeSettings:
    #: 总开关
    enabled: bool = False
    #: AI 提议可偏离用户设定的最大比例（0.30 = ±30%）
    max_deviation_ratio: float = DEFAULT_MAX_DEVIATION_RATIO


def get_chapter_target_runtime_settings() -> ChapterTargetRuntimeSettings:
    section = getattr(get_config(), "chapter_target", None)
    defaults = ChapterTargetRuntimeSettings()
    # X-02：总闸优先于逐条开关（总闸关闭 ⇒ 一律不发起调用）。
    from application.engine.services.ai_participation_settings import (
        ai_participation_enabled,
    )

    return ChapterTargetRuntimeSettings(
        enabled=bool(section_value(section, "enabled", defaults.enabled))
        and ai_participation_enabled("chapter_target"),
        max_deviation_ratio=non_negative_float(
            section_value(
                section, "max_deviation_ratio", defaults.max_deviation_ratio
            ),
            defaults.max_deviation_ratio,
        ),
    )
