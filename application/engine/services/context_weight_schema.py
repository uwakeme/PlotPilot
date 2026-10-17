"""D-02：上下文槽位权重的 LLM 输出契约。

三态语义
--------
权重是「本章需要什么记忆」的判断。**判不出来就必须说判不出来** ——
给一个默认权重（比如全部 0.5）会被读成「AI 看过本章，认为各槽位同等重要」，
那与「没问过」完全不是一回事。故 `evaluated=False` 时 `weights` 为 `None`，
且必须带 `reason`。

槽位名与含义
------------
键名必须落在 `ContextBudgetAllocator` 实际注册的槽位名上（见
`SLOT_CATALOG`）。用错名字的键会被静默忽略，所以**宁可少给**。
"""
from __future__ import annotations

from typing import Dict, Optional

from pydantic import BaseModel, Field


class ContextWeightsLlmPayload(BaseModel):
    """planning-context-weights 节点的 LLM 输出。"""

    #: 槽位名 → 权重（0.0-1.0）。未评估时为 None，**不得**用默认值代替。
    weights: Optional[Dict[str, float]] = Field(
        default=None,
        description="槽位权重表；无法判断时必须为 null，而不是给一套默认值",
    )
    evaluated: bool = Field(
        default=False,
        description="是否真的做出了判断。false 表示「没判断出来」，不是「判断为平均」",
    )
    reason: str = Field(
        default="",
        description="判断依据；未评估时说明为什么判不出来",
    )
