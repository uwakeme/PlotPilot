"""B-07/B-13：幕级终局意图判定的 LLM 输出契约。

这块判断此前是**代码写死的**（``continuous_planning_service._act_budget_context``
里两段创作文案：收束分支的「本幕必须完成结局：核心冲突正面了结……最后一章
必须落在结局而非新悬念上」，以及非终局分支的对应文案）—— 创作判断被写成了
常量，而且落进每一次幕级规划与下一次建幕的提示词里。

三态
----
``is_finale_act`` 是 ``Optional[bool]``：

- ``True``  —— 这一幕应当是全书的收束幕
- ``False`` —— 这一幕是推进幕
- ``None``  —— **判不出来**。调用方不得把它当成 ``False``，
  也不得回填成 ``False`` 再假装这是 AI 的结论。

``act_direction`` 是本幕的创作方向（收束幕写收束方案，推进幕写推进方向）。
**空串是合法值** —— 模型没写就是没写，代码不得代写
（登记表三态红线：禁止在降级路径返回任何看起来正常的数值/内容）。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class FinaleIntentLlmPayload(BaseModel):
    """planning-finale-intent 节点的 LLM 输出。"""

    #: True=收束幕 / False=推进幕 / **None=判不出来**
    is_finale_act: Optional[bool] = Field(
        default=None,
        description="这一幕是否应为全书收束幕；判不出来时必须为 null，不得填默认值",
    )
    act_direction: str = Field(
        default="",
        description="本幕的创作方向：收束幕写收束方案，推进幕写推进方向",
    )
    reason: str = Field(
        default="",
        description="判断依据；判不出来时说明为什么（信息不足/状态未知等）",
    )

    @field_validator("is_finale_act", mode="before")
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
            f"is_finale_act 必须是布尔值或 null，收到 {type(v).__name__}: {v!r}"
        )
