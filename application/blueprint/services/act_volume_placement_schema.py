"""B-08：卷号声明的 LLM 输出契约。

三态
----
``volume_number`` 是 ``Optional[int]``：

- 正整数 —— 这一幕属于第几卷；
- ``None`` —— **判不出来**。调用方不得把它当成「留在当前卷」，
  也不得回填成某个默认卷再假装这是 AI 的结论。

模型多半会答成一句话（"第二卷"）而不是数字。契约把它**显式拒收**
而不是悄悄猜一个卷号 —— 猜出来的卷号会被当成 AI 的声明落库，
而那是一个谁都没说过的决定。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class ActVolumePlacementLlmPayload(BaseModel):
    """planning-act-volume-placement 节点的 LLM 输出。"""

    volume_number: Optional[int] = Field(
        default=None,
        description="这一幕所属的卷号（正整数）；判不出来时必须为 null",
    )
    reason: str = Field(
        default="",
        description="判断依据：一句话说明这一幕为什么属于这一卷",
    )

    @field_validator("volume_number", mode="before")
    @classmethod
    def _only_int_or_none(cls, v: Any) -> Optional[int]:
        """只接受**真正的**整数（或纯数字字符串）或 null。

        pydantic 的 lax 模式会把 ``True`` 读成 ``1``、把 ``"第2卷"`` 读成失败：
        前者是假卷号，后者是模型没按契约答 —— 两种都不该被当成有效声明。
        """
        if v is None:
            return None
        if isinstance(v, bool):
            raise ValueError(f"volume_number 不能是布尔值，收到 {v!r}")
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            text = v.strip()
            if text.isdigit():
                return int(text)
            raise ValueError(f"volume_number 必须是整数卷号，收到 {v!r}")
        raise ValueError(
            f"volume_number 必须是整数或 null，收到 {type(v).__name__}"
        )
