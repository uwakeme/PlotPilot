"""foreshadow-selection 能力契约（D-03 P1）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.world.services.foreshadow_selection_schema import (
    ForeshadowSelectionLlmPayload,
)
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import FORESHADOW_SELECTION


class ForeshadowSelectionVariables(BaseModel):
    """伏笔按章取材的输入。"""

    novel_title: str = Field(default="", description="小说名称")
    chapter_number: int = Field(ge=0, description="章节号")
    chapter_outline: str = Field(default="", description="本章大纲/执行剧本")
    candidates: str = Field(default="", description="候选伏笔清单（JSON 文本）")


FORESHADOW_SELECTION_CONTRACT = PromptContract(
    node_key=FORESHADOW_SELECTION,
    version="1.0.0",
    variables_schema=ForeshadowSelectionVariables,
    output_schema=ForeshadowSelectionLlmPayload,
    generation_profile="foreshadow_selection",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
