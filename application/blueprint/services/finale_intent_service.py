"""B-07/B-13：幕级终局意图判定（AI 看全局判断「本幕是否该收束」）。

改造前这一幕的收束口径是**代码写死的两段文案**（见
``continuous_planning_service._act_budget_context``），它每次幕级规划与
每次建幕都会进模型 —— 创作判断被写成了常量。

本服务把「这一幕是不是全书该收束的地方」「这一幕往哪个方向写」交回 AI，
并遵守三条纪律：

1. **判不出来就是判不出来** —— 返回 ``is_finale_act=None``，不回落成 False；
2. **绝不代写** —— 模型没给方向就是空串，代码不补一句像样的收束方案；
3. **失败不假装** —— 调用失败、输出非 JSON、不合契约，一律按未评估处理并告警。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.finale_intent import FINALE_INTENT_CONTRACT
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.engine.services.ai_participation_settings import (
    ai_participation_enabled,
)
from application.blueprint.services.finale_intent_contract import (
    FinaleIntent,
    SOURCE_NONE,
    normalize_finale_intent,
)

logger = logging.getLogger(__name__)


class FinaleIntentService:
    """判定这一幕是不是全书该收束的地方，并给出本幕创作方向。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def resolve(
        self,
        *,
        novel_id: str,
        act_number: int,
        total_acts: int,
        target_chapters: int,
        act_title: str = "",
        act_description: str = "",
        previous_summary: str = "",
        is_last_act: bool = False,
    ) -> FinaleIntent:
        """返回一次终局意图判定；判不出来时 ``is_finale_act=None``、方向为空串。"""
        if not ai_participation_enabled("finale_intent"):
            logger.info(
                "AI 参与总闸关闭：终局意图按未判定处理 novel=%s act=%s",
                novel_id, act_number,
            )
            return FinaleIntent(None, "", SOURCE_NONE)
        try:
            rendered = get_prompt_gateway().render(
                FINALE_INTENT_CONTRACT,
                {
                    "novel_id": novel_id,
                    "act_number": int(act_number),
                    "total_acts": max(int(total_acts), 1),
                    "target_chapters": max(int(target_chapters), 1),
                    "act_title": act_title or "（未命名幕）",
                    "act_description": act_description or "（无）",
                    "previous_summary": previous_summary or "（无）",
                    "is_last_act_note": "是（全书最后一幕）" if is_last_act else "否",
                },
            )
            config = generation_config_from_profile("finale_intent")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning(
                "终局意图判定调用失败，按未判定处理 novel=%s act=%s: %s",
                novel_id, act_number, exc,
            )
            return FinaleIntent(None, "", SOURCE_NONE)

        data = self._parse(result.content)
        if data is None:
            return FinaleIntent(None, "", SOURCE_NONE)

        intent = normalize_finale_intent(data)
        if intent.source != "ai":
            logger.info(
                "终局意图未判定 novel=%s act=%s（is_finale_act=%r 方向长度=%d）",
                novel_id, act_number, intent.is_finale_act, len(intent.act_direction),
            )
        return intent

    @staticmethod
    def _parse(raw_text: str) -> Optional[dict]:
        """解析并按契约校验。走 ``PromptGateway.validate_output``。

        契约声明了 ``output_schema=FinaleIntentLlmPayload``，其中
        ``is_finale_act`` 是 ``Optional[bool]`` —— 模型给字符串 "true" 会被
        契约判为不合规（lax 强转会把含糊读成明确结论）。
        """
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("终局意图判定返回非 JSON，按未判定处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("终局意图判定返回的 JSON 不是对象，按未判定处理")
            return None
        try:
            validated = get_prompt_gateway().validate_output(
                FINALE_INTENT_CONTRACT, data
            )
        except Exception as exc:
            logger.warning("终局意图判定输出不合契约，按未判定处理: %s", exc)
            return None
        return {
            "is_finale_act": getattr(validated, "is_finale_act", None),
            "act_direction": getattr(validated, "act_direction", ""),
            "reason": getattr(validated, "reason", ""),
        }
