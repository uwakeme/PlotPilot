"""F-08：让 AI 分类剧情点（这一章算不算剧情点、是哪一类、怎么描述）。

代码给**证据**（本章张力、前章张力、张力变化、章纲与正文节选），AI 下**判断**。

降级纪律
--------
- 判不出来（调用失败 / 输出不合契约 / 标签不在封闭枚举内）⇒ **未评估**，
  **不生成剧情点** —— 不用旧的阈值表兜底（那正是要消灭的东西）。
- AI 明确判「本章不是剧情点」⇒ 也不生成，但那**是判定**（``source="ai"``），
  与「未评估」可分辨。
- 描述由 AI 写：代码不再代写「高潮：张力从 40 跃升至 85」这种文案。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.plot_point_classification import (
    PLOT_POINT_CLASSIFICATION_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.engine.services.ai_participation_settings import (
    ai_participation_enabled,
)
from application.world.services.plot_point_classifier_schema import (
    UNASSESSED,
    PlotPointVerdict,
    normalize_verdict,
)

logger = logging.getLogger(__name__)

#: 送给 AI 的正文节选长度（字符）——判断剧情点不需要全文
_CONTENT_EXCERPT_CHARS = 1500
#: 送给 AI 的章纲长度（字符）
_SUMMARY_CHARS = 600


class PlotPointClassifier:
    """剧情点分类（AI 语义判断）。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def classify(
        self,
        *,
        chapter_number: int,
        chapter_summary: str = "",
        content: str = "",
        tension_score: Optional[float] = None,
        prev_tension_score: Optional[float] = None,
        novel_title: str = "",
    ) -> PlotPointVerdict:
        """返回一次剧情点判定；判不出来时是**未评估**。"""
        if not ai_participation_enabled("plot_point_classifier"):
            logger.info(
                "AI 参与总闸关闭：剧情点分类按未评估处理 ch=%s", chapter_number
            )
            return UNASSESSED
        try:
            rendered = get_prompt_gateway().render(
                PLOT_POINT_CLASSIFICATION_CONTRACT,
                {
                    "novel_title": str(novel_title or ""),
                    "chapter_number": int(chapter_number or 0),
                    "chapter_summary": str(chapter_summary or "")[:_SUMMARY_CHARS],
                    "content_excerpt": str(content or "")[:_CONTENT_EXCERPT_CHARS],
                    "tension_score": "" if tension_score is None else f"{float(tension_score):.0f}",
                    "prev_tension_score": (
                        "" if prev_tension_score is None else f"{float(prev_tension_score):.0f}"
                    ),
                },
            )
            config = generation_config_from_profile("plot_point_classification")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning(
                "剧情点分类调用失败，按未评估处理 ch=%s: %s", chapter_number, exc
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
            logger.warning("剧情点分类返回非 JSON，按未评估处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("剧情点分类返回的 JSON 不是对象，按未评估处理")
            return None
        try:
            get_prompt_gateway().validate_output(PLOT_POINT_CLASSIFICATION_CONTRACT, data)
        except Exception as exc:
            logger.warning("剧情点分类输出不合契约，按未评估处理: %s", exc)
            return None
        return data
