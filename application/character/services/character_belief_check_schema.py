"""H-03：角色行为一致性判断的取值口径与三态。

改造前这段是**代码用关键词表 + 正则猜**的（`CharacterMask.validate_behavior`）：

- 信念里出现「不信任 / 警惕 / 谨慎 …」⇒ 套「{名字}{0,8}(毫不犹豫地相信|完全信任|…)」；
- 信念里出现「力量 / 强者为尊 …」⇒ 套「{名字}{0,8}(退缩|害怕|求饶|示弱|妥协)」；
- 禁忌用**字面包含**判（``taboo in action``）；
- 旧伤用**触发词字面包含**判（``trigger in context``）。

四段全是**语义判断**被写成了字符串匹配，既漏报（换个说法就抓不到）又误报
（「他不再不信任任何人」会命中「不信任」这条禁忌）。登记表 H-03 的判定是 C：
**LLM 做「信念+行为」蕴含判断**。

本模块定义判断结果的口径：

- **AI 判语义**（信念 / 禁忌 / 旧伤 / 语体），代码不再代做；
- **三态**：判定成功 / 未评估（LLM 不可用或输出不合契约）。
  未评估时 ``valid`` 必须是 ``None``，**不能报 ``valid=True``** ——
  「没查过」与「查过没问题」不是一回事。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

#: 来源标记
SOURCE_AI = "ai"
SOURCE_NONE = ""

#: 允许的 finding 类型（封闭枚举 —— 代码只校验，不裁决）
FINDING_TYPES = ("belief", "taboo", "wound", "voice")
#: 允许的严重级别
FINDING_SEVERITIES = ("critical", "warning", "info")


class CharacterBeliefCheckLlmPayload(BaseModel):
    """LLM 返回的结构化判定结果。"""

    consistent: bool = Field(default=True)
    findings: List[Dict[str, Any]] = Field(default_factory=list)


@dataclass(frozen=True)
class BehaviorVerdict:
    """一次行为一致性判定。

    ``evaluated=False`` ⇒ **未评估**（``valid`` 必为 ``None``）。
    """

    evaluated: bool
    valid: Optional[bool] = None
    warnings: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    source: str = SOURCE_NONE

    @property
    def is_usable(self) -> bool:
        return self.evaluated


UNASSESSED = BehaviorVerdict(False, None, [], [], SOURCE_NONE)


def normalize_verdict(data: Optional[Dict[str, Any]]) -> BehaviorVerdict:
    """把 LLM 输出收成判定结果；形状不对 ⇒ 未评估。"""
    if not isinstance(data, dict):
        return UNASSESSED
    raw_consistent = data.get("consistent")
    if not isinstance(raw_consistent, bool):
        return UNASSESSED
    raw_findings = data.get("findings")
    if not isinstance(raw_findings, list):
        raw_findings = []

    warnings: List[str] = []
    suggestions: List[str] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        ftype = str(item.get("type") or "").strip()
        if ftype and ftype not in FINDING_TYPES:
            # 不认识的类型不静默吞掉 —— 契约变了，要看得见
            continue
        severity = str(item.get("severity") or "info").strip()
        if severity not in FINDING_SEVERITIES:
            severity = "info"
        message = str(item.get("message") or "").strip()[:300]
        if not message:
            continue
        warnings.append(f"[{severity}] {message}")
        suggestion = str(item.get("suggestion") or "").strip()[:200]
        if suggestion:
            suggestions.append(suggestion)

    return BehaviorVerdict(
        evaluated=True,
        valid=bool(raw_consistent),
        warnings=warnings,
        suggestions=suggestions,
        source=SOURCE_AI,
    )
