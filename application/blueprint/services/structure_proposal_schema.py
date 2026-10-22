"""B-05：结构网格提案的 LLM 输出契约。

三态
----
三个数字**都**必须是**正整数**才算可用；任一给不出 ⇒ 整次提案判为**未评估**
（调用方回落代码兜底网格 3×3×3），**不许**逐个字段猜。

⚠ ``True`` 不是 1：``bool`` 在 Python 里是 ``int`` 的子类，
不显式拒收的话模型答 ``true`` 会变成「1 部」。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class StructureProposalLlmPayload(BaseModel):
    """planning-structure-proposal 节点的 LLM 输出。"""

    parts: Optional[int] = Field(
        default=None, description="部数（正整数）；判不出来时必须为 null"
    )
    volumes_per_part: Optional[int] = Field(
        default=None, description="每部卷数（正整数）；判不出来时必须为 null"
    )
    acts_per_volume: Optional[int] = Field(
        default=None, description="每卷幕数（正整数）；判不出来时必须为 null"
    )
    reason: str = Field(default="", description="为什么这样切分（一句话）")
    # ── A-07：卷内节奏建议（并入同一个节点，省一次 LLM 调用） ─────────
    climaxes_per_volume: Optional[int] = Field(
        default=None,
        description="每卷高潮节点数（正整数）；判不出来时必须为 null",
    )
    hook_style: str = Field(
        default="",
        description="卷末钩子的具体形态（读者会问的那个问题或那个场面）",
    )

    @field_validator("parts", "volumes_per_part", "acts_per_volume", "climaxes_per_volume", mode="before")
    @classmethod
    def _only_int_or_none(cls, v: Any) -> Optional[int]:
        """只接受**真正的**整数（或纯数字字符串）或 null。

        pydantic 的 lax 模式会把 ``True`` 读成 ``1`` —— 那不是模型说的部数，
        是类型系统的顺手一猜。
        """
        if v is None:
            return None
        if isinstance(v, bool):
            raise ValueError(f"不能是布尔值，收到 {v!r}")
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            text = v.strip()
            if text.isdigit():
                return int(text)
            raise ValueError(f"必须是整数，收到 {v!r}")
        raise ValueError(f"必须是整数或 null，收到 {type(v).__name__}")
