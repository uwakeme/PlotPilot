"""G-05：新角色发现的 LLM 输出契约。

三态
----
``characters`` 是**数组**（不是对象）：模型答不出就给空数组 ⇒
**未评估**（调用方回落正则 + 黑名单），而不是给一个编出来的名字。

每个角色必须带 ``evidence``（原文里能对上的一段话）⇒
代码会校验它**真的出现在大纲里** —— 模型的记忆不算数。
"""
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator


class CharacterNerEntry(BaseModel):
    """一个新角色候选。"""

    name: str = Field(default="", description="角色名（2-6 个字）")
    evidence: str = Field(default="", description="大纲里能对上这个角色的一段话")

    @field_validator("name", "evidence", mode="before")
    @classmethod
    def _as_text(cls, v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, (str, int, float)):
            return str(v).strip()
        return ""


class CharacterNerLlmPayload(BaseModel):
    """planning-character-ner 节点的 LLM 输出。"""

    characters: List[CharacterNerEntry] = Field(
        default_factory=list,
        description="本章出场、且不在已知角色/地点名单里的角色",
    )
    reason: str = Field(default="", description="判不出时说明为什么")
