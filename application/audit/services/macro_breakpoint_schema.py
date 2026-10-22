"""H-06 子项 A：宏观诊断的「断点分类」口径与三态。

改造前，断点是什么类型**全由代码判**（``macro_refactor_scanner.py``）：

- ``TRAIT_CONFLICT_RULES`` —— 一张**写死 4 个人设标签**的表
  （冷酷 / 理性 / 谨慎 / 温和）；表外的人设标签**一个断点都发现不了**，
  而生产路径恰好只扫这 4 个标签（``MacroDiagnosisService.DEFAULT_SCAN_TRAITS``）
  ⇒ 诊断的对象与本书角色**无关**。
- ``_classify_breakpoint`` —— scar 命中 ⇒ 应激；执念 priority≥8 ⇒ 突破；
  **其余全部落 OOC**。而「其余」里包含四种**信息不足**的情形：
  没有人物状态仓储 / 提不出角色 / 没有角色状态 / 没找到任何触发原因
  ⇒ **「判不出」被说成「人设崩塌」**（落库后的假结论）。

现在由 **AI 做语义诊断**：它看到「角色 + 他的人设 / 伤疤 / 执念 / 当前状态」
与「候选事件清单」，判断每条事件**是不是偏离、属于哪一类**。
代码只做**不越界**的校验，不代做语义判断。

三态
----
- ``ai``：AI 判过了（哪怕它说「都不偏离」也算判过 ⇒ **不回退**）；
- ``fallback``：AI 不可用 ⇒ 回落代码规则判定（改造前的行为），来源可分辨；
- ``""``（未评估）：连规则也判不出 ⇒ 断点类型落 ``BreakpointType.UNASSESSED``，
  **不落 OOC**（「模型说没有」与「没评估」必须可分辨 —— 登记表三态红线）。

不变量（代码保证，AI 动不了）
----------------------------
1. **事件白名单**：``event_id`` 必须来自候选清单 —— AI 不得凭空造事件；
2. **角色白名单**：``character_id`` 必须来自角色清单（留空表示「提不出归属」）；
3. **verdict 封闭枚举**：只有 ``deviation`` / ``breakout`` / ``scar_triggered`` /
   ``not_deviation`` / ``unclear`` 五种；
4. **有结论必须有依据**：``verdict != unclear`` 时 ``reason`` 不得为空
   —— 不许给一个没有理由的判定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from application.audit.dtos.macro_refactor_dto import (
    SOURCE_AI,
    SOURCE_NONE,
    SOURCE_RULE,
)
from domain.novel.value_objects.character_state import BreakpointType

#: 真崩塌 —— 没有合理原因的偏离，需要把人设拉回来
VERDICT_DEVIATION = "deviation"
#: 人物突破 —— 偏离有正当理由（成长到了那一步），是高光时刻，**不要**拉回
VERDICT_BREAKOUT = "breakout"
#: 应激反应 —— 旧伤被触发的条件反射，不是性格变了
VERDICT_SCAR_TRIGGERED = "scar_triggered"
#: 不偏离 —— 这条事件与该角色的人设一致
VERDICT_NOT_DEVIATION = "not_deviation"
#: 判不出来 —— 信息不足，**不下结论**
VERDICT_UNCLEAR = "unclear"

#: verdict 封闭枚举（AI 只能在这五种里选）
VERDICTS = (
    VERDICT_DEVIATION,
    VERDICT_BREAKOUT,
    VERDICT_SCAR_TRIGGERED,
    VERDICT_NOT_DEVIATION,
    VERDICT_UNCLEAR,
)

#: verdict → 断点类型（**只有前三个会产生断点**）
VERDICT_TO_BREAKPOINT_TYPE: Dict[str, BreakpointType] = {
    VERDICT_DEVIATION: BreakpointType.OOC,
    VERDICT_BREAKOUT: BreakpointType.CHARACTER_BREAKOUT,
    VERDICT_SCAR_TRIGGERED: BreakpointType.SCAR_TRIGGERED,
}

#: 会产生断点的 verdict
DEVIATION_VERDICTS = frozenset(VERDICT_TO_BREAKPOINT_TYPE)


@dataclass(frozen=True)
class BreakpointVerdict:
    """AI 对**一条**事件给出的判定。"""

    event_id: str
    character_id: str
    verdict: str
    reason: str

    @property
    def is_deviation(self) -> bool:
        """是否构成断点（``not_deviation`` / ``unclear`` 都不构成）。"""
        return self.verdict in DEVIATION_VERDICTS

    def __repr__(self) -> str:  # pragma: no cover - 仅为断言失败信息可读
        return (
            f"BreakpointVerdict(event_id={self.event_id!r}, "
            f"character_id={self.character_id!r}, verdict={self.verdict!r})"
        )


@dataclass(frozen=True)
class BreakpointClassification:
    """一次分类的结果（含来源标记）。

    ``verdicts`` 按 ``event_id`` 索引，**包含** ``not_deviation`` / ``unclear``
    —— 它们是「AI 判过了」的证据，调用方据此知道这件事**已经被判过**，
    不该再让规则结论（尤其是 OOC）覆盖上去。
    """

    verdicts: Dict[str, BreakpointVerdict] = field(default_factory=dict)
    source: str = SOURCE_NONE

    @property
    def is_usable(self) -> bool:
        """是否为「AI 真的答过了」（答「都不偏离」也算答过）。"""
        return self.source == SOURCE_AI

    def deviation_event_ids(self) -> List[str]:
        """构成断点的事件 ID。"""
        return [
            event_id
            for event_id, verdict in self.verdicts.items()
            if verdict.is_deviation
        ]


class MacroBreakpointVerdictItem(BaseModel):
    """AI 返回的一条判定。"""

    event_id: str = Field(default="", description="事件 ID（必须来自候选清单）")
    character_id: str = Field(
        default="", description="角色 ID（必须来自角色清单；提不出归属可留空）"
    )
    verdict: str = Field(
        default="",
        description=(
            "deviation（真崩塌）| breakout（人物突破）| scar_triggered（应激反应）"
            " | not_deviation（不偏离）| unclear（判不出来）"
        ),
    )
    reason: str = Field(default="", description="判断依据（verdict != unclear 时不得为空）")


class MacroBreakpointLlmPayload(BaseModel):
    """planning-macro-breakpoint 节点的 LLM 输出。"""

    verdicts: List[MacroBreakpointVerdictItem] = Field(default_factory=list)
    reason: str = Field(default="", description="整体说明（可空）")


def normalize_classification(
    data: Optional[Dict[str, Any]],
    *,
    valid_event_ids: Any,
    valid_character_ids: Any,
) -> BreakpointClassification:
    """把 AI 的输出收成可用的分类结果。

    - 形状不对 ⇒ **未评估**（``source=""``）；
    - ``event_id`` 不在候选清单 ⇒ 该条丢弃（**AI 不得凭空造事件**）；
    - ``character_id`` 不在角色清单 ⇒ 该条丢弃（**AI 不得凭空造人**）；
    - ``verdict`` 不在封闭枚举里 ⇒ 该条丢弃；
    - ``verdict != unclear`` 而 ``reason`` 为空 ⇒ 该条丢弃
      （不许给一个没有理由的判定）；
    - AI **给了判定但一条都用不了** ⇒ 判为**未评估**（这份回答不可信），
      调用方按兜底处理；
    - AI 合法地回答「都不偏离」⇒ ``source="ai"``，**不回退**
      （模型说没有 ≠ 没评估）。
    """
    if not isinstance(data, dict):
        return BreakpointClassification({}, SOURCE_NONE)
    raw = data.get("verdicts")
    if not isinstance(raw, list):
        return BreakpointClassification({}, SOURCE_NONE)

    allowed_events = {
        str(e).strip() for e in (valid_event_ids or []) if str(e).strip()
    }
    allowed_chars = {
        str(c).strip() for c in (valid_character_ids or []) if str(c).strip()
    }

    out: Dict[str, BreakpointVerdict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        event_id = str(item.get("event_id") or "").strip()
        if not event_id or event_id not in allowed_events:
            continue
        verdict = str(item.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            continue
        reason = str(item.get("reason") or "").strip()
        if verdict != VERDICT_UNCLEAR and not reason:
            continue
        character_id = str(item.get("character_id") or "").strip()
        if character_id and character_id not in allowed_chars:
            continue
        if event_id in out:
            # 同一事件只取第一条（AI 重复给同一事件时不应叠加）
            continue
        out[event_id] = BreakpointVerdict(
            event_id=event_id,
            character_id=character_id,
            verdict=verdict,
            reason=reason,
        )

    if raw and not out:
        # AI 明明给了判定，却一条都不可用 ⇒ 这份回答不可信 ⇒ 未评估
        return BreakpointClassification({}, SOURCE_NONE)
    return BreakpointClassification(out, SOURCE_AI)
