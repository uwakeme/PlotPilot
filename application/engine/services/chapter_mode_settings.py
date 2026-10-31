"""D-01：章节节奏判定（是否余韵章）的运行时开关。

**独立于上下文权重开关** —— 两者是两项不同的创作决策，开一个不该顺带开另一个。

默认关闭 ⇒ ``_decide_chapter_mode`` 直接写 ``(False, "")``，
即保持改造前行为（StoryPipeline 从来没有过余韵章）。

配置位置：`config/performance.yaml` 的 `chapter_mode:` 段。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from application.core.config.config_loader import get_config
from application.core.config.runtime_settings_utils import section_value


@dataclass(frozen=True)
class ChapterModeRuntimeSettings:
    #: 总开关。关闭 ⇒ 不发起判定调用
    enabled: bool = False
    #: 余韵章指令是否拼进 outline。False = 只判定不施加（观察期）
    apply: bool = False


def get_chapter_mode_runtime_settings(
    novel_switches: Mapping[str, Any] | None = None,
) -> ChapterModeRuntimeSettings:
    """读本章的余韵章开关。

    ⚠ 第 74 批（Q-1）：走**统一读取层**（novel 级 > env 级 > 默认级）
      ⇒ 规格 §3.2 的三级优先级第一次**真的**存在了。
      ⇒ 默认级仍是 ``config/performance.yaml`` 那份 ⇒ **零行为变化**。
    """
    section = getattr(get_config(), "chapter_mode", None)
    defaults = ChapterModeRuntimeSettings()
    # X-02：总闸优先于逐条开关（总闸关闭 ⇒ 一律不发起调用）。
    from application.engine.services.ai_participation_settings import (
        ai_participation_enabled,
    )
    from application.engine.services.ai_switch_layer import resolve_switch

    return ChapterModeRuntimeSettings(
        enabled=resolve_switch(
            "chapter_mode.enabled",
            default=bool(section_value(section, "enabled", defaults.enabled)),
            novel_switches=novel_switches,
        )
        and ai_participation_enabled("chapter_mode"),
        apply=resolve_switch(
            "chapter_mode.apply",
            default=bool(section_value(section, "apply", defaults.apply)),
            novel_switches=novel_switches,
        ),
    )


#: 余韵章的节奏指令。与 legacy_writing_delegate 的拼接文本同义，
#: 但提到模块级常量，便于测试与日后按题材调整。
BUFFER_CHAPTER_DIRECTIVE = (
    "【余韵章：高潮余波】"
    "本章节奏适度放缓但不中断叙事势能——"
    "角色消化刚刚发生的重大冲击（震惊/损失/获得），"
    "周围势力对主角态度发生明显变化，"
    "新的暗线/线索/威胁在余波中悄然浮现。"
    "确保读者在喘息中依然保持期待，而不是「聊天喝茶」式断裂。"
)


def buffer_chapter_directive() -> str:
    return BUFFER_CHAPTER_DIRECTIVE
