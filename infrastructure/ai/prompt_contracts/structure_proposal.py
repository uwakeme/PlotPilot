"""planning-structure-proposal 能力契约（B-05）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.blueprint.services.structure_proposal_schema import (
    StructureProposalLlmPayload,
)
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import PLANNING_STRUCTURE_PROPOSAL


class StructureProposalVariables(BaseModel):
    """结构网格提案的输入。"""

    novel_id: str = Field(min_length=1, description="小说 ID")
    title: str = Field(default="", description="书名")
    genre: str = Field(default="", description="题材")
    premise: str = Field(default="", description="一句话梗概")
    target_chapters: str = Field(default="0", description="全书目标章数")
    max_parts: str = Field(default="10", description="部数上限（代码保留的不变量）")
    max_volumes_per_part: str = Field(default="10", description="每部卷数上限")
    max_acts_per_volume: str = Field(default="10", description="每卷幕数上限")
    max_climaxes_per_volume: str = Field(
        default="6", description="每卷高潮节点数上限（A-07：卷内节奏建议）"
    )


STRUCTURE_PROPOSAL_CONTRACT = PromptContract(
    node_key=PLANNING_STRUCTURE_PROPOSAL,
    version="1.0.0",
    variables_schema=StructureProposalVariables,
    output_schema=StructureProposalLlmPayload,
    generation_profile="structure_proposal",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
