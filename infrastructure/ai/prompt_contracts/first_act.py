"""planning-first-act 能力契约（C-07）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.blueprint.services.first_act_schema import FirstActLlmPayload
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import PLANNING_FIRST_ACT


class FirstActVariables(BaseModel):
    """首幕生成输入。"""

    novel_id: str = Field(min_length=1, description="小说 ID")
    title: str = Field(default="", description="书名")
    genre: str = Field(default="", description="题材")
    premise: str = Field(default="", description="一句话梗概（开书时用户给的）")
    volume_title: str = Field(default="", description="首幕将要挂载的卷")
    bible_digest: str = Field(default="", description="Bible 摘要（世界观/主角/冲突）")
    fallback_chapter_count: int = Field(ge=0, description="代码兜底的每幕建议章数（模型没给章数时用）")


FIRST_ACT_CONTRACT = PromptContract(
    node_key=PLANNING_FIRST_ACT,
    version="1.0.0",
    variables_schema=FirstActVariables,
    output_schema=FirstActLlmPayload,
    generation_profile="planning_first_act",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
