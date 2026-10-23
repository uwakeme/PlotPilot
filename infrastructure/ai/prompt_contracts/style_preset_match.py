"""bible-style-preset-match 能力契约（J-02）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.world.services.style_preset_match_schema import (
    StylePresetMatchLlmPayload,
)
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import BIBLE_STYLE_PRESET_MATCH


class StylePresetMatchVariables(BaseModel):
    """文风预设语义匹配的输入。"""

    novel_id: str = Field(min_length=1, description="小说 ID")
    title: str = Field(default="", description="书名")
    genre: str = Field(default="", description="题材")
    premise: str = Field(default="", description="一句话梗概")
    worldbuilding_style: str = Field(default="", description="世界观风格要点")
    current_style_notes: str = Field(default="", description="当前已写的文风公约（可为空）")
    candidates: str = Field(default="", description="候选预设清单（题材特征）")


STYLE_PRESET_MATCH_CONTRACT = PromptContract(
    node_key=BIBLE_STYLE_PRESET_MATCH,
    version="1.0.0",
    variables_schema=StylePresetMatchVariables,
    output_schema=StylePresetMatchLlmPayload,
    generation_profile="style_preset_match",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
