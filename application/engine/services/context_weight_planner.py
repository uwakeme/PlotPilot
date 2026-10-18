"""D-02：「本章需要什么记忆」规划调用。

给 AI 的问题：即将写的这一章，哪几类上下文槽位更重要？
输出：``{槽位 key: 权重 0-1}``，代码在 tier 预算内换算成 token
（见 `context_budget_policy.allocate_tier` 的 `slot_weights`）。

三态
----
本模块返回 ``None`` 表示「没判出来」，**不返回默认权重**。默认权重（比如全 0.5）
会被读成「AI 看过本章，认为各槽位同等重要」，那与「没问过」完全不是一回事。

降级链（按设计文档的红线）
--------------------------
LLM 失败 / 输出不合契约 ⇒ ``None``（未评估）
⇒ 分配器走无权重路径（纯静态 priority，与改造前逐位一致）

绝不在这里编一个「合理默认」。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.context_weights import (
    CONTEXT_WEIGHTS_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.engine.services.context_weight_catalog import (
    catalog_lines,
    sanitize_weights,
)
from application.engine.services.context_weight_settings import (
    get_context_weight_runtime_settings,
)

logger = logging.getLogger(__name__)


class ContextWeightPlanner:
    """按章规划上下文槽位权重。

    实例必须是长生命周期的（DI 工厂带 ``@lru_cache``）——下面两级缓存
    都挂在实例上：新建实例会让缓存永远 miss、退避永远不生效。
    """

    def __init__(self, llm: LLMService):
        self._llm = llm
        # (novel_id, chapter_number) -> (过期时间戳, 权重)
        self._cache: Dict[Tuple[str, int], Tuple[float, Dict[str, float]]] = {}
        # novel_id -> 退避截止时间戳（调用失败后短期内不再重试）
        self._backoff_until: Dict[str, float] = {}

    async def plan_weights(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        chapter_outline: str = "",
        phase_hint: str = "未知",
        recent_digest: str = "",
    ) -> Optional[Dict[str, float]]:
        """返回 ``{槽位 key: 权重}``；判不出来或调用失败一律返回 ``None``。"""
        settings = get_context_weight_runtime_settings()

        cached = self._cache.get((novel_id, chapter_number))
        if cached is not None and cached[0] > time.monotonic():
            logger.info(
                "上下文权重命中缓存 novel=%s ch=%s slots=%s",
                novel_id, chapter_number, sorted(cached[1]),
            )
            return dict(cached[1])

        if time.monotonic() < self._backoff_until.get(novel_id, 0.0):
            logger.info(
                "上下文权重处于失败退避期，跳过调用 novel=%s ch=%s",
                novel_id, chapter_number,
            )
            return None

        try:
            rendered = get_prompt_gateway().render(
                CONTEXT_WEIGHTS_CONTRACT,
                {
                    "novel_id": novel_id,
                    "chapter_number": chapter_number,
                    "chapter_outline": chapter_outline or "（未提供）",
                    "phase_hint": phase_hint,
                    "slot_catalog": catalog_lines(),
                    "recent_digest": recent_digest or "（未提供）",
                },
            )
            config = generation_config_from_profile("context_weights")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            # 降级不是「出错」而是「没判出来」—— 分配器据此走旧路径。
            logger.warning(
                "上下文权重规划调用失败，按未评估处理 novel=%s ch=%s: %s",
                novel_id, chapter_number, exc,
            )
            self._backoff_until[novel_id] = (
                time.monotonic() + settings.failure_backoff_seconds
            )
            return None

        payload = self._parse(rendered, result.content, novel_id, chapter_number)
        if payload is None:
            return None

        if not payload.get("evaluated", False):
            logger.info(
                "上下文权重未评估 novel=%s ch=%s reason=%s",
                novel_id, chapter_number, payload.get("reason", ""),
            )
            return None

        weights = sanitize_weights(payload.get("weights"))
        if not weights:
            # 声称「评估了」但一个合法槽位都没给 —— 当作没判出来，
            # 而不是退回平均值。
            logger.warning(
                "上下文权重声称已评估但无可用槽位，按未评估处理 novel=%s ch=%s",
                novel_id, chapter_number,
            )
            return None

        ttl = settings.cache_ttl_seconds
        if ttl > 0:
            self._cache[(novel_id, chapter_number)] = (
                time.monotonic() + ttl,
                dict(weights),
            )
        logger.info(
            "上下文权重已规划 novel=%s ch=%s slots=%s",
            novel_id, chapter_number, sorted(weights),
        )
        return weights

    @staticmethod
    def _parse(
        rendered: Any, raw_text: str, novel_id: str, chapter_number: int
    ) -> Optional[Dict[str, Any]]:
        """解析并按契约校验。任何不合契约的输出都算「没判出来」。

        走 ``PromptGateway.validate_output`` —— 契约里声明了
        ``output_schema=ContextWeightsLlmPayload``，手写解析等于让它形同虚设：
        契约改字段时手写解析不会跟着变。
        """
        text = (raw_text or "").strip()
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(
                "上下文权重返回非 JSON，按未评估处理 novel=%s ch=%s: %s",
                novel_id, chapter_number, exc,
            )
            return None
        if not isinstance(data, dict):
            logger.warning(
                "上下文权重返回的 JSON 不是对象，按未评估处理 novel=%s ch=%s",
                novel_id, chapter_number,
            )
            return None
        try:
            validated = get_prompt_gateway().validate_output(
                CONTEXT_WEIGHTS_CONTRACT, data
            )
        except Exception as exc:
            logger.warning(
                "上下文权重输出不合契约，按未评估处理 novel=%s ch=%s: %s",
                novel_id, chapter_number, exc,
            )
            return None
        return {
            "evaluated": bool(getattr(validated, "evaluated", False)),
            "weights": getattr(validated, "weights", None),
            "reason": str(getattr(validated, "reason", "") or ""),
        }
