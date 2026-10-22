"""D-04：让 AI 决定「这一章该带这个角色的哪几条记忆进上下文」。

改造前这段是代码按固定条数切事实（``scars[:2]`` / ``motivations[:2]`` / …），
且 T2 措辞对所有角色一字不差 —— 「这一章该给模型看哪几条」是**创作判断**，
被写成了常量。登记表 D-04 的判定是 C。

分工
----
- **代码给候选**（``CharacterContextCompiler.build_candidates``），AI **只挑不新增**；
- **代码保证不越界**：候选 id 白名单校验（AI 不得凭空造记忆）、
  角色锚点必进（名字/公开面/核心信念/禁忌/当前状态）、
  T2 的 ``- {名字}：`` 前缀由代码拼（AI 只写后半句）；
- **AI 决定**：哪些事实进上下文、每个角色的 T2 措辞。

降级纪律
--------
- 判不出来 / 调用失败 / 输出不合契约 ⇒ 未评估（``source=""``），
  调用方按**兜底**处理：用代码固定切片（改造前的行为，逐位不变），
  但来源标记必须可分辨（``fallback``），**不进 prompt 文本**。
- 绝不用代码代写的措辞顶替 AI 的判断后又声称「AI 选过」。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.character_context_selection import (
    CHARACTER_CONTEXT_SELECTION_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.engine.services.ai_participation_settings import (
    ai_participation_enabled,
)
from application.character.services.character_context_selection_schema import (
    CharacterContextSelection,
    SOURCE_NONE,
    normalize_selection,
)

logger = logging.getLogger(__name__)

#: 送给 AI 的单个候选描述上限（字符）——防 prompt 膨胀
_CANDIDATE_TEXT_LIMIT = 160


class CharacterContextSelector:
    """AI 取材调用：给候选清单，返回取材结果。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def select(
        self,
        *,
        novel_title: str = "",
        chapter_number: int = 0,
        chapter_outline: str = "",
        candidates: List[Dict[str, Any]],
    ) -> CharacterContextSelection:
        """返回一次取材结果；判不出来时 ``source=""``（未评估）。"""
        usable = [c for c in candidates if c.get("fact_ids")]
        if not usable:
            return CharacterContextSelection({}, SOURCE_NONE)

        valid_fact_ids = {
            str(c.get("character_id") or ""): set(str(x) for x in (c.get("fact_ids") or []))
            for c in usable
        }
        if not ai_participation_enabled("character_context_selection"):
            logger.info(
                "AI 参与总闸关闭：角色上下文取材按未评估处理 ch=%s",
                chapter_number,
            )
            return CharacterContextSelection({}, SOURCE_NONE)
        try:
            rendered = get_prompt_gateway().render(
                CHARACTER_CONTEXT_SELECTION_CONTRACT,
                {
                    "novel_title": str(novel_title or ""),
                    "chapter_number": int(chapter_number or 0),
                    "chapter_outline": str(chapter_outline or ""),
                    "characters": json.dumps(usable, ensure_ascii=False),
                },
            )
            config = generation_config_from_profile("character_context_selection")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning(
                "角色上下文取材调用失败，按未评估处理 ch=%s: %s",
                chapter_number, exc,
            )
            return CharacterContextSelection({}, SOURCE_NONE)

        data = self._parse(result.content)
        if data is None:
            return CharacterContextSelection({}, SOURCE_NONE)
        return normalize_selection(data, valid_fact_ids=valid_fact_ids)

    @staticmethod
    def _parse(raw_text: str) -> Optional[dict]:
        """解析并按契约校验。走 ``PromptGateway.validate_output``。"""
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("角色上下文取材返回非 JSON，按未评估处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("角色上下文取材返回的 JSON 不是对象，按未评估处理")
            return None
        try:
            get_prompt_gateway().validate_output(CHARACTER_CONTEXT_SELECTION_CONTRACT, data)
        except Exception as exc:
            logger.warning("角色上下文取材输出不合契约，按未评估处理: %s", exc)
            return None
        return data


class CharacterContextSelectionService:
    """把「选角 → 取 projection → 造候选 → 调 AI」串起来。

    依赖三个现成设施：
    - ``kernel``（``CharacterNarrativeKernel``）：本章选角
    - ``projection_service``（``CharacterProjectionService``）：角色记忆 projection
    - ``llm_service``：AI 调用
    """

    def __init__(
        self,
        *,
        llm_service: LLMService,
        allocator: Any,
    ) -> None:
        self._llm = llm_service
        self._allocator = allocator
        self._selector = CharacterContextSelector(llm_service)

    async def select_for_chapter(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        outline: str = "",
        novel_title: str = "",
        scene_director: Optional[Dict[str, Any]] = None,
    ) -> CharacterContextSelection:
        """返回本章的取材结果；任何一环失败 ⇒ 未评估（``source=""``）。"""
        try:
            candidates = self._allocator.build_character_context_candidates(
                novel_id, chapter_number, outline, scene_director=scene_director
            )
        except Exception as exc:
            logger.warning("角色上下文取材：候选构建失败，按未评估处理 ch=%s: %s", chapter_number, exc)
            return CharacterContextSelection({}, SOURCE_NONE)

        if not candidates:
            return CharacterContextSelection({}, SOURCE_NONE)

        return await self._selector.select(
            novel_title=novel_title,
            chapter_number=int(chapter_number or 0),
            chapter_outline=outline or "",
            candidates=candidates,
        )
