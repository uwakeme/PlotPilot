"""J-02：让 AI 把这本书**匹配**到一个文风公约预设。

改造前
------
「这本书该用哪条文风公约」只有两个来源：作者手选；或前端按关键词打分猜一个
（``matchPresetValue`` —— **只管显示标签**，不写进 Bible）。
⇒ 登记表 J-02 要的那一步（「AI 语义匹配到预设」）**根本不存在**。

现在
----
AI 看题材/梗概/世界观/已写文风，在**封闭的预设清单**里挑一个，或明说都不匹配。

三条不变量
----------
1. **判不出来不许回落成「第一个预设」** —— 那是第 4 批删掉的「默认注入
   ``presets[0]」那条（前端替作者定文风，而它会具名进正文提示词 ⇒ 确实生效）。
   ⇒ 回落等于把那条复活。
2. **「AI 说都不匹配」与「没评估」必须可分辨** ⇒ 前者 ``source="ai"`` 且
   ``value=None``，后者 ``source=""``。
3. **正文不由模型写** ⇒ 模型只给 ``value``（标识符），正文由代码从清单取。

⚠ 这项能力**默认不改变今天的行为**：它是一条**新入口**（作者点「AI 推荐」），
不参与任何既有链路 ⇒ 没有作者点它，就一次模型调用都不会发生。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from application.engine.services.ai_participation_settings import (
    ai_participation_enabled,
)
from application.world.services.style_preset_match_schema import (
    SOURCE_NONE,
    StylePresetSuggestion,
    normalize_suggestion,
)
from application.world.style_preset_catalog import get_style_preset, get_style_presets
from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.style_preset_match import (
    STYLE_PRESET_MATCH_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway

logger = logging.getLogger(__name__)

#: 总闸 feature id（登记在 ``AI_PARTICIPATION_FEATURES``）。
#:
#: ⚠ 闸门调用处**必须写字面量** —— 总闸覆盖守卫
#: (``test_ai_participation_master_switch_coverage.py``) 是用 AST 扫
#: ``ai_participation_enabled("<字面量>")`` 这个**形状**来判定
#: 「闸门真的挡住了模型调用」的；走常量就扫不到
#: ⇒ 守卫会把这条能力判成「没接闸」（假红）。
FEATURE_ID = "style_preset_match"

#: 本次模型调用的**硬上限**（秒）。
#:
#: ⚠ 复审 B2：``config/generation_profiles.yaml`` 里的 ``timeout_seconds`` / ``retries``
#:   **全仓无人读**（``generation_config_from_profile`` 只读 model / max_tokens /
#:   temperature / response_format）⇒ 改那两个键是**空操作**；
#:   真实上限在 **provider 层**（``LLM_MAX_TOTAL_ATTEMPTS = 3`` 次尝试 ×
#:   ``Settings.timeout_seconds``（默认 300））⇒ **最坏约 900 秒**，
#:   而前端 30 秒（``shortTaskTimeoutMs``）就放弃 ⇒ 用户看到失败，后端还在烧 token。
#: ⇒ 这里**真的**加一道硬闸：超时 ⇒ 按「未评估」处理（**不编造**）。
#:
#: 数值与前端 ``shortTaskTimeoutMs``（30s）对齐：两边同时放弃。
MATCH_TIMEOUT_SECONDS = 30


class StylePresetMatcher:
    """文风预设语义匹配器。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def suggest(
        self,
        *,
        novel_id: str,
        title: str = "",
        genre: str = "",
        premise: str = "",
        worldbuilding_style: str = "",
        current_style_notes: str = "",
    ) -> StylePresetSuggestion:
        """返回推荐结果；判不出来 ⇒ ``source=""``（未评估）。"""
        presets = get_style_presets()
        if not presets:
            return StylePresetSuggestion(
                source=SOURCE_NONE, reason="没有可用的文风预设清单，未评估"
            )

        # ⚠ 字面量（不是 FEATURE_ID）—— 理由见 FEATURE_ID 的注释。
        if self._llm is None or not ai_participation_enabled("style_preset_match"):
            return StylePresetSuggestion(
                source=SOURCE_NONE, reason="AI 参与总闸关闭，未评估"
            )

        candidates = "\n".join(preset.as_candidate_line() for preset in presets)

        try:
            rendered = get_prompt_gateway().render(
                STYLE_PRESET_MATCH_CONTRACT,
                {
                    "novel_id": str(novel_id),
                    "title": str(title or "") or "（未提供）",
                    "genre": str(genre or "") or "（未提供）",
                    "premise": str(premise or "") or "（未提供）",
                    "worldbuilding_style": str(worldbuilding_style or "") or "（未提供）",
                    "current_style_notes": str(current_style_notes or "") or "（未提供）",
                    "candidates": candidates,
                },
            )
            config = generation_config_from_profile(FEATURE_ID)
            result = await asyncio.wait_for(
                self._llm.generate(rendered.prompt, config),
                timeout=MATCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # ⚠ 复审 B2：provider 层的真实上限是 ~900s ⇒ 这里自己加一道硬闸，
            #   与前端的 30s 对齐 ⇒ 两边同时放弃（不再「用户看到失败、后端还在烧」）。
            logger.warning(
                "文风预设匹配超时（>%ss），按未评估处理 novel=%s",
                MATCH_TIMEOUT_SECONDS,
                novel_id,
            )
            return StylePresetSuggestion(
                source=SOURCE_NONE,
                reason=f"模型调用超时（>{MATCH_TIMEOUT_SECONDS} 秒），未评估",
            )
        except Exception as exc:
            logger.warning("文风预设匹配调用失败，按未评估处理 novel=%s: %s", novel_id, exc)
            return StylePresetSuggestion(
                source=SOURCE_NONE, reason="模型调用失败，未评估"
            )

        data = self._parse(self._extract_text(result))
        if data is None:
            return StylePresetSuggestion(
                source=SOURCE_NONE, reason="模型输出不合契约，未评估"
            )
        return normalize_suggestion(data, preset_lookup=get_style_preset)

    @staticmethod
    def _extract_text(result: Any) -> str:
        """从 LLM 返回值里取文本。

        ⚠ ``LLMService.generate`` 的契约是返回 ``GenerationResult``，
        但**既有 stub / 部分 provider** 会直接返回字符串。
        ⇒ 这里两种都认，**不因为形态差异就崩**。
        """
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        content = getattr(result, "content", None)
        return content if isinstance(content, str) else ""

    @staticmethod
    def _parse(raw_text: str) -> Optional[Dict[str, Any]]:
        """解析并按契约校验（走 ``PromptGateway.validate_output``）。"""
        try:
            data = json.loads((raw_text or "").strip())
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("文风预设匹配返回非 JSON，按未评估处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("文风预设匹配返回的 JSON 不是对象，按未评估处理")
            return None
        try:
            validated = get_prompt_gateway().validate_output(
                STYLE_PRESET_MATCH_CONTRACT, data
            )
        except Exception as exc:
            logger.warning("文风预设匹配输出不合契约，按未评估处理: %s", exc)
            return None
        return validated.model_dump()
