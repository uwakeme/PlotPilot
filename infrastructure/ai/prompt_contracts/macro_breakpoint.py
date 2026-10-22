"""planning-macro-breakpoint 能力契约（H-06 子项 A）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.audit.services.macro_breakpoint_schema import (
    MacroBreakpointLlmPayload,
)
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import PLANNING_MACRO_BREAKPOINT


class MacroBreakpointVariables(BaseModel):
    """宏观断点语义诊断的输入。"""

    characters_json: str = Field(
        default="",
        description="角色清单（JSON 文本）：id / 人设标签 / 伤疤 / 执念 / 当前状态",
    )
    events_json: str = Field(
        default="",
        description="候选事件清单（JSON 文本）：event_id / 章节 / 摘要 / 标签",
    )


MACRO_BREAKPOINT_CONTRACT = PromptContract(
    node_key=PLANNING_MACRO_BREAKPOINT,
    version="1.0.0",
    variables_schema=MacroBreakpointVariables,
    output_schema=MacroBreakpointLlmPayload,
    generation_profile="macro_breakpoint",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
