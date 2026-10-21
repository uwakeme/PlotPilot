"""D-03 P1：让 AI 决定「这一章该推进哪几条伏笔、怎么推进」。

改造前是**代码按固定条数切**（领域层合格集），且取材发生在上下文组装阶段
—— 七段执行剧本在那之前就定稿 ⇒ 伏笔永远影响不了剧本。

现在：代码给**候选清单**（带稳定 id），AI 选；代码保证

- 候选 id 白名单（AI 不得凭空造伏笔）；
- **核心伏笔不得遗忘**（AI 漏选 ⇒ 代码补回并标记）；
- 三态：AI 不可用 ⇒ 回落领域层合格集（``source="fallback"``，来源可分辨）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, List, Optional, Sequence

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.foreshadow_selection import (
    FORESHADOW_SELECTION_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.world.services.foreshadow_selection_schema import (
    SOURCE_AI,
    SOURCE_NONE,
    ForeshadowCandidate,
    ForeshadowSelection,
    normalize_selection,
)

logger = logging.getLogger(__name__)


class ForeshadowSelectionPlanner:
    """伏笔按章取材（AI 语义判断）。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def select(
        self,
        *,
        chapter_number: int,
        candidates: Sequence[ForeshadowCandidate],
        chapter_outline: str = "",
        novel_title: str = "",
    ) -> ForeshadowSelection:
        """返回一次取材结果；判不出来时是**未评估**（``source=""``）。"""
        if not candidates:
            return ForeshadowSelection((), {}, SOURCE_NONE, ())

        payload = [
            {
                "id": c.id,
                "description": c.description,
                "planted_in_chapter": c.planted_in_chapter,
                "suggested_resolve_chapter": c.suggested_resolve_chapter,
                "is_core": c.is_core,
            }
            for c in candidates
        ]

        try:
            rendered = get_prompt_gateway().render(
                FORESHADOW_SELECTION_CONTRACT,
                {
                    "novel_title": str(novel_title or ""),
                    "chapter_number": int(chapter_number or 0),
                    "chapter_outline": str(chapter_outline or ""),
                    "candidates": json.dumps(payload, ensure_ascii=False),
                },
            )
            config = generation_config_from_profile("foreshadow_selection")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning(
                "伏笔取材调用失败，按未评估处理 ch=%s: %s", chapter_number, exc
            )
            return ForeshadowSelection((), {}, SOURCE_NONE, ())

        data = self._parse(result.content)
        if data is None:
            return ForeshadowSelection((), {}, SOURCE_NONE, ())
        return normalize_selection(data, candidates, source=SOURCE_AI)

    @staticmethod
    def _parse(raw_text: str) -> Optional[dict]:
        """解析并按契约校验。走 ``PromptGateway.validate_output``。"""
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("伏笔取材返回非 JSON，按未评估处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("伏笔取材返回的 JSON 不是对象，按未评估处理")
            return None
        try:
            get_prompt_gateway().validate_output(FORESHADOW_SELECTION_CONTRACT, data)
        except Exception as exc:
            logger.warning("伏笔取材输出不合契约，按未评估处理: %s", exc)
            return None
        return data
