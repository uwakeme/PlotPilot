"""J-02：文风预设**语义匹配**的结果口径与三态。

改造前
------
「这本书该用哪条文风公约」只有两个来源：
1. 作者在面板上**手选**；
2. 前端按关键词**打分**猜一个（``matchPresetValue``）—— 只管显示标签，
   不写进 Bible。

⇒ 登记表 J-02 要的那一步（「AI 语义匹配到预设」）**根本不存在**。

现在
----
AI 看题材/梗概/世界观/已有文风，在**封闭的预设清单**里挑一个（或明说都不匹配）。

三态（**不许编**）
-----------------
- ``source="ai"`` + ``value="xianxia_hot"`` ⇒ AI 匹配到了；
- ``source="ai"`` + ``value=None`` ⇒ **AI 明确判「都不匹配」**（这是判定）；
- ``source=""`` ⇒ **未评估**（调用失败 / 输出不合契约 / 返回了清单外的值 /
  总闸关闭）—— 与上一条**必须可分辨**。

⚠ **匹配不出来时不许回落成「第一个预设」** ——
那正是第 4 批删掉的「默认注入 ``presets[0]」那条（前端替作者定文风，
而它会具名进正文提示词 ⇒ 确实生效）。回落等于把那条**复活**。
⇒ 判不出来就是 ``value=None`` + 一个说人话的 ``reason``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

#: 来源标记
SOURCE_AI = "ai"
SOURCE_NONE = ""


class StylePresetMatchLlmPayload(BaseModel):
    """LLM 返回的结构化匹配结果。

    ⚠ ``value`` 只是**标识符**，不是正文 ——
    正文由代码从清单里取（模型给什么就写什么 ⇒ 代码替模型说话）。
    空串 / ``null`` = 「都不匹配」。

    ⚠ 复审 S8：``value`` 必须是 **Optional** ——
    此前它是 ``str`` ⇒ 模型写 ``{"value": null, "reason": "都不匹配"}``
    会**过不了 pydantic** ⇒ 落成「未评估」⇒
    **「AI 说都不匹配」这一态几乎不可达**（而 ``null`` 是模型更常用的写法）。
    ⇒ 两种写法都收。
    """

    value: Optional[str] = Field(default=None, description="预设标识符；都不匹配时为 null 或空串")
    reason: Optional[str] = Field(default=None, description="依据 / 为什么不匹配")


@dataclass(frozen=True)
class StylePresetSuggestion:
    """一次文风预设推荐。"""

    #: 命中的预设标识符；``None`` = 没有匹配（**不回落成第一个**）
    value: Optional[str] = None
    #: 命中预设的显示名（未命中时为空）
    label: str = ""
    #: 命中预设的图标（未命中时为空）
    icon: str = ""
    #: 命中预设的文风公约正文（未命中时为空）
    body: str = ""
    #: 为什么（AI 给的；未评估时是代码给的降级说明）
    reason: str = ""
    #: ``ai`` / ``""``（未评估）
    source: str = SOURCE_NONE

    @property
    def matched(self) -> bool:
        """是否**匹配到了**（AI 判了、且给出了合法预设）。"""
        return self.source == SOURCE_AI and bool(self.value)

    @property
    def assessed(self) -> bool:
        """AI 是否**做出了判定**（判「都不匹配」也算判定）。"""
        return self.source == SOURCE_AI


def normalize_suggestion(
    data: Optional[Dict[str, Any]],
    *,
    preset_lookup,
) -> StylePresetSuggestion:
    """把 LLM 输出收成推荐结果。

    - 输出形状不对 ⇒ **未评估**；
    - ``value`` 不在清单里 ⇒ **未评估**（枚举封闭，不回落成第一个）；
    - ``value`` 为空串 + 给了理由 ⇒ **AI 明确判「都不匹配」**；
    - ``value`` 为空串 + 没给理由 ⇒ 什么都没说 ⇒ **未评估**。
    """
    if not isinstance(data, dict):
        return StylePresetSuggestion(source=SOURCE_NONE, reason="模型输出不是对象，未评估")

    raw_value = str(data.get("value") or "").strip()
    reason = str(data.get("reason") or "").strip()[:400]

    if not raw_value:
        if not reason:
            return StylePresetSuggestion(
                source=SOURCE_NONE, reason="模型没有给出匹配结果，也没有说明原因，未评估"
            )
        return StylePresetSuggestion(value=None, reason=reason, source=SOURCE_AI)

    preset = preset_lookup(raw_value)
    if preset is None:
        return StylePresetSuggestion(
            source=SOURCE_NONE,
            reason=f"模型返回了清单外的预设「{raw_value}」，未评估",
        )

    return StylePresetSuggestion(
        value=preset.value,
        label=preset.label,
        icon=preset.icon,
        body=preset.body,
        reason=reason,
        source=SOURCE_AI,
    )
