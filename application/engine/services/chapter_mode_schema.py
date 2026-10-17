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

from typing import Optional

from pydantic import BaseModel, Field


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
