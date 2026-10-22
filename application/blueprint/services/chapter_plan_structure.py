"""E-01b：章前剧本的**结构由 AI 选**（七段降为可选契约之一）。

改造前，「七段执行剧本」的**结构词汇表**是代码写死的
（``chapter_plan_renderer.render_chapter_execution_plan`` 把固定的七个字段
映射成固定的七个段标题）⇒ 两个后果：

1. 不适用的段也得占位 —— 模型没填就渲染成「（待补充）」
   （生产库实测 592 章里 14 章命中：「主角状态变化」空 14 次、
   「角色关键决策」空 9 次、「爽点/反转设计」空 9 次）；
2. 装不下七段的章节（纯对白章、闪回章、揭密章）**没有地方可写** ——
   创作判断被压成了一张固定表格。

现在结构由 AI 声明：模型可以在 ``chapter_plan.plan_sections`` 里自选段落，
不给就仍走标准七段（**逐位一致**）。

开关
----
``config/performance.yaml`` 的 ``chapter_plan_structure.enabled``
（**默认关闭**）⇒ 关闭时渲染与章前剧本提示词**逐位一致**。

关闭的理由与 E-06 同源：这一改会让**每一章**的细纲形状与正文提示词措辞一起变，
一次全量生效没法在小范围里观察质量 ⇒ 必须开关 + 灰度。

不变量（登记表 E-01 的「代码保留的不变量：大纲必须落地」）
--------------------------------------------------------
「落地」不再靠「七段固定」这个**形状**来保证，而靠两件事：

- **代码**：渲染结果**至少有一段有内容**（否则视为生成失败，不落空细纲）；
- **提示词**：正文按「**细纲里列出的段落/条目**」锚定（结构无关）。
"""
from __future__ import annotations

from dataclasses import dataclass

from application.core.config.config_loader import get_config
from application.core.config.runtime_settings_utils import section_value

#: 开关默认**关闭** ⇒ 行为与改造前逐位一致
DEFAULT_CHAPTER_PLAN_STRUCTURE_ENABLED = False

_TRUE_TOKENS = {"1", "true", "yes", "on", "y", "t"}
_FALSE_TOKENS = {"0", "false", "no", "off", "n", "f", ""}


@dataclass(frozen=True)
class ChapterPlanStructureSettings:
    """章前剧本结构的运行时开关。"""

    #: AI 是否可以自选段落结构（七段降为「可选契约之一」）
    enabled: bool = DEFAULT_CHAPTER_PLAN_STRUCTURE_ENABLED


def _as_bool(value: object, default: bool) -> bool:
    """把配置值读成布尔（字符串 ``"false"`` 必须是 False，不是 True）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_TOKENS:
            return True
        if text in _FALSE_TOKENS:
            return False
    return default


def get_chapter_plan_structure_settings() -> ChapterPlanStructureSettings:
    """读 ``config/performance.yaml`` 的 ``chapter_plan_structure:`` 段。"""
    section = getattr(get_config(), "chapter_plan_structure", None)
    defaults = ChapterPlanStructureSettings()
    return ChapterPlanStructureSettings(
        enabled=_as_bool(
            section_value(section, "enabled", defaults.enabled),
            defaults.enabled,
        ),
    )


def chapter_plan_structure_enabled() -> bool:
    """AI 是否可以自选章前剧本的段落结构。"""
    return get_chapter_plan_structure_settings().enabled


def plan_structure_prompt_flag() -> str:
    """给提示词模板用的开关标记。

    空串 = 关闭 ⇒ 模板里的 ``{% if plan_structure_enabled %}`` 整块不渲染
    ⇒ 提示词与改造前**逐位一致**。
    """
    return "1" if chapter_plan_structure_enabled() else ""
