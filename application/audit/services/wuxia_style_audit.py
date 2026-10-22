"""E-04：让 AI 审「武侠战斗逻辑」与「现代口语」，代码只校验不越界。

分工
----
- **AI 决定**：本章正文里有没有**违反武功逻辑**的描写（一招秒杀没有铺垫）、
  有没有**现代口语**混进对白。
- **代码保证不越界**：违规类型封闭枚举、**证据必须逐字出现在正文里**
  （AI 编的证据一律丢弃）、有结论必须有依据（见
  ``wuxia_style_audit_schema`` 的模块 docstring）。

改造前这两件事都是**代码做的**：两条正则 + 恰 7 个词的现代语黑名单
⇒ 词表外的写法一律漏报，而真正违反战斗逻辑的写法反而抓不到。

降级纪律
--------
- 调用失败 / 输出不合契约 / 给的全是废数据 ⇒ **未评估**，
  调用方回落**代码表**（改造前的行为，逐位一致），来源可分辨（``fallback``）。
- **AI 合法地说「没有违规」不算未评估** ⇒ 不回退。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, List, Optional

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.wuxia_style_audit import (
    WUXIA_STYLE_AUDIT_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.audit.services.wuxia_style_audit_schema import (
    SOURCE_AI,
    SOURCE_FALLBACK,
    SOURCE_NONE,
    TYPE_COMBAT_LOGIC,
    TYPE_MODERN_SLANG,
    StyleAudit,
    StyleViolation,
    normalize_style_audit,
)

logger = logging.getLogger(__name__)

#: 送进提示词的正文长度上限（字符）
_CONTENT_CHARS = 6000

#: 代码兜底：「无铺垫的一招秒杀」两条正则（改造前的原样）
FALLBACK_COMBAT_PATTERNS = (
    r"一招.{0,4}(秒杀|击杀|击毙|毙命)",
    r"随手一.{0,2}(毙|杀|灭)",
)
#: 代码兜底：现代口语黑名单（改造前的原样，恰好 7 项）
FALLBACK_MODERN_WORDS = ("搞定", "没毛病", "杠杠的", "绝绝子", "yyds", "666", "离谱")


def fallback_audit(content: str) -> StyleAudit:
    """代码兜底：改造前的两条正则 + 7 项黑名单（**逐位一致**）。"""
    text = content or ""
    violations: List[StyleViolation] = []
    for pattern in FALLBACK_COMBAT_PATTERNS:
        if re.search(pattern, text):
            violations.append(
                StyleViolation(
                    type=TYPE_COMBAT_LOGIC,
                    evidence="",
                    description="出现无铺垫的一招秒杀，可能违反武功逻辑",
                    severity=0.7,
                    suggestion="如确需秒杀，前面应有充分铺垫（暗器/毒/偷袭/境界碾压）",
                )
            )
            break
    for word in FALLBACK_MODERN_WORDS:
        if word in text:
            violations.append(
                StyleViolation(
                    type=TYPE_MODERN_SLANG,
                    evidence=word,
                    description=f"武侠文中出现现代口语：{word}",
                    severity=0.8,
                    suggestion="替换为古白话表达",
                )
            )
    return StyleAudit(violations, SOURCE_FALLBACK)


class WuxiaStyleAuditor:
    """AI 审武侠战斗逻辑与现代口语。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def audit(self, *, content: str) -> StyleAudit:
        """返回审结果；判不出来时 ``source=""``（未评估）。"""
        if self._llm is None or not content:
            return StyleAudit([], SOURCE_NONE)

        try:
            rendered = get_prompt_gateway().render(
                WUXIA_STYLE_AUDIT_CONTRACT,
                {"content": str(content or "")[:_CONTENT_CHARS]},
            )
            config = generation_config_from_profile("wuxia_style_audit")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning("武侠风格审计调用失败，按未评估处理: %s", exc)
            return StyleAudit([], SOURCE_NONE)

        data = self._parse(result.content)
        if data is None:
            return StyleAudit([], SOURCE_NONE)
        return normalize_style_audit(data, content=str(content or ""))

    @staticmethod
    def _parse(raw_text: str) -> Optional[dict]:
        """解析并按契约校验。走 ``PromptGateway.validate_output``。"""
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("武侠风格审计返回非 JSON，按未评估处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("武侠风格审计返回的 JSON 不是对象，按未评估处理")
            return None
        try:
            get_prompt_gateway().validate_output(WUXIA_STYLE_AUDIT_CONTRACT, data)
        except Exception as exc:
            logger.warning("武侠风格审计输出不合契约，按未评估处理: %s", exc)
            return None
        return data
