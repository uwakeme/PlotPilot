"""planning-context-weights 能力契约（D-02）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.engine.services.context_weight_schema import (
    ContextWeightsLlmPayload,
)
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import PLANNING_CONTEXT_WEIGHTS


class ContextWeightsVariables(BaseModel):
    """槽位权重规划输入。"""

    novel_id: str = Field(min_length=1, description="小说 ID")
    chapter_number: int = Field(ge=1, description="即将创作的章节号")
    chapter_outline: str = Field(default="", description="本章大纲/梗概")
    phase_hint: str = Field(default="未知", description="当前叙事阶段，如 开篇/发展/收敛/终局")
    slot_catalog: str = Field(
        default="", description="可选槽位清单（由 SLOT_CATALOG 生成，含 key/层级/用途）"
    )
    recent_digest: str = Field(default="", description="最近若干章的一句话摘要，帮助判断上下文侧重")


CONTEXT_WEIGHTS_CONTRACT = PromptContract(
    node_key=PLANNING_CONTEXT_WEIGHTS,
    version="1.0.0",
    variables_schema=ContextWeightsVariables,
    output_schema=ContextWeightsLlmPayload,
    generation_profile="context_weights",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
