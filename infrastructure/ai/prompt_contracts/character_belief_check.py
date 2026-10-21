"""character-belief-check 能力契约（H-03）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.character.services.character_belief_check_schema import (
    CharacterBeliefCheckLlmPayload,
)
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import CHARACTER_BELIEF_CHECK


class CharacterBeliefCheckVariables(BaseModel):
    """行为一致性判定的输入。"""

    novel_title: str = Field(default="", description="小说名称")
    character_name: str = Field(default="", description="角色名")
    core_belief: str = Field(default="", description="核心信念")
    moral_taboos: str = Field(default="", description="绝对禁忌（顿号分隔）")
    active_wounds: str = Field(default="", description="旧伤列表（JSON 文本）")
    voice_style: str = Field(default="", description="语言风格")
    action: str = Field(default="", description="待判定的行为描写")
    context: str = Field(default="", description="场景上下文")


CHARACTER_BELIEF_CHECK_CONTRACT = PromptContract(
    node_key=CHARACTER_BELIEF_CHECK,
    version="1.0.0",
    variables_schema=CharacterBeliefCheckVariables,
    output_schema=CharacterBeliefCheckLlmPayload,
    generation_profile="character_belief_check",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
