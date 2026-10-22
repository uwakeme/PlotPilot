"""planning-depth-assessment 能力契约（B-06）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import PLANNING_DEPTH_ASSESSMENT


class PlanningDepthVariables(BaseModel):
    """规划深度自评的输入。"""

    genre: str = Field(default="", description="题材")
    premise: str = Field(default="", description="一句话梗概")
    target_chapters: str = Field(default="0", description="目标总章数")


class PlanningDepthPayload(BaseModel):
    """planning-depth-assessment 节点的 LLM 输出。"""

    recommended_depth: str = Field(
        default="",
        description="推荐档位：framework（只铺到部/卷）| partial | full（铺到幕）",
    )
    reason: str = Field(default="", description="判断依据（可空）")


PLANNING_DEPTH_CONTRACT = PromptContract(
    node_key=PLANNING_DEPTH_ASSESSMENT,
    version="1.0.0",
    variables_schema=PlanningDepthVariables,
    output_schema=PlanningDepthPayload,
    generation_profile="planning_depth",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
