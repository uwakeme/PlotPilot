"""E-06：让 AI 定「本章节奏配额」（目标张力 + 节奏档 + 具体指令）。

分工
----
- **AI 决定**：这一章该多紧（目标张力）、是爆发/升温/维持/缓冲、
  以及**一句具体可执行**的指令（不是空话）。
- **代码保证不越界**：``target_tension`` 必须是 0-100 的整数、
  ``rhythm`` 必须是封闭枚举里的四个词之一、``instruction`` 不得为空
  （见 ``rhythm_quota`` 的模块 docstring）。

降级纪律
--------
调用失败 / 输出不合契约 ⇒ 返回**未评估**（``source=""``），
由调用方回落**代码表**（改造前的行为，逐位一致），来源可分辨。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.rhythm_quota import RHYTHM_QUOTA_CONTRACT
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.engine.services.ai_participation_settings import (
    ai_participation_enabled,
)
from application.engine.services.rhythm_quota import (
    SOURCE_NONE,
    RhythmQuota,
    normalize_quota,
    rhythm_quota_delivery_enabled,
)

logger = logging.getLogger(__name__)


class RhythmQuotaPlanner:
    """AI 定本章的节奏配额。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def plan(
        self,
        *,
        genre: str = "",
        chapter_number: int = 0,
        prev_tension: Optional[float] = None,
        arc_tension: Optional[int] = None,
    ) -> RhythmQuota:
        """返回配额；判不出来时 ``source=""``（未评估，调用方回落代码表）。"""
        if self._llm is None or not ai_participation_enabled("rhythm_quota"):
            return RhythmQuota(source=SOURCE_NONE)
        # ⚠ **开关关着就别打模型**（第 3 批复审 S13）：
        #   `rhythm_quota.enabled` 管的是「配额文本送不送进正文提示词」。
        #   此前只在**投递侧**看开关 ⇒ 开关关着时仍然**付费调一次模型**、
        #   拿回来的结果再被丢掉 ⇒ 白花钱。
        #   现在在**调用侧**也看 ⇒ 关着就真的不打模型。
        if not rhythm_quota_delivery_enabled():
            logger.info("节奏配额开关关闭，跳过 AI 配额（不打模型）")
            return RhythmQuota(source=SOURCE_NONE)

        try:
            rendered = get_prompt_gateway().render(
                RHYTHM_QUOTA_CONTRACT,
                {
                    "genre": str(genre or "") or "（未提供）",
                    "chapter_number": str(int(chapter_number or 0)),
                    "prev_tension": (
                        f"{float(prev_tension):.0f}"
                        if prev_tension is not None
                        else "（未评估）"
                    ),
                    "arc_tension": (
                        str(int(arc_tension)) if arc_tension is not None else "（未评估）"
                    ),
                },
            )
            config = generation_config_from_profile("rhythm_quota")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning("节奏配额判定调用失败，按未评估处理: %s", exc)
            return RhythmQuota(source=SOURCE_NONE)

        data = self._parse(result.content)
        if data is None:
            return RhythmQuota(source=SOURCE_NONE)
        return normalize_quota(data)

    @staticmethod
    def _parse(raw_text: str) -> Optional[Dict[str, Any]]:
        """解析并按契约校验。走 ``PromptGateway.validate_output``。"""
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("节奏配额判定返回非 JSON，按未评估处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("节奏配额判定返回的 JSON 不是对象，按未评估处理")
            return None
        try:
            get_prompt_gateway().validate_output(RHYTHM_QUOTA_CONTRACT, data)
        except Exception as exc:
            logger.warning("节奏配额判定输出不合契约，按未评估处理: %s", exc)
            return None
        return data
