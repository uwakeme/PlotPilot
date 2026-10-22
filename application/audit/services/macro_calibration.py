"""H-06：宏观诊断的「叙事校准」指令由 LLM 写。

改造前，这段指令是**代码写死的三套模板句**（``macro_diagnosis_service`` 的
``build_silent_context_patch``）：

- OOC 断点 → "后续生成须强化「X」所要求的行为倾向，避免继续偏离预设标签。"
- Breakout 断点 → "保持情绪张力，不要强行拉回原人设。"
- Scar 断点 → "此行为是角色伤疤的应激反应，应延续情绪强度，不要立即恢复冷静。"

它们被静默注入生成上下文头部（``context_budget_allocator`` 读 DB 里的
``context_patch``）—— 也就是说，**代码替编辑决定了接下来怎么写**。

本模块把这段指令交回 LLM：它看到人设标签 + 每个断点的事件摘要、标签与
规则分类后，写出一段具体的校准指令。

降级纪律
--------
- LLM 没给 / 调用失败 / 输出不合契约 ⇒ 返回 **空串** ⇒ **不注入任何东西**。
- 绝不回落到代码代写的模板句（登记表三态红线：禁止在降级路径返回任何看起来
  正常的数值/内容）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.macro_calibration import (
    MACRO_CALIBRATION_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway

from application.audit.services.macro_calibration_schema import (
    MacroCalibrationLlmPayload,
    normalize_calibration,
)

logger = logging.getLogger(__name__)

#: 送进提示词的断点条数上限（防长篇事件列表撑爆 prompt）
MAX_BREAKPOINTS = 12
#: 单条事件摘要截断
_EVENT_SUMMARY_LIMIT = 300


def _breakpoints_payload(trait: str, breakpoints: List[Any]) -> str:
    """把断点摊成给 LLM 看的清单。"""
    items = []
    for bp in breakpoints[:MAX_BREAKPOINTS]:
        to_dict = getattr(bp, "to_dict", None)
        data = to_dict() if callable(to_dict) else dict(bp or {})
        summary = str(data.get("event_summary") or "")[:_EVENT_SUMMARY_LIMIT]
        items.append(
            {
                "event_id": str(data.get("event_id") or ""),
                "chapter": data.get("chapter"),
                "事件摘要": summary,
                "标签": list(data.get("tags") or []),
                # H-06 子项 A：分类可能来自 AI，也可能来自规则或未评估
                "分类": str(data.get("breakpoint_type") or ""),
                "分类来源": str(data.get("breakpoint_source") or ""),
                "突破原因": str(data.get("breakout_reason") or ""),
                "角色": str(data.get("character_id") or ""),
            }
        )
    return json.dumps({"人设标签": trait, "断点": items}, ensure_ascii=False, indent=2)


async def build_macro_calibration(
    llm_service: Optional[LLMService],
    *,
    trait: str,
    breakpoints: List[Any],
) -> str:
    """让 LLM 写叙事校准指令；拿不到就返回空串（**不注入**）。"""
    if llm_service is None or not breakpoints:
        return ""

    try:
        rendered = get_prompt_gateway().render(
            MACRO_CALIBRATION_CONTRACT,
            {
                "trait": trait or "（未指定）",
                "breakpoints_json": _breakpoints_payload(trait, breakpoints),
            },
        )
        config = generation_config_from_profile("macro_calibration")
        result = await llm_service.generate(rendered.prompt, config)
    except Exception as exc:
        logger.warning("叙事校准指令生成失败（不注入任何校准）: %s", exc)
        return ""

    try:
        data = json.loads((result.content or "").strip())
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("叙事校准指令返回非 JSON（不注入）: %s", exc)
        return ""
    if not isinstance(data, dict):
        logger.warning("叙事校准指令返回的 JSON 不是对象（不注入）")
        return ""

    try:
        validated = get_prompt_gateway().validate_output(
            MACRO_CALIBRATION_CONTRACT, data
        )
    except Exception as exc:
        logger.warning("叙事校准指令输出不合契约（不注入）: %s", exc)
        return ""

    calibration = normalize_calibration(
        getattr(validated, "calibration", ""),
        getattr(validated, "reason", ""),
    )
    if not calibration:
        logger.info("叙事校准未给出（不注入）: %s", getattr(validated, "reason", ""))
        return ""
    return calibration
