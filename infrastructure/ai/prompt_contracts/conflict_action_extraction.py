"""conflict-action-extraction 能力契约（H-02b）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.audit.services.conflict_action_extraction_schema import (
    ConflictActionExtractionLlmPayload,
)
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import CONFLICT_ACTION_EXTRACTION


class ConflictActionExtractionVariables(BaseModel):
    """实体-动作对抽取的输入。"""

    novel_title: str = Field(default="", description="小说名称")
    chapter_number: int = Field(ge=0, description="章节号")
    outline: str = Field(default="", description="本章大纲")
    entities: str = Field(default="", description="候选实体名清单（JSON 文本）")


CONFLICT_ACTION_EXTRACTION_CONTRACT = PromptContract(
    node_key=CONFLICT_ACTION_EXTRACTION,
    version="1.0.0",
    variables_schema=ConflictActionExtractionVariables,
    output_schema=ConflictActionExtractionLlmPayload,
    generation_profile="conflict_action_extraction",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
