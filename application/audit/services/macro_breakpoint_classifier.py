"""H-06 子项 A：让 AI 做宏观诊断的**语义分类**（发现 + 归类）。

分工
----
- **AI 决定**：这几章的事件里，哪些**偏离了角色的人设**、属于哪一类
  （真崩塌 / 人物突破 / 应激反应），以及依据是什么。
- **代码保证不越界**：事件白名单（AI 不得凭空造事件）、角色白名单、
  ``verdict`` 封闭枚举、有结论必须有依据（见
  ``macro_breakpoint_schema`` 的模块 docstring）。

改造前这两件事都是**代码做的**：

- 发现：``TRAIT_CONFLICT_RULES``（写死 4 个人设标签的标签求交表）⇒
  表外的人设一个断点都发现不了；
- 分类：``_classify_breakpoint`` 里 scar / 执念两条规则之外**一律 OOC** ⇒
  「判不出」被说成「人设崩塌」。

降级纪律
--------
- 调用失败 / 输出不合契约 / 给的全是废数据 ⇒ **未评估**（``source=""``），
  调用方回落**代码规则判定**（改造前的行为），且来源可分辨（``fallback``）。
- **AI 合法地说「都不偏离」不算未评估** ⇒ 不回退。
  （模型说没有 ≠ 没评估。）
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.macro_breakpoint import (
    MACRO_BREAKPOINT_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.engine.services.ai_participation_settings import (
    ai_participation_enabled,
)
from application.audit.services.macro_breakpoint_schema import (
    SOURCE_NONE,
    BreakpointClassification,
    normalize_classification,
)

logger = logging.getLogger(__name__)

#: 单个角色档案里保留的伤疤 / 执念条数（防长篇人物撑爆提示词）
_MAX_SCARS = 3
_MAX_MOTIVATIONS = 3


def _trim_character(profile: Dict[str, Any]) -> Dict[str, Any]:
    """把角色档案压成提示词能吃的形状。"""
    scars = list(profile.get("scars") or [])[:_MAX_SCARS]
    motivations = list(profile.get("motivations") or [])[:_MAX_MOTIVATIONS]
    return {
        "character_id": str(profile.get("character_id") or ""),
        "name": str(profile.get("name") or ""),
        "personality": str(profile.get("personality") or "")[:200],
        "description": str(profile.get("description") or "")[:300],
        "core_belief": str(profile.get("core_belief") or "")[:200],
        "traits": list(profile.get("traits") or []),
        "scars": [
            {
                "impact": str(s.get("impact") or ""),
                "source_event": str(s.get("source_event") or ""),
                "source_chapter": s.get("source_chapter"),
            }
            for s in scars
            if isinstance(s, dict)
        ],
        "motivations": [
            {
                "description": str(m.get("description") or ""),
                "priority": m.get("priority"),
            }
            for m in motivations
            if isinstance(m, dict)
        ],
        "state_summary": str(profile.get("state_summary") or ""),
    }


def _trim_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """把事件压成提示词能吃的形状。"""
    return {
        "event_id": str(event.get("event_id") or ""),
        "chapter": event.get("chapter"),
        "summary": str(event.get("summary") or ""),
        "tags": list(event.get("tags") or []),
    }


class MacroBreakpointClassifier:
    """AI 语义诊断：哪些事件偏离了角色人设、属于哪一类。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def classify(
        self,
        *,
        characters: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
    ) -> BreakpointClassification:
        """返回分类结果；判不出来时 ``source=""``（未评估）。"""
        if self._llm is None or not ai_participation_enabled("macro_breakpoint"):
            return BreakpointClassification({}, SOURCE_NONE)

        char_payload = [_trim_character(c) for c in (characters or [])]
        event_payload = [_trim_event(e) for e in (events or [])]
        event_ids = [str(e.get("event_id") or "") for e in event_payload]
        character_ids = [str(c.get("character_id") or "") for c in char_payload]

        try:
            rendered = get_prompt_gateway().render(
                MACRO_BREAKPOINT_CONTRACT,
                {
                    "characters_json": json.dumps(
                        char_payload, ensure_ascii=False, indent=2
                    ),
                    "events_json": json.dumps(
                        event_payload, ensure_ascii=False, indent=2
                    ),
                },
            )
            config = generation_config_from_profile("macro_breakpoint")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning("宏观断点分类调用失败，按未评估处理: %s", exc)
            return BreakpointClassification({}, SOURCE_NONE)

        data = self._parse(result.content)
        if data is None:
            return BreakpointClassification({}, SOURCE_NONE)
        return normalize_classification(
            data,
            valid_event_ids=event_ids,
            valid_character_ids=character_ids,
        )

    @staticmethod
    def _parse(raw_text: str) -> Optional[Dict[str, Any]]:
        """解析并按契约校验。走 ``PromptGateway.validate_output``。"""
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("宏观断点分类返回非 JSON，按未评估处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("宏观断点分类返回的 JSON 不是对象，按未评估处理")
            return None
        try:
            get_prompt_gateway().validate_output(MACRO_BREAKPOINT_CONTRACT, data)
        except Exception as exc:
            logger.warning("宏观断点分类输出不合契约，按未评估处理: %s", exc)
            return None
        return data
