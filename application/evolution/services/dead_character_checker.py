"""H-01：让 AI 判「死亡角色是活跃登场，还是只是被想起」。

分工
----
- **AI 决定**：大纲里这个已死亡的角色，是在**当下时间线里行动**，
  还是只是**被回忆 / 被纪念 / 被提及**（遗物、传闻、旧事）。
- **代码保证不越界**：角色白名单（AI 不得凭空造人）、``verdict`` 封闭枚举、
  有结论必须有依据（见 ``dead_character_check_schema`` 的模块 docstring）。

改造前这条判断是**代码做的**：``_outline_mentions`` 命中名字即判「活跃登场」
⇒ 回忆 / 遗物一律误报（而且规则自己的建议「改为回忆/遗物」并不能满足它）。

降级纪律
--------
- 调用失败 / 输出不合契约 / 给的全是废数据 ⇒ **未评估**（``source=""``），
  调用方回落**规则判定**（命中即拦，与改造前逐位一致），来源可分辨（``rule``）。
- **AI 合法地说「只是回忆」不算未评估** ⇒ 不回退。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.dead_character_check import (
    DEAD_CHARACTER_CHECK_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.engine.services.ai_participation_settings import (
    ai_participation_enabled,
)
from application.evolution.services.dead_character_check_schema import (
    SOURCE_NONE,
    DeadCharacterCheck,
    normalize_dead_character_check,
)

logger = logging.getLogger(__name__)

#: 送进提示词的大纲长度上限（字符）
_OUTLINE_CHARS = 4000
#: 候选角色名保留的个数
_MAX_NAMES = 4


class DeadCharacterChecker:
    """AI 判「死亡角色的出场方式」。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def check(
        self,
        *,
        outline: str,
        candidates: List[Dict[str, Any]],
    ) -> DeadCharacterCheck:
        """返回检查结果；判不出来时 ``source=""``（未评估）。

        ``candidates``：``[{"character_id": ..., "names": [...]}]`` ——
        由闸门给出（「已死亡 **且** 大纲提到了」的角色）。
        """
        if self._llm is None or not ai_participation_enabled("dead_character_check"):
            return DeadCharacterCheck({}, SOURCE_NONE)

        payload = [
            {
                "character_id": str(c.get("character_id") or ""),
                "names": [str(n) for n in (c.get("names") or [])][:_MAX_NAMES],
            }
            for c in candidates
            if str(c.get("character_id") or "")
        ]
        if not payload:
            return DeadCharacterCheck({}, SOURCE_NONE)

        try:
            rendered = get_prompt_gateway().render(
                DEAD_CHARACTER_CHECK_CONTRACT,
                {
                    "outline": str(outline or "")[:_OUTLINE_CHARS],
                    "candidates_json": json.dumps(payload, ensure_ascii=False, indent=2),
                },
            )
            config = generation_config_from_profile("evolution_dead_character_check")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning("死亡角色出场判定调用失败，按未评估处理: %s", exc)
            return DeadCharacterCheck({}, SOURCE_NONE)

        data = self._parse(result.content)
        if data is None:
            return DeadCharacterCheck({}, SOURCE_NONE)
        return normalize_dead_character_check(
            data,
            valid_character_ids=[c["character_id"] for c in payload],
        )

    @staticmethod
    def _parse(raw_text: str) -> Optional[Dict[str, Any]]:
        """解析并按契约校验。走 ``PromptGateway.validate_output``。"""
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("死亡角色出场判定返回非 JSON，按未评估处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("死亡角色出场判定返回的 JSON 不是对象，按未评估处理")
            return None
        try:
            get_prompt_gateway().validate_output(DEAD_CHARACTER_CHECK_CONTRACT, data)
        except Exception as exc:
            logger.warning("死亡角色出场判定输出不合契约，按未评估处理: %s", exc)
            return None
        return data
