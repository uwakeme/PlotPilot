"""E-04：武侠题材的「战斗逻辑」与「现代口语」判定口径与三态。

改造前
------
``engine/pipelines/wuxia_pipeline.py`` 用**代码**判两条题材规则：

- 「一招秒杀」：**两条正则**（``一招.{0,4}(秒杀|击杀|击毙|毙命)`` /
  ``随手一.{0,2}(毙|杀|灭)``）⇒ 命中就报 ``combat_logic``；
- 「现代口语」：**恰 7 个词**的黑名单（搞定 / 没毛病 / 杠杠的 / 绝绝子 /
  yyds / 666 / 离谱）。

⇒ 「什么算无铺垫的一招秒杀」与「什么算现代口语」都是**代码代做的语义判断**：
正则只认字面，词表外的写法一律漏报（「一记秒了」「这波稳了」「666」写成
「六六大顺」等），而**真正**违反战斗逻辑的写法（没有境界差就秒杀）反而抓不到。

现在由 **AI 审**（``wuxia-style-audit`` 节点）：它看本章正文，判断
① 有没有**违反武功逻辑**的描写 ② 有没有**现代口语**。
代码只做**不越界**的校验。

三态
----
- ``ai``：AI 审过了（哪怕它说「没有违规」也算审过）；
- ``fallback``：AI 不可用 ⇒ 回落**代码表**（改造前的行为，逐位一致），
  来源可分辨；
- ``""``（未评估）：连代码表都没跑成（正文为空）⇒ 没有结论。

不变量（代码保证，AI 动不了）
----------------------------
1. **type 封闭枚举**：只有 ``combat_logic`` / ``modern_slang`` 两种；
2. **证据必须逐字出现在正文里** —— AI 编的证据一律丢弃；
3. **有结论必须有依据**：``description`` 不得为空。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

#: 来源标记
SOURCE_AI = "ai"
SOURCE_FALLBACK = "fallback"
SOURCE_NONE = ""

#: 违反武功逻辑（无铺垫的一招秒杀）
TYPE_COMBAT_LOGIC = "combat_logic"
#: 现代口语混进武侠对白
TYPE_MODERN_SLANG = "modern_slang"
#: 违规类型封闭枚举（AI 只能在这两种里选）
VIOLATION_TYPES = (TYPE_COMBAT_LOGIC, TYPE_MODERN_SLANG)

#: 单条证据长度上限
MAX_EVIDENCE_LEN = 60

#: 每种违规的默认建议（AI 没给就用它 —— 与改造前的代码兜底一致）
SUGGESTION_BY_TYPE = {
    TYPE_COMBAT_LOGIC: "如确需秒杀，前面应有充分铺垫（暗器/毒/偷袭/境界碾压）",
    TYPE_MODERN_SLANG: "替换为古白话表达",
}


@dataclass(frozen=True)
class StyleViolation:
    """一条违规。"""

    type: str
    evidence: str
    description: str
    severity: float = 0.7
    suggestion: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension": "wuxia",
            "type": self.type,
            "severity": self.severity,
            "description": self.description,
            "suggestion": self.suggestion
            or SUGGESTION_BY_TYPE.get(self.type, ""),
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class StyleAudit:
    """一次审的结果（含来源标记）。"""

    violations: List[StyleViolation] = field(default_factory=list)
    source: str = SOURCE_NONE

    @property
    def is_usable(self) -> bool:
        """是否为「AI 真的答过了」（答「没有违规」也算答过）。"""
        return self.source == SOURCE_AI


class WuxiaStyleViolationItem(BaseModel):
    """AI 返回的一条违规。"""

    type: str = Field(default="", description="combat_logic | modern_slang")
    evidence: str = Field(default="", description="原文片段（必须逐字出现在正文里）")
    description: str = Field(default="", description="为什么算违规（不得为空）")
    severity: float = Field(default=0.7, description="严重度 0~1")
    suggestion: str = Field(default="", description="修改建议（可空，用代码默认）")


class WuxiaStyleAuditLlmPayload(BaseModel):
    """wuxia-style-audit 节点的 LLM 输出。"""

    violations: List[WuxiaStyleViolationItem] = Field(default_factory=list)
    reason: str = Field(default="", description="整体说明（可空）")


def normalize_style_audit(
    data: Optional[Dict[str, Any]],
    *,
    content: str,
) -> StyleAudit:
    """把 AI 的输出收成可用的审结果。

    - 形状不对 ⇒ **未评估**（``source=""``）；
    - ``type`` 不在封闭枚举里 ⇒ 该条丢弃；
    - ``evidence`` **不逐字出现在正文里** ⇒ 该条丢弃（AI 编的证据一律丢弃）；
    - ``description`` 为空 ⇒ 该条丢弃（有结论必须有依据）；
    - AI **给了违规但一条都用不了** ⇒ 判为**未评估**（这份回答不可信），
      调用方按代码表兜底处理；
    - AI 合法地说「没有违规」（空列表）⇒ ``source="ai"``，**不回退**。
    """
    if not isinstance(data, dict):
        return StyleAudit([], SOURCE_NONE)
    raw = data.get("violations")
    if not isinstance(raw, list):
        return StyleAudit([], SOURCE_NONE)

    text = content or ""
    out: List[StyleViolation] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        vtype = str(item.get("type") or "").strip()
        if vtype not in VIOLATION_TYPES:
            continue
        evidence = str(item.get("evidence") or "").strip()
        if not evidence or len(evidence) > MAX_EVIDENCE_LEN:
            continue
        if evidence not in text:
            # 证据必须**逐字**出现在正文里 —— AI 编的一律丢弃
            continue
        description = str(item.get("description") or "").strip()
        if not description:
            continue
        try:
            severity = float(item.get("severity", 0.7))
        except (TypeError, ValueError):
            severity = 0.7
        severity = min(1.0, max(0.0, severity))
        out.append(
            StyleViolation(
                type=vtype,
                evidence=evidence,
                description=description,
                severity=severity,
                suggestion=str(item.get("suggestion") or "").strip(),
            )
        )

    if raw and not out:
        return StyleAudit([], SOURCE_NONE)
    return StyleAudit(out, SOURCE_AI)
