"""D-01：本章节奏模式判定（「要不要余韵章」）。

这块判断此前由代码公式决定（``legacy_writing_delegate.py:202-203``
的 ``tension_100 >= 80``），并在命中时把一段固定文案拼到 outline 前面。
本模块把它交回 AI，并让判断结果真正流进 StoryPipeline ——
此前 ``PipelineContext.needs_buffer`` / ``buffer_reason`` 是**声明了但全仓无赋值**
的死字段。

代码保留的不变量
----------------
**章号连续**。选章仍然由 ``_step_find_next_chapter`` 按「第一个未完成」决定，
AI 不参与选章 —— 实测生产库 3 本书 592 个章节点零缺口，连续下本就没有可选空间，
让 AI 在一个候选里"选"是假参与。AI 判断的是**这一章该怎么写**，不是**写哪一章**。

三态
----
判不出来返回 ``None``。调用方据此**保持原行为（正常推进）**并记录原因，
绝不把 ``None`` 当成 ``False`` 再谎称是 AI 的判断。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.chapter_mode import CHAPTER_MODE_CONTRACT
from infrastructure.ai.prompt_gateway import get_prompt_gateway

logger = logging.getLogger(__name__)


class ChapterModePlanner:
    """判定本章是余韵章还是正常推进。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def decide(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        outline: str = "",
        last_tension: Optional[float] = None,
        stage_hint: str = "未知",
        due_debts: str = "",
        due_foreshadowings: str = "",
    ) -> Optional[Dict[str, Any]]:
        """返回 ``{"needs_buffer": bool, "reason": str, "signals": [...]}``；
        判不出来或调用失败返回 ``None``。"""
        try:
            rendered = get_prompt_gateway().render(
                CHAPTER_MODE_CONTRACT,
                {
                    "novel_id": novel_id,
                    "chapter_number": chapter_number,
                    "outline": outline or "（未提供）",
                    "last_tension": "（无数据）" if last_tension is None else str(last_tension),
                    "stage_hint": stage_hint,
                    "due_debts": due_debts or "（无）",
                    "due_foreshadowings": due_foreshadowings or "（无）",
                },
            )
            config = generation_config_from_profile("chapter_mode")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning(
                "章节节奏判定调用失败，按未判定处理 novel=%s ch=%s: %s",
                novel_id, chapter_number, exc,
            )
            return None

        data = self._parse(result.content)
        if data is None:
            return None

        if data.get("needs_buffer") is None:
            logger.info(
                "章节节奏未判定 novel=%s ch=%s reason=%s",
                novel_id, chapter_number, data.get("reason", ""),
            )
            return None

        return {
            "needs_buffer": bool(data["needs_buffer"]),
            "reason": str(data.get("reason") or ""),
            "signals": self._normalize_signals(data.get("signals")),
        }

    @staticmethod
    def _normalize_signals(raw: Any) -> List[str]:
        if not isinstance(raw, list):
            return []
        return [str(x).strip()[:60] for x in raw if str(x).strip()][:6]

    @staticmethod
    def _parse(raw_text: str) -> Optional[Dict[str, Any]]:
        """解析并按契约校验。走 ``PromptGateway.validate_output``。

        契约声明了 ``output_schema=ChapterModeLlmPayload``，其中
        ``needs_buffer`` 是 ``Optional[bool]`` 且默认 ``None`` ——
        模型不给、给 null、给字符串 "true" 三种情况都由契约统一裁决成
        「没给出明确结论」，而不是在这里手写分支。
        """
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("章节节奏判定返回非 JSON，按未判定处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("章节节奏判定返回的 JSON 不是对象，按未判定处理")
            return None
        try:
            validated = get_prompt_gateway().validate_output(
                CHAPTER_MODE_CONTRACT, data
            )
        except Exception as exc:
            # 契约不通过（整坨垃圾输出）⇒ 未判定。**不得**退回手写宽松解析。
            logger.warning("章节节奏判定输出不合契约，按未判定处理: %s", exc)
            return None
        return {
            "needs_buffer": getattr(validated, "needs_buffer", None),
            "reason": str(getattr(validated, "reason", "") or ""),
            "signals": getattr(validated, "signals", []),
        }
