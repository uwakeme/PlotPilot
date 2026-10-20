"""B-07/B-13：终局意图的归一化与来源标记。

范式照 A-01 的 ``subline_contract.py``：**兜底标记而不编造**。

- ``ai``   —— 模型给出了可用的判定（``is_finale_act`` 是真 bool）且方向文本非空
- ``none`` —— 模型没给，或给的东西不可用 ⇒ 方向为空，**代码绝不代写**

不变量
------
- ``is_finale_act`` 必须是真 bool 或 ``None``；``"true"`` / ``1`` 一律判为未评估
  （pydantic 的 lax 模式会把它们静默强转成 ``True``）
- ``act_direction`` 必须是字符串；非字符串一律丢弃（**不** ``str()`` 转成假文本 ——
  ``str(None)`` 会得到字面量 ``"None"``，那是把「模型没给」伪装成「模型说了 None」）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

SOURCE_AI = "ai"
SOURCE_NONE = "none"

#: 方向文本长度上限（防止模型写一整页塞进提示词）
MAX_DIRECTION_CHARS = 800


@dataclass(frozen=True)
class FinaleIntent:
    """一次终局意图判定的结果。"""

    #: True=AI 判定为收束幕 / False=AI 判定为推进幕 / None=未评估
    is_finale_act: Optional[bool]
    #: 本幕的创作方向（AI 原文）。空串 = 模型没给。
    act_direction: str
    #: ``ai`` / ``none``
    source: str

    @property
    def is_usable(self) -> bool:
        """能不能拿这个方向去注入提示词。"""
        return self.source == SOURCE_AI and bool(self.act_direction)


def normalize_finale_intent(raw: Any) -> FinaleIntent:
    """规范化模型的终局意图输出。**不编造**：给不出就是给不出。"""
    if not isinstance(raw, dict):
        return FinaleIntent(None, "", SOURCE_NONE)

    raw_flag = raw.get("is_finale_act")
    if raw_flag is None:
        is_finale_act: Optional[bool] = None
    elif isinstance(raw_flag, bool):
        is_finale_act = raw_flag
    else:
        # "true" / 1 / "yes" —— 含糊输出不是明确结论
        is_finale_act = None

    raw_direction = raw.get("act_direction")
    if isinstance(raw_direction, str):
        direction = raw_direction.strip()[:MAX_DIRECTION_CHARS]
    else:
        direction = ""

    source = SOURCE_AI if (is_finale_act is not None and direction) else SOURCE_NONE
    return FinaleIntent(is_finale_act, direction, source)
