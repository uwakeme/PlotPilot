"""planning-rhythm-quota 能力契约（E-06）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from application.engine.services.rhythm_quota import RhythmQuotaLlmPayload
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_keys import PLANNING_RHYTHM_QUOTA


class RhythmQuotaVariables(BaseModel):
    """节奏配额判定的输入。"""

    genre: str = Field(default="", description="题材")
    chapter_number: str = Field(default="0", description="本章章号")
    prev_tension: str = Field(default="", description="前章实际张力（未评估时写「未评估」）")
    arc_tension: str = Field(default="", description="预设期望张力（未评估时写「未评估」）")


RHYTHM_QUOTA_CONTRACT = PromptContract(
    node_key=PLANNING_RHYTHM_QUOTA,
    version="1.0.0",
    variables_schema=RhythmQuotaVariables,
    output_schema=RhythmQuotaLlmPayload,
    generation_profile="rhythm_quota",
    target_models=("claude-3.5-sonnet", "gpt-4.1-mini"),
)
