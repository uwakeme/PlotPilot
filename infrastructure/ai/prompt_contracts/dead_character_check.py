"""evolution-dead-character-check 能力契约（H-01）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.evolution.services.dead_character_check_schema import (
    DeadCharacterCheckLlmPayload,
)
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import PLANNING_DEAD_CHARACTER_CHECK


class DeadCharacterCheckVariables(BaseModel):
    """死亡角色出场判定的输入。"""

    outline: str = Field(default="", description="本章大纲（会被截断）")
    candidates_json: str = Field(
        default="",
        description="候选死亡角色清单（JSON 文本）：character_id / 名字",
    )


DEAD_CHARACTER_CHECK_CONTRACT = PromptContract(
    node_key=PLANNING_DEAD_CHARACTER_CHECK,
    version="1.0.0",
    variables_schema=DeadCharacterCheckVariables,
    output_schema=DeadCharacterCheckLlmPayload,
    generation_profile="evolution_dead_character_check",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
