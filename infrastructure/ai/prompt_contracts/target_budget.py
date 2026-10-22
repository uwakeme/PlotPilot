"""planning-target-budget-proposal 能力契约（J-01）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.blueprint.services.target_budget_schema import (
    TargetBudgetProposalLlmPayload,
)
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import PLANNING_TARGET_BUDGET_PROPOSAL


class TargetBudgetProposalVariables(BaseModel):
    """目标章数 / 终局预算提案输入。"""

    novel_id: str = Field(min_length=1, description="小说 ID")
    title: str = Field(default="", description="书名")
    genre: str = Field(default="", description="题材")
    target_chapters: int = Field(ge=0, description="本书当前目标章数")
    completed_chapters: int = Field(ge=0, description="已完成章数")
    remaining_threads: str = Field(default="（未提供）", description="尚未收束的主要线索/伏笔")
    recent_digest: str = Field(default="（未提供）", description="最近几章的一句话梗概")
    current_finale_budget: int = Field(ge=0, description="当前已设的终局预算（章）")


TARGET_BUDGET_PROPOSAL_CONTRACT = PromptContract(
    node_key=PLANNING_TARGET_BUDGET_PROPOSAL,
    version="1.0.0",
    variables_schema=TargetBudgetProposalVariables,
    output_schema=TargetBudgetProposalLlmPayload,
    generation_profile="target_budget_proposal",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
