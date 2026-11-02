"""独立多维张力分析服务。

将张力评分从 llm_chapter_extract_bundle() 的多任务 JSON 提取中拆出，
使用专门的多维 prompt（情节/情绪/节奏）进行精准分析。
"""
from __future__ import annotations

import logging
from typing import Optional

from domain.ai.services.llm_service import LLMService
from domain.novel.value_objects.tension_dimensions import TensionDimensions
from application.ai.tension_scoring_contract import (
    TensionScoringLlmPayload,
    tension_scoring_payload_to_domain,
    tension_scoring_response_format,
)
from application.ai.structured_json_pipeline import structured_json_generate_with_status
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_gateway import PromptGatewayError, get_prompt_gateway
from infrastructure.ai.prompt_keys import TENSION_SCORING

logger = logging.getLogger(__name__)

# 章节正文最大长度（与 llm_chapter_extract_bundle 保持一致）
_MAX_CONTENT_LENGTH = 24000

_TENSION_SCORING_CONTRACT = PromptContract(
    node_key=TENSION_SCORING,
    version="1.0.0",
    output_schema=TensionScoringLlmPayload,
    generation_profile="tension_scoring",
)


#: 「前章综合张力」进提示词时的**两种**形态。
#:
#: ⚠ 第 84 批：此前这个变量的**默认值是 50.0** ⇒ 没有前章时也在告诉模型
#:   「前章综合张力为 50/100」⇒ 假值污染判断依据（提示词里还有
#:   「若前章≥70 可以回落 / 若前章≤30 需要拉升」这种**条件指令**）
#:   ⇒ 光把数字换成「未评估」不够 ⇒ 条件指令也要一起撤掉。
PREV_TENSION_UNKNOWN_TEXT = (
    "前章综合张力未评估（没有可参考的前章）⇒ 只依据本章正文判断；"
    "不要假设前章是 50 分，也不要因为「前章是中庸的 50 分」而给出中庸的分。"
)
PREV_TENSION_KNOWN_TEMPLATE = (
    "前章综合张力为 {value}/100。若前章≥70，本章可以短暂回落但必须保留余震；"
    "若前章≤30，本章需要明显拉升冲突或信息增量。"
)


def build_prev_tension_text(prev_chapter_tension: Optional[float]) -> str:
    """把「前章张力」渲染成**可直接进提示词**的一句话。

    ⚠ ``None`` ⇒ 未评估 ⇒ 明确说「未评估」并**撤掉条件指令**
    （否则模型会自己假设一个数）。
    """
    if prev_chapter_tension is None:
        return PREV_TENSION_UNKNOWN_TEXT
    return PREV_TENSION_KNOWN_TEMPLATE.format(value=f"{float(prev_chapter_tension):.0f}")


class TensionScoringService:
    """独立多维张力分析服务。

    对章节正文进行三维度（情节张力、情绪张力、节奏张力）评分，
    并通过加权公式计算综合张力分。
    """

    def __init__(self, llm_service: LLMService) -> None:
        self._llm = llm_service

    async def score_chapter(
        self,
        chapter_content: str,
        chapter_number: int,
        prev_chapter_tension: Optional[float] = None,
    ) -> TensionDimensions:
        """分析章节的多维张力。

        Args:
            chapter_content: 章节正文
            chapter_number: 章节号
            prev_chapter_tension: 前章综合张力（0-100），用于提供上下文基准。
                ⚠ 第 84 批：默认从 ``50.0`` 改成 ``None``
                  —— 「没有前章」与「前章正好 50 分」是两件事
                  ⇒ 没给就是**未评估**，不许拿 50 冒充。

        Returns:
            TensionDimensions 多维张力结果
        """
        body = chapter_content.strip()
        if not body:
            return TensionDimensions.unevaluated()
        if len(body) > _MAX_CONTENT_LENGTH:
            body = body[:_MAX_CONTENT_LENGTH] + "\n\n…（正文过长已截断）"

        try:
            prompt = get_prompt_gateway().render(
                _TENSION_SCORING_CONTRACT,
                {
                    "content": body,
                    "prev_tension": build_prev_tension_text(prev_chapter_tension),
                },
            ).prompt
        except PromptGatewayError as exc:
            logger.warning("张力评分提示词渲染失败: %s", exc)
            return TensionDimensions.unevaluated()
        config = generation_config_from_profile(
            "tension_scoring",
            response_format=tension_scoring_response_format(),
        )

        try:
            parsed = await structured_json_generate_with_status(
                llm=self._llm,
                prompt=prompt,
                config=config,
                schema_model=TensionScoringLlmPayload,
            )
            payload = parsed.payload
        except Exception as e:
            logger.warning("张力评分管线异常: %s", e)
            payload = None
            parsed = None

        if payload is None:
            if parsed is not None and parsed.failure_stage == "parse_or_schema":
                # 解析失败不是「张力正好 50」。返回 UNEVALUATED 哨兵，
                # 由下游（chapter_narrative_sync / monitor）按未评估处理。
                logger.warning(
                    "张力评分解析失败，本次不评分（未评估）: %s",
                    list(parsed.errors[:4]),
                )
                return TensionDimensions.unevaluated()
            return TensionDimensions.unevaluated()

        dims = tension_scoring_payload_to_domain(payload)
        logger.debug(
            "张力评分完成: plot=%.0f emotional=%.0f pacing=%.0f composite=%.1f",
            dims.plot_tension,
            dims.emotional_tension,
            dims.pacing_tension,
            dims.composite_score,
        )
        return dims
