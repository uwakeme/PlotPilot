"""G-05：新角色发现 —— **LLM 做 NER**（正则 + 黑名单作兜底）。

改造前
------
「本章出现了哪些不在 Bible 里的角色」是**代码用正则猜**的：
``re.findall(r"[一-龥]{2,4}", outline)`` ⇒ 任意 2-4 个汉字都成了人名候选
（「他转身」「雨水」也是候选）。

而且那条「显式出场名单」分支（``scene_director['characters']``）
在生产上**从未被触发** —— 两个路由都硬传 ``scene_director=None``
⇒ 登记表说的「LLM 做 NER」其实是**待接线**的目标。

现在
----
- **AI 做 NER**：新节点 ``planning-character-ner`` 读本章大纲，
  给出「本章出场、且不在 Bible 里的角色」名单（带证据）；
- **证据必须在大纲里真出现过**（代码校验）—— 模型的记忆不算数；
- AI 不可用 / 输出不合契约 ⇒ **未评估** ⇒ 回落原来的正则 + 黑名单
  （逐位一致）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.character_ner import CHARACTER_NER_CONTRACT
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.engine.services.ai_participation_settings import (
    ai_participation_enabled,
)
from application.character.services.character_ner_schema import CharacterNerLlmPayload

logger = logging.getLogger(__name__)

#: 来源标记
SOURCE_AI = "ai"
SOURCE_NONE = ""

#: 送进提示词的大纲上限（字符）
MAX_OUTLINE_CHARS = 6000
#: 送进提示词的已知角色名上限（个）
MAX_KNOWN_NAMES = 60


@dataclass
class CharacterNerResult:
    """一次 NER 的结果。"""

    #: 通过校验的角色名（按模型给的顺序）
    names: List[str] = field(default_factory=list)
    #: 因为「证据在大纲里找不到」被拒收的名字（**不静默**）
    rejected: List[str] = field(default_factory=list)
    #: ``ai`` / ``""``（未评估）
    source: str = SOURCE_NONE

    @property
    def is_assessed(self) -> bool:
        return self.source == SOURCE_AI


class CharacterNerService:
    """让 AI 找出「本章出场、但不在 Bible 里的角色」。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def extract(
        self,
        *,
        novel_id: str,
        outline: str,
        known_names: Sequence[str] = (),
        location_names: Sequence[str] = (),
    ) -> CharacterNerResult:
        """返回角色名；判不出来 ⇒ ``source=""``（调用方回落正则兜底）。"""
        text = (outline or "").strip()
        if not text:
            return CharacterNerResult(source=SOURCE_NONE)
        if self._llm is None or not ai_participation_enabled("character_ner"):
            logger.debug("CharacterNerService: llm_service 未配置，跳过 NER")
            return CharacterNerResult(source=SOURCE_NONE)

        known = [str(n).strip() for n in known_names if str(n).strip()][:MAX_KNOWN_NAMES]
        locations = [str(n).strip() for n in location_names if str(n).strip()]

        try:
            rendered = get_prompt_gateway().render(
                CHARACTER_NER_CONTRACT,
                {
                    "novel_id": str(novel_id),
                    "outline": text[:MAX_OUTLINE_CHARS],
                    "known_names": "、".join(known) if known else "（无）",
                    "location_names": "、".join(locations) if locations else "（无）",
                },
            )
            config = generation_config_from_profile("character_ner")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning("角色 NER 调用失败，按未评估处理 novel=%s: %s", novel_id, exc)
            return CharacterNerResult(source=SOURCE_NONE)

        payload = self._parse(result.content)
        if payload is None:
            return CharacterNerResult(source=SOURCE_NONE)

        names, rejected = self._validate(payload, text, known, locations)
        logger.info(
            "[CharacterNer] novel=%s 采纳 %d 个角色名，拒收 %d 个（证据对不上）",
            novel_id,
            len(names),
            len(rejected),
        )
        return CharacterNerResult(names=names, rejected=rejected, source=SOURCE_AI)

    @staticmethod
    def _parse(raw_text: str) -> Optional[CharacterNerLlmPayload]:
        """解析并按契约校验（走 ``PromptGateway.validate_output``）。"""
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("角色 NER 返回非 JSON，按未评估处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("角色 NER 返回的 JSON 不是对象，按未评估处理")
            return None
        try:
            validated = get_prompt_gateway().validate_output(
                CHARACTER_NER_CONTRACT, data
            )
        except Exception as exc:
            logger.warning("角色 NER 输出不合契约，按未评估处理: %s", exc)
            return None
        return validated

    @staticmethod
    def _validate(
        payload: CharacterNerLlmPayload,
        outline: str,
        known_names: Sequence[str],
        location_names: Sequence[str],
    ) -> tuple[List[str], List[str]]:
        """代码侧把关：证据必须在大纲里真出现过，且不能是已知角色/地点。"""
        known = {str(n).strip() for n in known_names if str(n).strip()}
        locations = {str(n).strip() for n in location_names if str(n).strip()}
        names: List[str] = []
        rejected: List[str] = []
        seen: set[str] = set()
        for entry in payload.characters:
            name = (entry.name or "").strip()
            evidence = (entry.evidence or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            if name in known or name in locations:
                continue
            if not evidence or evidence not in outline:
                # 模型记错了 ⇒ 名字不可信 ⇒ 拒收（**不静默**）
                rejected.append(name)
                continue
            names.append(name)
        return names, rejected
