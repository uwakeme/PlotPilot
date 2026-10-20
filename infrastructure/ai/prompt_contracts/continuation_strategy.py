"""planning-continuation-strategy 能力契约（D-08）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.engine.services.continuation_strategy_schema import (
    ContinuationStrategyLlmPayload,
)
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import PLANNING_CONTINUATION_STRATEGY


class ContinuationStrategyVariables(BaseModel):
    """续写策略判定的输入。"""

    novel_id: str = Field(min_length=1, description="小说 ID")
    chapter_number: int = Field(ge=1, description="要写的章节号")
    chapter_title: str = Field(default="", description="章节标题")
    chapter_outline: str = Field(default="", description="本章大纲/执行剧本")
    draft_excerpt: str = Field(default="", description="上次中断稿的尾部若干字")
    draft_char_count: int = Field(default=0, description="上次中断稿的总字数")
    target_words: int = Field(default=0, description="本章目标字数")


CONTINUATION_STRATEGY_CONTRACT = PromptContract(
    node_key=PLANNING_CONTINUATION_STRATEGY,
    version="1.0.0",
    variables_schema=ContinuationStrategyVariables,
    output_schema=ContinuationStrategyLlmPayload,
    generation_profile="continuation_strategy",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
