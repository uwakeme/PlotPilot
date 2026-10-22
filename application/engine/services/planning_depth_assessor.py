"""B-06：让 AI 自评「这本书的结构要铺到多细」。

分工
----
- **AI 决定**：推荐档位（``framework`` / ``partial`` / ``full``）+ 依据；
- **代码保证不越界**：档位必须在封闭枚举里；**章数上限由代码夹紧**
  （不许比上限更细 —— 那会让输出规模随章数一起炸）。

降级纪律
--------
调用失败 / 输出不合契约 / 没给 ⇒ **未评估** ⇒ 调用方用章数阈值那档
（与改造前**逐位一致**），来源标 ``threshold``。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.planning_depth import (
    PLANNING_DEPTH_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.engine.services.ai_participation_settings import (
    ai_participation_enabled,
)
from application.blueprint.services.planning_depth import DEPTHS

logger = logging.getLogger(__name__)


class PlanningDepthAssessor:
    """AI 自评规划深度。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def assess(
        self,
        *,
        genre: str = "",
        premise: str = "",
        target_chapters: int = 0,
    ) -> Optional[str]:
        """返回推荐档位；判不出来时返回 ``None``（调用方回落章数阈值）。"""
        if self._llm is None or not ai_participation_enabled("planning_depth"):
            return None
        try:
            rendered = get_prompt_gateway().render(
                PLANNING_DEPTH_CONTRACT,
                {
                    "genre": str(genre or "") or "（未提供）",
                    "premise": str(premise or "") or "（未提供）",
                    "target_chapters": str(int(target_chapters or 0)),
                },
            )
            config = generation_config_from_profile("planning_depth")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning("规划深度自评调用失败，按未评估处理: %s", exc)
            return None

        data = self._parse(result.content)
        if data is None:
            return None
        depth = str(data.get("recommended_depth") or "").strip().lower()
        if depth not in DEPTHS:
            logger.info("规划深度自评给了非法档位 %r，按未评估处理", depth)
            return None
        return depth

    @staticmethod
    def _parse(raw_text: str) -> Optional[Dict[str, Any]]:
        """解析并按契约校验。走 ``PromptGateway.validate_output``。"""
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("规划深度自评返回非 JSON，按未评估处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("规划深度自评返回的 JSON 不是对象，按未评估处理")
            return None
        try:
            get_prompt_gateway().validate_output(PLANNING_DEPTH_CONTRACT, data)
        except Exception as exc:
            logger.warning("规划深度自评输出不合契约，按未评估处理: %s", exc)
            return None
        return data
