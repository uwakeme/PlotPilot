"""J-01：目标章数 / 终局预算的**AI 提案**。

改造前这块判断是代码写死的常量公式
（``target_budget.propose_continue_block`` = ``max(20, round(target * 0.2))``），
而「这本书还差多少章收得住」「终局该留几章」是**创作判断** ⇒ 交回 AI。

三态与来源
----------
- AI 给出**完整且自洽**的提案 ⇒ 用它，来源 ``ai``；
- AI 调用失败 / 输出不合契约 / 明确说「给不出」⇒ 回落**引擎默认公式**
  （与改造前逐位相同），来源 ``fallback``。

⇒ 读端能分辨「这是 AI 说的」与「这是公式兜的」——
**模型说「没有」≠ 没评估**，但两者都不许被伪装成对方的结论。

代码保留的不变量
----------------
- ``continue_block >= 1``（0 等于「继续写」什么也不做）；
- 终局预算仍被 domain 的 ``clamp_finale_budget``（[5, 200]）钳住 ——
  **AI 动不了这个上限**（那是产品常量，不是创作判断）；
- 提案钳制后必须仍然自洽（min ≤ 值 ≤ max）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from domain.ai.services.llm_service import LLMService
from domain.novel.value_objects.generation_preferences import (
    FINALE_BUDGET_MAX,
    FINALE_BUDGET_MIN,
    clamp_finale_budget,
)
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.target_budget import (
    TARGET_BUDGET_PROPOSAL_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.engine.services.ai_participation_settings import (
    ai_participation_enabled,
)
from application.blueprint.services.target_budget import (
    clamp_target_chapters,
    propose_continue_block,
)
from application.blueprint.services.target_budget_schema import (
    SOURCE_AI,
    SOURCE_FALLBACK,
)

logger = logging.getLogger(__name__)


class TargetBudgetProposalService:
    """让 AI 提「再加多少章 / 终局留几章」，失败回落公式。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def propose(
        self,
        *,
        novel_id: str,
        title: str = "",
        genre: str = "",
        target_chapters: int = 0,
        completed_chapters: int = 0,
        remaining_threads: str = "",
        recent_digest: str = "",
        current_finale_budget: int = 0,
    ) -> Dict[str, Any]:
        """返回提案 dict（含来源标记）。

        ``source`` ∈ ``{"ai", "fallback"}``；``reason`` 是 AI 的依据
        （回落时写明「AI 提案不可用，已回落引擎默认公式」）。
        """
        target = max(int(target_chapters or 0), 0)
        completed = max(int(completed_chapters or 0), 0)

        ai = await self._ask_ai(
            novel_id=novel_id,
            title=title,
            genre=genre,
            target_chapters=target,
            completed_chapters=completed,
            remaining_threads=remaining_threads,
            recent_digest=recent_digest,
            current_finale_budget=current_finale_budget,
        )

        if ai is not None:
            finale_budget = clamp_finale_budget(ai["finale_budget"])
            finale_min = clamp_finale_budget(ai["finale_budget_min"])
            finale_max = clamp_finale_budget(ai["finale_budget_max"])
            # 钳制后可能破坏自洽（min/value/max 各自被夹到 [5,200]）⇒ 重新对齐
            finale_min = min(finale_min, finale_budget)
            finale_max = max(finale_max, finale_budget)
            continue_block = max(int(ai["continue_block"]), 1)
            source = SOURCE_AI
            reason = str(ai.get("reason") or "")
        else:
            continue_block = propose_continue_block(
                target_chapters=target, completed_chapters=completed
            )
            finale_budget = clamp_finale_budget(current_finale_budget)
            finale_min = FINALE_BUDGET_MIN
            finale_max = FINALE_BUDGET_MAX
            source = SOURCE_FALLBACK
            reason = "AI 提案不可用，已回落引擎默认公式"

        return {
            "continue_target": clamp_target_chapters(completed + continue_block),
            "continue_block": continue_block,
            "finale_budget": finale_budget,
            "finale_budget_min": finale_min,
            "finale_budget_max": finale_max,
            "source": source,
            "reason": reason,
        }

    async def _ask_ai(self, **variables: Any) -> Optional[Dict[str, Any]]:
        """问 AI；拿不到**完整且自洽**的提案就返回 None（由调用方回落公式）。"""
        if not ai_participation_enabled("target_budget_proposal"):
            logger.info(
                "AI 参与总闸关闭：目标预算提案按引擎默认公式处理 novel=%s",
                variables.get("novel_id"),
            )
            return None
        try:
            rendered = get_prompt_gateway().render(
                TARGET_BUDGET_PROPOSAL_CONTRACT,
                {
                    "novel_id": str(variables["novel_id"]),
                    "title": str(variables.get("title") or "") or "（未提供）",
                    "genre": str(variables.get("genre") or "") or "（未提供）",
                    "target_chapters": int(variables.get("target_chapters") or 0),
                    "completed_chapters": int(variables.get("completed_chapters") or 0),
                    "remaining_threads": str(variables.get("remaining_threads") or "")
                    or "（未提供）",
                    "recent_digest": str(variables.get("recent_digest") or "")
                    or "（未提供）",
                    "current_finale_budget": int(
                        variables.get("current_finale_budget") or 0
                    ),
                },
            )
            config = generation_config_from_profile("target_budget_proposal")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning(
                "目标预算提案调用失败，回落引擎默认公式 novel=%s: %s",
                variables.get("novel_id"),
                exc,
            )
            return None

        data = self._parse(result.content)
        if data is None:
            return None
        return {
            "continue_block": data["continue_block"],
            "finale_budget": data["finale_budget"],
            "finale_budget_min": data["finale_budget_min"],
            "finale_budget_max": data["finale_budget_max"],
            "reason": data.get("reason", ""),
        }

    @staticmethod
    def _parse(raw_text: str) -> Optional[Dict[str, Any]]:
        """解析并按契约校验（走 ``PromptGateway.validate_output``）。"""
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("目标预算提案返回非 JSON，回落引擎默认公式: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("目标预算提案返回的 JSON 不是对象，回落引擎默认公式")
            return None
        try:
            validated = get_prompt_gateway().validate_output(
                TARGET_BUDGET_PROPOSAL_CONTRACT, data
            )
        except Exception as exc:
            logger.warning("目标预算提案输出不合契约，回落引擎默认公式: %s", exc)
            return None

        # 四个数字要么全给、要么全不给（契约已保证）⇒ 缺一个就当「没给全」
        values = {
            "continue_block": getattr(validated, "continue_block", None),
            "finale_budget": getattr(validated, "finale_budget", None),
            "finale_budget_min": getattr(validated, "finale_budget_min", None),
            "finale_budget_max": getattr(validated, "finale_budget_max", None),
        }
        if any(v is None for v in values.values()):
            logger.info(
                "目标预算提案未给出完整数字，回落引擎默认公式 reason=%s",
                getattr(validated, "reason", ""),
            )
            return None
        return {**values, "reason": getattr(validated, "reason", "")}
