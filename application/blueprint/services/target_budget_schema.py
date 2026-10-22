"""J-01：目标章数 / 终局预算**提案**的 LLM 输出契约。

这块判断此前是代码写死的常量公式
（``target_budget.propose_continue_block`` = ``max(20, round(target * 0.2))``、
终局预算默认 12 章、上限 [5, 200]）—— 「这本书还差多少章才收得住」是
**创作判断**，被公式代答了。本契约把它交回 AI。

三态
----
- 给了具体数字 ⇒ AI 的提案（代码只做**不变量校验**）；
- 明确说「给不出」/ 字段为 null ⇒ **未评估**（调用方回落到常量公式，
  且来源标记为 ``fallback``，读端能分辨「这是 AI 说的」还是「公式兜的」）。

代码保留的不变量
----------------
- ``continue_block >= 1``：加写块不能是 0（否则「继续写」什么也不做）；
- ``finale_budget_min <= finale_budget <= finale_budget_max``：提案自身必须自洽；
- 终局预算仍被 domain 的 ``clamp_finale_budget``（[5, 200]）钳住 ——
  **AI 动不了这个上限**（那是产品常量，不是创作判断）。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

#: 提案来源标记：``ai`` = AI 的提案；``fallback`` = 引擎默认公式兜底。
#: 读端必须能分辨这两者 —— 「模型说没有」与「公式兜的」不许长得一样。
SOURCE_AI = "ai"
SOURCE_FALLBACK = "fallback"


class TargetBudgetProposalLlmPayload(BaseModel):
    """planning-target-budget-proposal 节点的 LLM 输出。"""

    #: 建议再加多少章（用户点「继续写」时的加写块）
    continue_block: Optional[int] = Field(
        default=None,
        description="建议再加多少章；判不出来时必须为 null，不得填默认值",
    )
    #: 终局预算（最后专门用来收束的章数）
    finale_budget: Optional[int] = Field(
        default=None,
        description="终局预算章数；判不出来时必须为 null",
    )
    #: 终局预算的允许下界
    finale_budget_min: Optional[int] = Field(
        default=None,
        description="终局预算允许的最小值；判不出来时必须为 null",
    )
    #: 终局预算的允许上界
    finale_budget_max: Optional[int] = Field(
        default=None,
        description="终局预算允许的最大值；判不出来时必须为 null",
    )
    #: 判断依据（任何情况下都要说；判不出来要说为什么）
    reason: str = Field(
        default="",
        description="提案依据；判不出来时说明原因（信息不足/剧情已收束等）",
    )

    @field_validator("continue_block", "finale_budget", "finale_budget_min", "finale_budget_max", mode="before")
    @classmethod
    def _only_real_ints(cls, v: Any) -> Optional[int]:
        """只接受**真正的**整数或 null。

        pydantic 的 lax 模式会把 ``"12"`` 静默转成 12、把 ``12.7`` 截断成 12。
        那是把模型的含糊输出读成一个斩钉截铁的数字。
        """
        if v is None or isinstance(v, bool):
            return v if v is None else None
        if isinstance(v, int):
            return v
        raise ValueError(
            f"章数必须是整数或 null，收到 {type(v).__name__}: {v!r}"
        )

    @model_validator(mode="after")
    def _bounds_are_self_consistent(self) -> "TargetBudgetProposalLlmPayload":
        """提案自身的区间必须自洽（min ≤ 值 ≤ max）。

        只在**四个数字都给全**时校验 —— 缺项是「未评估」，
        那是调用方回落公式的信号，不是「提案自相矛盾」。
        """
        values = (
            self.continue_block,
            self.finale_budget,
            self.finale_budget_min,
            self.finale_budget_max,
        )
        if any(v is None for v in values):
            return self
        if self.continue_block < 1:
            raise ValueError(f"continue_block 必须 >= 1，收到 {self.continue_block}")
        if self.finale_budget_min > self.finale_budget:
            raise ValueError(
                f"finale_budget_min({self.finale_budget_min}) > finale_budget({self.finale_budget})"
            )
        if self.finale_budget > self.finale_budget_max:
            raise ValueError(
                f"finale_budget({self.finale_budget}) > finale_budget_max({self.finale_budget_max})"
            )
        return self
