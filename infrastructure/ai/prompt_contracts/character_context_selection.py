"""character-context-selection 能力契约（D-04）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.character.services.character_context_selection_schema import (
    CharacterContextSelectionLlmPayload,
)
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import CHARACTER_CONTEXT_SELECTION


class CharacterContextSelectionVariables(BaseModel):
    """按章取材判定的输入。"""

    novel_title: str = Field(default="", description="小说名称")
    chapter_number: int = Field(ge=1, description="章节号")
    chapter_outline: str = Field(default="", description="本章大纲/执行剧本")
    characters: str = Field(default="", description="候选角色及其事实候选清单（JSON 文本）")


CHARACTER_CONTEXT_SELECTION_CONTRACT = PromptContract(
    node_key=CHARACTER_CONTEXT_SELECTION,
    version="1.0.0",
    variables_schema=CharacterContextSelectionVariables,
    output_schema=CharacterContextSelectionLlmPayload,
    generation_profile="character_context_selection",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
