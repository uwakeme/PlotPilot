"""D-08：续写策略判定的输出契约与归一化。

改造前「是否续写、从哪续、怎么续」全部由代码决定：preview ≥500 字就自动切
「续写底稿」模式，并注入一段固定话术（"请从其结尾处自然续写至全章完成，
不要重复已有内容"）。登记表的判定是 **C（创作判断）** —— 该由 AI 决定。

三态
----
``strategy``：

- ``"continue"`` —— 接着写完
- ``"rewrite"``  —— 这稿子不值得接，整章重写
- ``None``      —— **判不出来**。调用方不得把它当成 ``"rewrite"``，
  也不得回填成 ``"continue"`` 再假装这是 AI 的结论。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

#: AI 可以给出的续写策略
STRATEGY_CONTINUE = "continue"
STRATEGY_REWRITE = "rewrite"
VALID_STRATEGIES = (STRATEGY_CONTINUE, STRATEGY_REWRITE)

SOURCE_AI = "ai"
SOURCE_NONE = "none"


@dataclass(frozen=True)
class ContinuationStrategy:
    """一次续写策略判定的结果。"""

    #: ``continue`` / ``rewrite`` / ``None``（未评估）
    strategy: Optional[str]
    #: 续写指令（仅 continue 时有意义）。空串 = 模型没写。
    directive: str
    #: 判断依据
    reason: str
    #: ``ai`` / ``none``
    source: str

    @property
    def is_usable(self) -> bool:
        """能不能拿这个判断去驱动续写。"""
        return self.source == SOURCE_AI and self.strategy is not None


class ContinuationStrategyLlmPayload(BaseModel):
    """planning-continuation-strategy 节点的 LLM 输出。"""

    #: continue=接着写完 / rewrite=整章重写 / **None=判不出来**
    strategy: Optional[str] = Field(
        default=None,
        description="续写策略；判不出来时必须为 null，不得填默认值",
    )
    directive: str = Field(
        default="",
        description="continue 时的续写指令：从哪里接、怎么接、不要重复什么",
    )
    reason: str = Field(
        default="",
        description="判断依据；判不出来时说明为什么（底稿太短/信息不足等）",
    )

    @field_validator("strategy", mode="before")
    @classmethod
    def _only_known_strategy(cls, v: Any) -> Optional[str]:
        """只接受 ``continue`` / ``rewrite`` / null。

        含糊的输出（``"resume"`` / ``true`` / ``1``）不是明确结论 ⇒ 判为未评估。
        """
        if v is None:
            return None
        if isinstance(v, str) and v.strip().lower() in VALID_STRATEGIES:
            return v.strip().lower()
        raise ValueError(
            f"strategy 必须是 continue / rewrite / null，收到 {type(v).__name__}: {v!r}"
        )


def normalize_continuation_strategy(raw: Any) -> ContinuationStrategy:
    """归一化模型的续写策略。**不编造**：判不出来就是判不出来。"""
    if not isinstance(raw, dict):
        return ContinuationStrategy(None, "", "", SOURCE_NONE)

    raw_strategy = raw.get("strategy")
    if raw_strategy is None:
        strategy: Optional[str] = None
    elif isinstance(raw_strategy, str) and raw_strategy.strip().lower() in VALID_STRATEGIES:
        strategy = raw_strategy.strip().lower()
    else:
        strategy = None

    raw_directive = raw.get("directive")
    directive = (
        raw_directive.strip()[:600] if isinstance(raw_directive, str) else ""
    )
    raw_reason = raw.get("reason")
    reason = raw_reason.strip()[:300] if isinstance(raw_reason, str) else ""

    source = SOURCE_AI if strategy is not None else SOURCE_NONE
    return ContinuationStrategy(strategy, directive, reason, source)
