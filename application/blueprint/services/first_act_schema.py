"""C-07：全书**首幕**的 LLM 输出契约。

daemon 动态补首幕时，标题/描述/建议章数此前**全部由代码写死**
（「第一幕 · 开端」「故事起始，建立世界观与主角目标」）——
那是创作判断被写成了常量。本契约把它交回 AI。

三态
----
- 给了 title / description ⇒ AI 的内容（代码只做不变量校验）；
- 缺一不可 ⇒ 整次作废（**不许**拿占位文案冒充创作内容，登记表 C-06）；
- `suggested_chapter_count` 是**预算**不是创作内容：模型没给（或给了非正数）
  ⇒ 退到本书预算，并标 ``chapter_count_source = "fallback"``。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class FirstActLlmPayload(BaseModel):
    """planning-first-act 节点的 LLM 输出。"""

    title: str = Field(default="", description="首幕标题")
    description: str = Field(default="", description="首幕描述（本幕要建立什么）")
    suggested_chapter_count: Optional[int] = Field(
        default=None,
        description="建议章数；模型没给时为 null，由代码退到本书预算",
    )
    reason: str = Field(default="", description="判断依据")

    @field_validator("suggested_chapter_count", mode="before")
    @classmethod
    def _only_real_ints(cls, v: Any) -> Optional[int]:
        """只接受**真正的**整数或 null（lax 模式会把 "12" 静默转成 12）。"""
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        raise ValueError(
            f"suggested_chapter_count 必须是整数或 null，收到 {type(v).__name__}: {v!r}"
        )
