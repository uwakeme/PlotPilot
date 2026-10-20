"""D-08：让 AI 判断「这章要不要接着上次中断的正文写」。

改造前这段是代码定的：preview ≥500 字就自动切「续写底稿」模式，并注入固定
话术「请从其结尾处自然续写至全章完成，不要重复已有内容」。那是**创作判断**
被写成了常量 —— 登记表 D-08 的判定是 C。

本模块把判断交回 AI：它看到底稿的**尾部**（断在哪）、本章大纲与目标字数后，
决定

- ``continue`` —— 接着写完，并给出**它自己写的**续写指令（从哪接、别重复什么）；
- ``rewrite``  —— 这稿子不值得接，整章重写。

降级纪律
--------
- 判不出来（``strategy=None``）⇒ 调用方**不注入底稿**（按全新生成处理）。
  绝不用一段代码代写的续写指令顶替 —— 那正是要消灭的东西。
- 调用失败 / 输出不合契约 ⇒ 同上，按未判定处理并告警。
- 底稿全文仍按原样进上下文（这是既有行为，不新增 token 成本）；
  判定调用只看尾部若干字。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.continuation_strategy import (
    CONTINUATION_STRATEGY_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.engine.services.continuation_strategy_schema import (
    ContinuationStrategy,
    SOURCE_NONE,
    normalize_continuation_strategy,
)

logger = logging.getLogger(__name__)

#: 判定时给 AI 看的底稿尾部字数（底稿全文仍照原样进正文上下文）
_DRAFT_EXCERPT_CHARS = 1200


class ContinuationStrategyPlanner:
    """判断上一章的中断稿该不该接着写。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def decide(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        chapter_title: str = "",
        chapter_outline: str = "",
        draft: str = "",
        draft_char_count: int = 0,
        target_words: int = 0,
    ) -> ContinuationStrategy:
        """返回一次续写策略判定；判不出来时 ``strategy=None``。"""
        draft_text = str(draft or "").strip()
        if not draft_text:
            return ContinuationStrategy(None, "", "", SOURCE_NONE)

        excerpt = draft_text[-_DRAFT_EXCERPT_CHARS:]
        try:
            rendered = get_prompt_gateway().render(
                CONTINUATION_STRATEGY_CONTRACT,
                {
                    "novel_id": str(novel_id),
                    "chapter_number": int(chapter_number),
                    "chapter_title": str(chapter_title or ""),
                    "chapter_outline": str(chapter_outline or ""),
                    "draft_excerpt": excerpt,
                    "draft_char_count": int(draft_char_count or len(draft_text)),
                    "target_words": int(target_words or 0),
                },
            )
            config = generation_config_from_profile("continuation_strategy")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning(
                "续写策略判定调用失败，按未判定处理 novel=%s ch=%s: %s",
                novel_id, chapter_number, exc,
            )
            return ContinuationStrategy(None, "", "", SOURCE_NONE)

        data = self._parse(result.content)
        if data is None:
            return ContinuationStrategy(None, "", "", SOURCE_NONE)

        strategy = normalize_continuation_strategy(data)
        if not strategy.is_usable:
            logger.info(
                "续写策略未判定 novel=%s ch=%s（strategy=%r）",
                novel_id, chapter_number, strategy.strategy,
            )
        return strategy

    @staticmethod
    def _parse(raw_text: str) -> Optional[dict]:
        """解析并按契约校验。走 ``PromptGateway.validate_output``。"""
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("续写策略判定返回非 JSON，按未判定处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("续写策略判定返回的 JSON 不是对象，按未判定处理")
            return None
        try:
            validated = get_prompt_gateway().validate_output(
                CONTINUATION_STRATEGY_CONTRACT, data
            )
        except Exception as exc:
            logger.warning("续写策略判定输出不合契约，按未判定处理: %s", exc)
            return None
        return {
            "strategy": getattr(validated, "strategy", None),
            "directive": getattr(validated, "directive", ""),
            "reason": getattr(validated, "reason", ""),
        }
