"""B-05：结构网格（部/卷/幕的**数量**）由 **AI 提案**，不再由代码公式铺死。

改造前
------
「精密」模式的结构网格是**代码/前端定的**：
``StructurePreference`` 默认 3×3×3 ⇒ ``_build_precise_structure_skeleton``
按 parts×vpp×apv 铺死整棵树，提示词还双重锁定它
（「结构网格绝对服从…不得新增、删除、合并或拆分节点」
+「你的任务只是为固定节点填写标题」）。

⇒ 「这本书该分几部、每部几卷、每卷几幕」这个**创作判断**被写成了一条常量，
而精密模式在生产上**根本没有前端入口**（SSE 路由固定传
``structure_preference=None`` ⇒ 走的是快捷模式）。

现在
----
AI 先**提案**网格（看题材、梗概、目标章数），代码**夹紧**提案，
再交给精密模式填充内容 ⇒ 最后由用户在规划弹窗里**确认**后落库。

不变量（登记表 B-05「代码保留的不变量」）
----------------------------------------
- **结构深度上限**：三层各自的层数 ∈ [1, 10]（与 ``StructurePreference`` 的
  ``le=10`` 一致）—— 不许模型提议一个 50 部的结构；
- **防无限嵌套**：幕数总数 ≤ 目标章数（每幕至少 1 章 ⇒ 幕数不能超过章数）；
- 被夹过时 ``clamped=True``，**不许静默**。

降级纪律
--------
AI 不可用 / 输出不合契约 / 没给出可用数字 ⇒ **未评估**
⇒ 回落**代码兜底网格**（3×3×3，与改造前**逐位一致**）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.structure_proposal import (
    STRUCTURE_PROPOSAL_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway
from application.engine.services.ai_participation_settings import (
    ai_participation_enabled,
)

logger = logging.getLogger(__name__)

#: 来源标记
SOURCE_AI = "ai"
SOURCE_FALLBACK = "fallback"
SOURCE_NONE = ""

#: 结构深度上限（三层各自）—— 登记表 B-05 的不变量
MAX_PARTS = 10
MAX_VOLUMES_PER_PART = 10
MAX_ACTS_PER_VOLUME = 10

#: 代码兜底网格（= 改造前 ``StructurePreference`` 的默认值 3×3×3）
FALLBACK_GRID = (3, 3, 3)

# ── A-07：卷内节奏建议（与网格同一次提案） ───────────────────────────
#: 每卷高潮节点数上限（代码保留的不变量）
MAX_CLIMAXES_PER_VOLUME = 6
#: 代码兜底：每卷 3 个高潮节点（与改造前写死的那句「2~3 个」的中间值）
FALLBACK_CLIMAXES_PER_VOLUME = 3


@dataclass(frozen=True)
class StructureGridProposal:
    """一次结构网格提案。"""

    parts: int
    volumes_per_part: int
    acts_per_volume: int
    #: ``ai`` / ``fallback`` / ``""``（未评估）
    source: str = SOURCE_NONE
    #: 是否被代码夹过（**不许静默**）
    clamped: bool = False
    #: AI 给的依据
    reason: str = ""
    # ── A-07：卷内节奏建议（与网格**同一次**提案给出） ─────────────────
    #: 每卷高潮节点数（代码夹过 ⇒ 落在 [1, MAX_CLIMAXES_PER_VOLUME]）
    climaxes_per_volume: int = FALLBACK_CLIMAXES_PER_VOLUME
    #: 卷末钩子的具体形态（模型写的；空串 = 模型没写）
    hook_style: str = ""
    #: 卷内节奏是否被代码夹过（**不许静默**）
    pacing_clamped: bool = False
    #: 卷内节奏的来源：``ai`` / ``fallback``
    #: ⚠ 与 ``source``（**网格**的来源）**分开** ——
    #: 网格是 AI 给的、卷内节奏判不出来，是完全可能的（模型只答了三个数字）。
    #: ⇒ 两者合成一个 source 会让「卷内节奏其实是兜底」这件事被淹没。
    pacing_source: str = SOURCE_FALLBACK

    @property
    def total_acts(self) -> int:
        return self.parts * self.volumes_per_part * self.acts_per_volume

    def as_preference(self) -> Dict[str, int]:
        """给 ``_build_precise_structure_skeleton`` 用的结构偏好。"""
        return {
            "parts": self.parts,
            "volumes_per_part": self.volumes_per_part,
            "acts_per_volume": self.acts_per_volume,
        }

    def as_pacing_guide(self) -> str:
        """给 ``<PACING_GUIDE>`` 用的卷内节奏建议文本。

        A-07：这段原先由代码写死（「每卷宜安排 2~3 个大高潮节点」「卷末留强钩子」，
        拼在用户梗概前缀里、用户看不见也改不了）⇒ 现在由 AI 给出。

        ⚠ 兜底时**明说**是代码兜底 ⇒ 不把兜底冒充成 AI 的判断。
        """
        if self.pacing_source == SOURCE_AI:
            head = f"每卷安排约 {self.climaxes_per_volume} 个高潮节点"
        else:
            head = (
                f"每卷安排约 {self.climaxes_per_volume} 个高潮节点"
                "（代码兜底，AI 未给出可用的卷内节奏建议）"
            )
        lines = [head]
        if self.hook_style:
            lines.append(f"卷末钩子：{self.hook_style}")
        else:
            lines.append("卷末钩子：（未指定）")
        if self.reason:
            lines.append(f"依据：{self.reason}")
        return "\n".join(lines)


def _coerce_int(value: Any) -> Optional[int]:
    """把模型给的数字读成 int；读不出返回 ``None``（不猜）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def clamp_structure_grid(
    raw: Any,
    *,
    target_chapters: int,
) -> StructureGridProposal:
    """把网格**夹**进允许的范围（登记表 B-05 的两条不变量）。

    - 三层各自的层数 ∈ [1, 10]；
    - 幕数总数 ≤ 目标章数（每幕至少 1 章）⇒ 超出时压 ``acts_per_volume``；
    - 被夹过 ⇒ ``clamped=True``（调用方必须说出口）。

    ⚠ 模型没给出可用数字 ⇒ **未评估**（``source=""``），
    调用方回落代码兜底网格；这里**不编造**一个数字。
    """
    if not isinstance(raw, dict):
        return StructureGridProposal(
            *FALLBACK_GRID, source=SOURCE_FALLBACK, clamped=False
        )

    parts = _coerce_int(raw.get("parts"))
    volumes = _coerce_int(raw.get("volumes_per_part"))
    acts = _coerce_int(raw.get("acts_per_volume"))
    if parts is None or volumes is None or acts is None:
        return StructureGridProposal(
            *FALLBACK_GRID, source=SOURCE_FALLBACK, clamped=False
        )

    clamped = False
    if parts < 1:
        parts, clamped = 1, True
    if parts > MAX_PARTS:
        parts, clamped = MAX_PARTS, True
    if volumes < 1:
        volumes, clamped = 1, True
    if volumes > MAX_VOLUMES_PER_PART:
        volumes, clamped = MAX_VOLUMES_PER_PART, True
    if acts < 1:
        acts, clamped = 1, True
    if acts > MAX_ACTS_PER_VOLUME:
        acts, clamped = MAX_ACTS_PER_VOLUME, True

    # 防无限嵌套：幕数总数不得超过目标章数（每幕至少 1 章）。
    # ⚠ 只压「每卷幕数」**不够** —— 部×卷 本身就能超编（10 部 × 10 卷 = 100 卷，
    #   每卷 1 幕也是 100 幕 > 50 章）⇒ 三层依次压到装得下。
    chapters = max(int(target_chapters or 0), 1)
    max_acts = max(chapters // max(1, parts * volumes), 1)
    if acts > max_acts:
        acts, clamped = max_acts, True
    if parts * volumes * acts > chapters:
        max_volumes = max(chapters // max(1, parts * acts), 1)
        if volumes > max_volumes:
            volumes, clamped = max_volumes, True
    if parts * volumes * acts > chapters:
        max_parts = max(chapters // max(1, volumes * acts), 1)
        if parts > max_parts:
            parts, clamped = max_parts, True
    # 兜底：逐层各退一步，直到真的装得下
    while parts * volumes * acts > chapters and acts > 1:
        acts -= 1
        clamped = True
    while parts * volumes * acts > chapters and volumes > 1:
        volumes -= 1
        clamped = True
    while parts * volumes * acts > chapters and parts > 1:
        parts -= 1
        clamped = True

    reason = raw.get("reason")
    # ── A-07：卷内节奏（与网格同一次提案，省一次 LLM 调用） ───────────
    climaxes = _coerce_int(raw.get("climaxes_per_volume"))
    pacing_clamped = False
    if climaxes is None:
        # 判不出来 ⇒ 用兜底值，但**来源标记如实**：
        # 网格可能是 AI 给的、卷内节奏却是兜底 ⇒ 两个来源**分开记**。
        climaxes = FALLBACK_CLIMAXES_PER_VOLUME
        pacing_source = SOURCE_FALLBACK
    else:
        pacing_source = SOURCE_AI
        if climaxes < 1:
            climaxes, pacing_clamped = 1, True
        if climaxes > MAX_CLIMAXES_PER_VOLUME:
            climaxes, pacing_clamped = MAX_CLIMAXES_PER_VOLUME, True

    return StructureGridProposal(
        parts=parts,
        volumes_per_part=volumes,
        acts_per_volume=acts,
        source=SOURCE_AI,
        clamped=clamped,
        reason=str(reason).strip()[:400] if isinstance(reason, str) else "",
        climaxes_per_volume=climaxes,
        hook_style=str(raw.get("hook_style") or "").strip()[:400],
        pacing_clamped=pacing_clamped,
        pacing_source=pacing_source,
    )


class StructureProposalService:
    """让 AI 提案结构网格。"""

    def __init__(self, llm: LLMService):
        self._llm = llm

    async def propose(
        self,
        *,
        novel_id: str,
        title: str = "",
        genre: str = "",
        premise: str = "",
        target_chapters: int = 0,
    ) -> StructureGridProposal:
        """返回网格提案；判不出来 ⇒ ``source=""``（未评估，调用方回落兜底）。"""
        if self._llm is None or not ai_participation_enabled("structure_proposal"):
            return StructureGridProposal(*FALLBACK_GRID, source=SOURCE_NONE)

        try:
            rendered = get_prompt_gateway().render(
                STRUCTURE_PROPOSAL_CONTRACT,
                {
                    "novel_id": str(novel_id),
                    "title": str(title or "") or "（未提供）",
                    "genre": str(genre or "") or "（未提供）",
                    "premise": str(premise or "") or "（未提供）",
                    "target_chapters": str(int(target_chapters or 0)),
                    "max_parts": str(MAX_PARTS),
                    "max_volumes_per_part": str(MAX_VOLUMES_PER_PART),
                    "max_acts_per_volume": str(MAX_ACTS_PER_VOLUME),
                    # A-07：卷内节奏的上限也告诉模型。
                    "max_climaxes_per_volume": str(MAX_CLIMAXES_PER_VOLUME),
                },
            )
            config = generation_config_from_profile("structure_proposal")
            result = await self._llm.generate(rendered.prompt, config)
        except Exception as exc:
            logger.warning("结构网格提案调用失败，按未评估处理 novel=%s: %s", novel_id, exc)
            return StructureGridProposal(*FALLBACK_GRID, source=SOURCE_NONE)

        data = self._parse(self._extract_text(result))
        if data is None:
            return StructureGridProposal(*FALLBACK_GRID, source=SOURCE_NONE)
        return clamp_structure_grid(data, target_chapters=target_chapters)

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
            logger.warning("结构网格提案返回非 JSON，按未评估处理: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("结构网格提案返回的 JSON 不是对象，按未评估处理")
            return None
        try:
            validated = get_prompt_gateway().validate_output(
                STRUCTURE_PROPOSAL_CONTRACT, data
            )
        except Exception as exc:
            logger.warning("结构网格提案输出不合契约，按未评估处理: %s", exc)
            return None
        return validated.model_dump()
