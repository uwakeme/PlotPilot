"""F-08：剧情点分类的取值口径与三态。

改造前是**代码用阈值表自动贴标签 + 代写描述**（`chapter_narrative_sync._auto_generate_plot_point`）：

- 张力跃升 ≥10 且 ≥75 ⇒ CLIMAX「高潮：张力从 40 跃升至 85」
- 张力跃升 ≥10 且 ≥55 ⇒ TURNING_POINT「转折：…」
- 张力跃升 ≥10 ⇒ RISING_ACTION「升温：…」
- 落差 ≥10 且前章 ≥65、现 <45 ⇒ FALLING_ACTION「回落：…」
- 落差 ≥10 ⇒ RESOLUTION「缓和：…」
- 现 ≥65 且前 <65 ⇒ CLIMAX「高峰：…」
- 连续两章 ≤30 ⇒ OPENING「持续低张：…（需注入冲突）」

「这一章算不算剧情点、算哪一类、该怎么描述」是**创作判断**，被写成了阈值表 ——
登记表 F-08 的判定是 C：**LLM 分类剧情点**。

本模块定义口径：

- **AI 判**：是不是剧情点、是哪一类、怎么描述（描述由 AI 写，代码不代写）。
- **标签枚举封闭**（6 值）：AI 给了枚举外的标签 ⇒ **未评估**
  （不回落成 ``rising`` —— 回落等于「代码替 AI 挑了一个标签」）。
- **三态**：判定成功 / 未评估（调用失败、输出不合契约、标签非法）。
  未评估 ⇒ **不生成剧情点**：不用阈值表兜底（那正是要消灭的东西）。
  AI 明确判「不是剧情点」与「未评估」可分辨：前者 ``source="ai"``，后者 ``source=""``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

#: 来源标记
SOURCE_AI = "ai"
SOURCE_NONE = ""

#: 剧情点类型（**封闭枚举** —— 与 ``domain.novel.value_objects.plot_point.PlotPointType`` 对齐）
PLOT_POINT_TYPES = (
    "opening",
    "rising",
    "turning",
    "climax",
    "falling",
    "resolution",
)


class PlotPointClassificationLlmPayload(BaseModel):
    """LLM 返回的结构化剧情点判定。"""

    is_plot_point: bool = Field(default=False)
    point_type: str = Field(default="")
    description: str = Field(default="")


@dataclass(frozen=True)
class PlotPointVerdict:
    """一次剧情点判定。"""

    is_plot_point: bool = False
    point_type: str = ""
    description: str = ""
    source: str = SOURCE_NONE

    @property
    def is_usable(self) -> bool:
        """是否做出了**判定**（判「不是剧情点」也算判定）。"""
        return self.source == SOURCE_AI


#: 未评估
UNASSESSED = PlotPointVerdict(False, "", "", SOURCE_NONE)


def normalize_verdict(data: Optional[Dict[str, Any]]) -> PlotPointVerdict:
    """把 LLM 输出收成判定结果；形状不对 ⇒ 未评估。"""
    if not isinstance(data, dict):
        return UNASSESSED
    is_point = data.get("is_plot_point")
    if not isinstance(is_point, bool):
        return UNASSESSED
    if not is_point:
        # AI 明确判「本章不是剧情点」—— 这是判定，不是未评估
        return PlotPointVerdict(False, "", "", SOURCE_AI)

    point_type = str(data.get("point_type") or "").strip()
    if point_type not in PLOT_POINT_TYPES:
        # 枚举封闭：非法标签 ⇒ 未评估（不回落成 rising）
        return UNASSESSED
    description = str(data.get("description") or "").strip()[:200]
    if not description:
        return UNASSESSED
    return PlotPointVerdict(True, point_type, description, SOURCE_AI)
