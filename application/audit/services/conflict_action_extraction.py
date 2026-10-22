"""H-02b：让 AI 抽「实体-动作对」，代码只校验不越界。

分工
----
- **AI 决定**：这段大纲里，谁做了什么（用了哪一系魔法 / 哪件武器）。
- **代码保证不越界**：实体白名单（AI 不得凭空造人）、``kind`` 封闭枚举、
  属性值格式（魔法必须是「X系」——与设定库同口径）、证据必须字面出现在大纲里。

降级纪律
--------
- 调用失败 / 输出不合契约 / 给的全是废数据 ⇒ **未评估**（``source=""``），
  调用方按**兜底**处理：用代码关键词表（改造前的行为，逐位不变），
  但来源标记必须可分辨（``fallback``）。
- **AI 合法地说「这段没有魔法/武器动作」不算未评估** ⇒ 不回退。
  （模型说「没有」与「没问」必须可分辨。）
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, List, Optional, Tuple

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.conflict_action_extraction import (
    CONFLICT_ACTION_EXTRACTION_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.engine.services.ai_participation_settings import (
    ai_participation_enabled,
)
from application.audit.services.conflict_action_extraction_schema import (
    SOURCE_AI,
    SOURCE_NONE,
    ConflictActionExtraction,
    normalize_extraction,
)

logger = logging.getLogger(__name__)

#: 送进提示词的大纲长度（字符）
_OUTLINE_CHARS = 3000


class ConflictActionExtractor:
    """AI 抽取「实体-动作对」。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def extract(
        self,
        *,
        outline: str,
        entity_names: Iterable[str],
        novel_title: str = "",
        chapter_number: int = 0,
    ) -> ConflictActionExtraction:
        """返回抽取结果；判不出来时 ``source=""``（未评估）。"""
        if self._llm is None or not ai_participation_enabled("conflict_action_extraction"):
            return ConflictActionExtraction({}, SOURCE_NONE)

        names = [str(n).strip() for n in (entity_names or []) if str(n).strip()]
        if not names:
            # 没有候选实体 ⇒ 没什么可抽的（也谈不上评估）
            return ConflictActionExtraction({}, SOURCE_NONE)

        try:
            rendered = get_prompt_gateway().render(
                CONFLICT_ACTION_EXTRACTION_CONTRACT,
                {
                    "novel_title": str(novel_title or ""),
                    "chapter_number": int(chapter_number or 0),
                    "outline": str(outline or "")[:_OUTLINE_CHARS],
                    "entities": json.dumps(names, ensure_ascii=False),
                },
            )
            config = generation_config_from_profile("conflict_action_extraction")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning("冲突动作抽取调用失败，按未评估处理: %s", exc)
            return ConflictActionExtraction({}, SOURCE_NONE)

        data = self._parse(result.content)
        if data is None:
            return ConflictActionExtraction({}, SOURCE_NONE)
        return normalize_extraction(
            data, valid_entity_names=names, outline=str(outline or "")
        )

    @staticmethod
    def _parse(raw_text: str) -> Optional[dict]:
        """解析并按契约校验。走 ``PromptGateway.validate_output``。"""
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("冲突动作抽取返回非 JSON，按未评估处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("冲突动作抽取返回的 JSON 不是对象，按未评估处理")
            return None
        try:
            get_prompt_gateway().validate_output(
                CONFLICT_ACTION_EXTRACTION_CONTRACT, data
            )
        except Exception as exc:
            logger.warning("冲突动作抽取输出不合契约，按未评估处理: %s", exc)
            return None
        return data
