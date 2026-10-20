"""planning-finale-intent 能力契约（B-07/B-13）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.blueprint.services.finale_intent_schema import FinaleIntentLlmPayload
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import PLANNING_FINALE_INTENT


class FinaleIntentVariables(BaseModel):
    """幕级终局意图判定的输入。"""

    novel_id: str = Field(min_length=1, description="小说 ID")
    act_number: int = Field(ge=0, description="要规划的幕号")
    total_acts: int = Field(ge=1, description="全书结构总幕数")
    target_chapters: int = Field(ge=1, description="全书目标章数")
    act_title: str = Field(default="", description="这一幕的标题")
    act_description: str = Field(default="", description="这一幕的简介")
    previous_summary: str = Field(default="", description="前情提要")
    #: 结构事实的**文本**形式（"是（全书最后一幕）" / "否"）。
    #: 用字符串而不是 bool：这一项进提示词，模型读中文比读 True/False 准。
    is_last_act_note: str = Field(default="否", description="结构位置说明")


FINALE_INTENT_CONTRACT = PromptContract(
    node_key=PLANNING_FINALE_INTENT,
    version="1.0.0",
    variables_schema=FinaleIntentVariables,
    output_schema=FinaleIntentLlmPayload,
    generation_profile="finale_intent",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
