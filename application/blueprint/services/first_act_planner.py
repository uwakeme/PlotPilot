"""C-07：全书**首幕**由 AI 决定（标题 / 描述 / 建议章数）。

daemon 动态补首幕时，这三项此前**全部由代码写死**
（「第一幕 · 开端」「故事起始，建立世界观与主角目标」+ 公式章数）——
创作判断被写成了常量。本模块把它交回 AI。

三态与来源
----------
- AI 给出 title + description ⇒ 用它（落库来源 ``AI_MACRO`` / ``CONFIRMED``）；
- 调用失败 / 输出不合契约 / 缺 title 或 description ⇒ 返回 ``None``，
  由调用方落**代码兜底**文案，且落库来源保持 ``DRAFT`` / ``MANUAL``
  ——**不许**把兜底文案标成「AI 规划好的」（那是落库后的假来源）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.first_act import FIRST_ACT_CONTRACT
from infrastructure.ai.prompt_gateway import get_prompt_gateway
from application.engine.services.ai_participation_settings import (
    ai_participation_enabled,
)

logger = logging.getLogger(__name__)


class FirstActPlanner:
    """让 AI 写全书第一幕。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def propose(
        self,
        *,
        novel_id: str,
        title: str = "",
        genre: str = "",
        premise: str = "",
        volume_title: str = "",
        bible_digest: str = "",
        fallback_chapter_count: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """返回 ``{"title", "description", "suggested_chapter_count", "reason"}``；
        拿不到可用结果返回 ``None``（调用方落代码兜底）。"""
        if not ai_participation_enabled("first_act"):
            logger.info("AI 参与总闸关闭：首幕提案按代码兜底处理 novel=%s", novel_id)
            return None
        try:
            rendered = get_prompt_gateway().render(
                FIRST_ACT_CONTRACT,
                {
                    "novel_id": str(novel_id),
                    "title": str(title or "") or "（未提供）",
                    "genre": str(genre or "") or "（未提供）",
                    "premise": str(premise or "") or "（未提供）",
                    "volume_title": str(volume_title or "") or "（未提供）",
                    "bible_digest": str(bible_digest or "") or "（未提供）",
                    "fallback_chapter_count": int(fallback_chapter_count or 0),
                },
            )
            config = generation_config_from_profile("planning_first_act")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning(
                "首幕生成调用失败，按代码兜底处理 novel=%s: %s", novel_id, exc
            )
            return None

        data = self._parse(result.content)
        if data is None:
            return None
        return {
            "title": data["title"],
            "description": data["description"],
            "suggested_chapter_count": data.get("suggested_chapter_count"),
            "reason": str(data.get("reason") or ""),
        }

    @staticmethod
    def _parse(raw_text: str) -> Optional[Dict[str, Any]]:
        """解析并按契约校验（走 ``PromptGateway.validate_output``）。"""
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("首幕生成返回非 JSON，按代码兜底处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("首幕生成返回的 JSON 不是对象，按代码兜底处理")
            return None
        try:
            validated = get_prompt_gateway().validate_output(FIRST_ACT_CONTRACT, data)
        except Exception as exc:
            logger.warning("首幕生成输出不合契约，按代码兜底处理: %s", exc)
            return None

        title = str(getattr(validated, "title", "") or "").strip()
        description = str(getattr(validated, "description", "") or "").strip()
        if not title or not description:
            # 创作内容缺一不可 ⇒ 整次作废（不许用占位文案冒充）
            logger.info(
                "首幕生成缺少 title/description，按代码兜底处理 title=%r",
                title,
            )
            return None
        return {
            "title": title,
            "description": description,
            "suggested_chapter_count": getattr(validated, "suggested_chapter_count", None),
            "reason": str(getattr(validated, "reason", "") or ""),
        }
