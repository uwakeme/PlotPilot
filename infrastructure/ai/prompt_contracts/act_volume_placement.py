"""planning-act-volume-placement 能力契约（B-08）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.blueprint.services.act_volume_placement_schema import (
    ActVolumePlacementLlmPayload,
)
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import PLANNING_ACT_VOLUME_PLACEMENT


class ActVolumePlacementVariables(BaseModel):
    """新一幕所属卷声明的输入。"""

    novel_id: str = Field(min_length=1, description="小说 ID")
    title: str = Field(default="", description="书名")
    genre: str = Field(default="", description="题材")
    premise: str = Field(default="", description="一句话梗概")
    volume_overview: str = Field(default="", description="全书各卷清单（卷号/卷名/卷简介/已有幕）")
    last_act: str = Field(default="", description="上一幕（最新一幕）的标题与简介")
    anchor_note: str = Field(default="", description="锚点卷说明与不可回填的约束")
    allowed_volume_numbers: str = Field(default="", description="允许选择的卷号清单")
    observed_acts_per_volume: str = Field(default="", description="已观测到的每卷幕数")


ACT_VOLUME_PLACEMENT_CONTRACT = PromptContract(
    node_key=PLANNING_ACT_VOLUME_PLACEMENT,
    version="1.0.0",
    variables_schema=ActVolumePlacementVariables,
    output_schema=ActVolumePlacementLlmPayload,
    generation_profile="act_volume_placement",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
