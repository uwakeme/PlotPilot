"""anti-ai-pattern-induction 能力契约（F-06）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.audit.services.anti_ai_pattern_induction_schema import (
    AntiAiPatternInductionLlmPayload,
)
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import ANTI_AI_PATTERN_INDUCTION


class AntiAiPatternInductionVariables(BaseModel):
    """AI 味模式归纳的输入。"""

    novel_title: str = Field(default="", description="小说名称")
    chapter_number: int = Field(ge=0, description="章节号")
    content: str = Field(default="", description="本章正文节选")
    known_hits: str = Field(default="", description="本章已命中的已知 AI 味模式")


ANTI_AI_PATTERN_INDUCTION_CONTRACT = PromptContract(
    node_key=ANTI_AI_PATTERN_INDUCTION,
    version="1.0.0",
    variables_schema=AntiAiPatternInductionVariables,
    output_schema=AntiAiPatternInductionLlmPayload,
    generation_profile="anti_ai_pattern_induction",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
