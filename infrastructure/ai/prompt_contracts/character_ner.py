"""planning-character-ner 能力契约（G-05）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.character.services.character_ner_schema import CharacterNerLlmPayload
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import PLANNING_CHARACTER_NER


class CharacterNerVariables(BaseModel):
    """角色 NER 的输入。"""

    novel_id: str = Field(min_length=1, description="小说 ID")
    outline: str = Field(default="", description="本章大纲正文")
    known_names: str = Field(default="（无）", description="已在册的角色名（顿号分隔）")
    location_names: str = Field(default="（无）", description="已知的地点名（顿号分隔）")


CHARACTER_NER_CONTRACT = PromptContract(
    node_key=PLANNING_CHARACTER_NER,
    version="1.0.0",
    variables_schema=CharacterNerVariables,
    output_schema=CharacterNerLlmPayload,
    generation_profile="character_ner",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
