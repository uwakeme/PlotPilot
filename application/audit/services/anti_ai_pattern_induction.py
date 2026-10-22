"""F-06：让 AI 归纳「这本书特有的 AI 味表达」。

旧实现是**代码用固定模板 + 固定触发词做统计**（见
``anti_ai_learning.AI_STRUCTURAL_TEMPLATES`` 与那 7 个触发词）—— 那不是归纳，
而且「什么算 AI 味」本身是代码代做的语义判断。

现在：AI 看本章正文与已知命中，输出归纳出的模式；代码只做**不越界**的校验
（必须是字面关键词、必须在本章正文里确实出现、严重度在白名单内）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, List, Optional, Tuple

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.anti_ai_pattern_induction import (
    ANTI_AI_PATTERN_INDUCTION_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.engine.services.ai_participation_settings import (
    ai_participation_enabled,
)
from application.audit.services.anti_ai_pattern_induction_schema import (
    SOURCE_AI,
    SOURCE_NONE,
    InducedPattern,
    validate_induced_patterns,
)

logger = logging.getLogger(__name__)

#: 送进提示词的正文长度（字符）
_CONTENT_CHARS = 3000
#: 送进提示词的已知命中条数
_KNOWN_HITS = 12


class AntiAiPatternInducer:
    """AI 归纳本书特有的 AI 味表达。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def induce(
        self,
        *,
        chapter_number: int,
        content: str,
        known_hits: Optional[Iterable[Any]] = None,
        known_keywords: Optional[Iterable[str]] = None,
        novel_title: str = "",
    ) -> Tuple[List[InducedPattern], str]:
        """返回 (通过校验的归纳结果, 来源标记)。

        来源标记为 ``""`` 表示**未评估**（AI 不可用 / 输出不合契约）——
        此时**不产生任何模式**，绝不用代码代写的模式顶替。
        """
        if self._llm is None or not ai_participation_enabled("anti_ai_pattern_induction"):
            return [], SOURCE_NONE

        hits_text = ""
        try:
            parts = []
            for hit in list(known_hits or [])[:_KNOWN_HITS]:
                # 注意：``ClicheHit`` 的字段是 ``pattern`` / ``text``。
                # ``pattern_name`` / ``matched_text`` 是
                # ``application/engine/rules/stream_ac_scanner.py`` 的命中类型
                # —— 另一个扫描器，字段名不通用（写错 ⇒ 已知命中永远传不进提示词）。
                name = str(getattr(hit, "pattern", "") or "")
                matched = str(getattr(hit, "text", "") or "")
                if name or matched:
                    parts.append(f"- {name}：{matched[:30]}")
            hits_text = "\n".join(parts)
        except Exception:
            hits_text = ""

        try:
            rendered = get_prompt_gateway().render(
                ANTI_AI_PATTERN_INDUCTION_CONTRACT,
                {
                    "novel_title": str(novel_title or ""),
                    "chapter_number": int(chapter_number or 0),
                    "content": str(content or "")[:_CONTENT_CHARS],
                    "known_hits": hits_text,
                },
            )
            config = generation_config_from_profile("anti_ai_pattern_induction")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning("AI 归纳 AI 味模式失败，按未评估处理 ch=%s: %s", chapter_number, exc)
            return [], SOURCE_NONE

        data = self._parse(result.content)
        if data is None:
            return [], SOURCE_NONE
        patterns = validate_induced_patterns(
            data, content=content or "", known_keywords=known_keywords
        )
        return patterns, SOURCE_AI

    @staticmethod
    def _parse(raw_text: str) -> Optional[dict]:
        """解析并按契约校验。走 ``PromptGateway.validate_output``。"""
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("AI 味模式归纳返回非 JSON，按未评估处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("AI 味模式归纳返回的 JSON 不是对象，按未评估处理")
            return None
        try:
            get_prompt_gateway().validate_output(ANTI_AI_PATTERN_INDUCTION_CONTRACT, data)
        except Exception as exc:
            logger.warning("AI 味模式归纳输出不合契约，按未评估处理: %s", exc)
            return None
        return data
