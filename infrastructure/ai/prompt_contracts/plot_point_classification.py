"""plot-point-classification 能力契约（F-08）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.world.services.plot_point_classifier_schema import (
    PlotPointClassificationLlmPayload,
)
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import PLOT_POINT_CLASSIFICATION


class PlotPointClassificationVariables(BaseModel):
    """剧情点判定的输入。"""

    novel_title: str = Field(default="", description="小说名称")
    chapter_number: int = Field(ge=0, description="章节号")
    chapter_summary: str = Field(default="", description="本章章纲/摘要")
    content_excerpt: str = Field(default="", description="本章正文节选")
    tension_score: str = Field(default="", description="本章张力（0-100，未评估时为空）")
    prev_tension_score: str = Field(default="", description="前章张力（0-100，没有前章时为空）")


PLOT_POINT_CLASSIFICATION_CONTRACT = PromptContract(
    node_key=PLOT_POINT_CLASSIFICATION,
    version="1.0.0",
    variables_schema=PlotPointClassificationVariables,
    output_schema=PlotPointClassificationLlmPayload,
    generation_profile="plot_point_classification",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
