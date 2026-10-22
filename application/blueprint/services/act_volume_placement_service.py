"""B-08：让 AI 声明「即将创建的这一幕属于哪一卷」。

分工
----
- **AI 决定**：这一幕属于哪一卷（换卷时机）。
- **代码保证不越界**：声明的卷号必须落在允许集合内
  （**卷号单调、不回填**，见 ``act_volume_placement_contract``）。

降级纪律
--------
调用失败 / 输出非 JSON / 不合契约 / 判不出来 ⇒ 一律返回**未评估**
（``source="none"``、卷号 ``None``），由调用方回落**代码选卷策略**
（改造前的行为，逐位一致）。**代码不代写任何卷号** ——
「模型说没有」与「没评估」必须可分辨。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.act_volume_placement import (
    ACT_VOLUME_PLACEMENT_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.engine.services.ai_participation_settings import (
    ai_participation_enabled,
)
from application.blueprint.services.act_volume_placement_contract import (
    SOURCE_NONE,
    VolumePlacement,
    normalize_volume_placement,
)

logger = logging.getLogger(__name__)


class ActVolumePlacementService:
    """AI 声明这一幕属于哪一卷。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def declare(
        self,
        *,
        novel_id: str,
        title: str = "",
        genre: str = "",
        premise: str = "",
        volume_overview: str = "",
        last_act: str = "",
        anchor_note: str = "",
        allowed_volume_numbers: str = "",
        observed_acts_per_volume: str = "",
    ) -> VolumePlacement:
        """返回一次卷号声明；判不出来时卷号为 ``None``、来源 ``none``。"""
        if self._llm is None or not ai_participation_enabled("act_volume_placement"):
            return VolumePlacement(None, "", SOURCE_NONE)

        try:
            rendered = get_prompt_gateway().render(
                ACT_VOLUME_PLACEMENT_CONTRACT,
                {
                    "novel_id": str(novel_id),
                    "title": str(title or "") or "（未提供）",
                    "genre": str(genre or "") or "（未提供）",
                    "premise": str(premise or "") or "（未提供）",
                    "volume_overview": str(volume_overview or "") or "（未提供）",
                    "last_act": str(last_act or "") or "（未提供）",
                    "anchor_note": str(anchor_note or "") or "（未提供）",
                    "allowed_volume_numbers": str(allowed_volume_numbers or ""),
                    "observed_acts_per_volume": (
                        str(observed_acts_per_volume or "") or "（未评估）"
                    ),
                },
            )
            config = generation_config_from_profile("act_volume_placement")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning(
                "卷号声明调用失败，按未评估处理 novel=%s: %s", novel_id, exc
            )
            return VolumePlacement(None, "", SOURCE_NONE)

        data = self._parse(result.content)
        if data is None:
            return VolumePlacement(None, "", SOURCE_NONE)
        return normalize_volume_placement(data)

    @staticmethod
    def _parse(raw_text: str) -> Optional[Dict[str, Any]]:
        """解析并按契约校验。走 ``PromptGateway.validate_output``。"""
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("卷号声明返回非 JSON，按未评估处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("卷号声明返回的 JSON 不是对象，按未评估处理")
            return None
        try:
            validated = get_prompt_gateway().validate_output(
                ACT_VOLUME_PLACEMENT_CONTRACT, data
            )
        except Exception as exc:
            logger.warning("卷号声明输出不合契约，按未评估处理: %s", exc)
            return None
        # 归一化吃**契约校验后**的值：形状口径只有契约一处说了算
        # （否则「2.0 在契约里合法、在归一化里却判未评估」这类分叉会出现）。
        return validated.model_dump()
