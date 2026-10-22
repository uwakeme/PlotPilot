"""wuxia-style-audit 能力契约（E-04）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.audit.services.wuxia_style_audit_schema import (
    WuxiaStyleAuditLlmPayload,
)
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import PLANNING_WUXIA_STYLE_AUDIT


class WuxiaStyleAuditVariables(BaseModel):
    """武侠风格审的输入。"""

    content: str = Field(default="", description="本章正文（会被截断）")


WUXIA_STYLE_AUDIT_CONTRACT = PromptContract(
    node_key=PLANNING_WUXIA_STYLE_AUDIT,
    version="1.0.0",
    variables_schema=WuxiaStyleAuditVariables,
    output_schema=WuxiaStyleAuditLlmPayload,
    generation_profile="wuxia_style_audit",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
