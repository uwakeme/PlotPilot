"""planning-macro-calibration 能力契约（H-06）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.audit.services.macro_calibration_schema import MacroCalibrationLlmPayload
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import PLANNING_MACRO_CALIBRATION


class MacroCalibrationVariables(BaseModel):
    """叙事校准指令生成的输入。"""

    trait: str = Field(default="", description="被诊断的人设标签（可能多标签合并）")
    breakpoints_json: str = Field(default="", description="候选断点清单（JSON 文本）")


MACRO_CALIBRATION_CONTRACT = PromptContract(
    node_key=PLANNING_MACRO_CALIBRATION,
    version="1.0.0",
    variables_schema=MacroCalibrationVariables,
    output_schema=MacroCalibrationLlmPayload,
    generation_profile="macro_calibration",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
