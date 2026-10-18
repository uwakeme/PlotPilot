"""D-01：本章节奏模式判定（是否余韵章）的 LLM 输出契约。

这块判断此前是代码用硬阈值做的（``legacy_writing_delegate.py:202-203``
``tension_100 >= 80``）—— 一个创作判断被写成了公式。本契约把它交回 AI。

三态
----
``needs_buffer`` 是 ``Optional[bool]``：

- ``True``  —— 本章应当是余韵章（消化上章冲击、让暗线在余波中浮现）
- ``False`` —— 本章应当正常推进
- ``None``  —— **判不出来**。调用方不得把它当成 ``False`` 之外的任何「折中值」，
  也不得回填成 ``False`` 再假装是 AI 的结论。

``reason`` 在任何一种情况下都必须填 —— 判不出来也要说为什么。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class ChapterModeLlmPayload(BaseModel):
    """planning-chapter-mode 节点的 LLM 输出。"""

    #: True=余韵章 / False=正常推进 / **None=判不出来**
    needs_buffer: Optional[bool] = Field(
        default=None,
        description="本章是否应为余韵章；判不出来时必须为 null，不得填默认值",
    )
    reason: str = Field(
        default="",
        description="判断依据；判不出来时说明为什么（信息不足/状态未知等）",
    )
    #: 触发判断的关键信号，供可观测与复盘
    signals: list[str] = Field(
        default_factory=list,
        description="本次判断主要依据了哪些信号（上章张力/叙事债务/伏笔到期等）",
    )

    @field_validator("needs_buffer", mode="before")
    @classmethod
    def _only_real_bool(cls, v: Any) -> Optional[bool]:
        """只接受**真正的** bool 或 null。

        pydantic 默认是 lax 模式，``"true"`` / ``"yes"`` / ``1`` 会被静默
        强转成 ``True``。那等于把模型的含糊输出读成一个斩钉截铁的「是」——
        正是本轮红线要消灭的东西：含糊 ≠ 明确结论。
        """
        if v is None or isinstance(v, bool):
            return v
        raise ValueError(
            f"needs_buffer 必须是布尔值或 null，收到 {type(v).__name__}: {v!r}"
        )

    @field_validator("signals", mode="before")
    @classmethod
    def _normalize_signals(cls, v: Any) -> list[str]:
        """归一化而不是整坨丢弃。

        模型偶尔会混进空串 / null / 数字。单条脏数据不该让**整次判定**
        作废（那会把「模型给了个能用的结论」误降级成「判不出来」），
        但脏项也绝不能原样流进产物 —— 去空白、剔非字符串、截断、去重。
        """
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        out: list[str] = []
        for item in v:
            text = str(item).strip()[:60] if not isinstance(item, str) else item.strip()[:60]
            if text and text not in out:
                out.append(text)
            if len(out) >= 6:
                break
        return out
