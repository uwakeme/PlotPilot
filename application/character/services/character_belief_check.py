"""H-03：让 AI 判断「这段行为是否与角色当前设定一致」。

改造前是**代码用关键词表 + 正则猜**的（见
``engine/core/value_objects/character_mask.py`` 的旧 ``validate_behavior``）：
信念关键词 → 固定正则；禁忌/旧伤用字面包含判。
那些是语义判断被写成了字符串匹配 —— 换个说法就漏报，「不再不信任」就误报。

现在判断整体交给 AI：它看到角色的核心信念 / 禁忌 / 旧伤 / 语体 + 待判定的
行为与场景，逐项判断是否冲突。

降级纪律
--------
- 判不出来（调用失败 / 输出不合契约）⇒ **未评估**
  （``evaluated=False``、``valid=None``）。
  **绝不返回 ``valid=True``** —— 「没查过」与「查过没问题」不是一回事，
  报成 True 就是假通过。
- 代码不再保留关键词表兜底：那正是要消灭的东西。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.character_belief_check import (
    CHARACTER_BELIEF_CHECK_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.character.services.character_belief_check_schema import (
    UNASSESSED,
    BehaviorVerdict,
    normalize_verdict,
)

logger = logging.getLogger(__name__)

#: 送进提示词的旧伤条数上限
_MAX_WOUNDS = 6


class CharacterBeliefChecker:
    """角色行为一致性判定（AI 语义判断）。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def check(
        self,
        *,
        character_name: str,
        action: str,
        context: str = "",
        core_belief: str = "",
        moral_taboos: Optional[Iterable[str]] = None,
        active_wounds: Optional[Iterable[Dict[str, Any]]] = None,
        voice_style: str = "",
        novel_title: str = "",
    ) -> BehaviorVerdict:
        """返回一次判定；判不出来时是**未评估**（``valid is None``）。"""
        action_text = str(action or "").strip()
        if not character_name or not action_text:
            return UNASSESSED

        taboos = [str(t).strip() for t in (moral_taboos or []) if str(t).strip()]
        wounds = [w for w in (active_wounds or []) if isinstance(w, dict)][:_MAX_WOUNDS]

        try:
            rendered = get_prompt_gateway().render(
                CHARACTER_BELIEF_CHECK_CONTRACT,
                {
                    "novel_title": str(novel_title or ""),
                    "character_name": str(character_name),
                    "core_belief": str(core_belief or ""),
                    "moral_taboos": "、".join(taboos),
                    "active_wounds": json.dumps(wounds, ensure_ascii=False),
                    "voice_style": str(voice_style or ""),
                    "action": action_text,
                    "context": str(context or ""),
                },
            )
            config = generation_config_from_profile("character_belief_check")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning(
                "角色行为一致性判定调用失败，按未评估处理 character=%s: %s",
                character_name, exc,
            )
            return UNASSESSED

        data = self._parse(result.content)
        if data is None:
            return UNASSESSED
        return normalize_verdict(data)

    @staticmethod
    def _parse(raw_text: str) -> Optional[dict]:
        """解析并按契约校验。走 ``PromptGateway.validate_output``。"""
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("角色行为一致性判定返回非 JSON，按未评估处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("角色行为一致性判定返回的 JSON 不是对象，按未评估处理")
            return None
        try:
            get_prompt_gateway().validate_output(CHARACTER_BELIEF_CHECK_CONTRACT, data)
        except Exception as exc:
            logger.warning("角色行为一致性判定输出不合契约，按未评估处理: %s", exc)
            return None
        return data
