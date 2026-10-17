"""planning-chapter-mode 能力契约（D-01）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.engine.services.chapter_mode_schema import ChapterModeLlmPayload
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import PLANNING_CHAPTER_MODE


class ChapterModeVariables(BaseModel):
    """章节节奏判定输入。"""

    novel_id: str = Field(min_length=1, description="小说 ID")
    chapter_number: int = Field(ge=1, description="即将创作的章节号")
    outline: str = Field(default="", description="本章大纲")
    last_tension: str = Field(default="（无数据）", description="上一章张力（0-100 刻度）")
    stage_hint: str = Field(default="未知", description="当前叙事阶段")
    due_debts: str = Field(default="（无）", description="到期的叙事债务")
    due_foreshadowings: str = Field(default="（无）", description="到期该回收的伏笔")


CHAPTER_MODE_CONTRACT = PromptContract(
    node_key=PLANNING_CHAPTER_MODE,
    version="1.0.0",
    variables_schema=ChapterModeVariables,
    output_schema=ChapterModeLlmPayload,
    generation_profile="chapter_mode",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
