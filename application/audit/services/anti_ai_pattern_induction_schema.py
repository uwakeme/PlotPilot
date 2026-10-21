"""F-06：AI 归纳「本书特有的 AI 味表达」的取值口径与校验。

改造前 ``anti_ai_learning`` 的「学习」是**代码用固定模板 + 固定触发词做统计**：
6 个结构模板正则（X地Y / 不禁X / 缓缓X / 微微X / 暗自X / 一X就Y）+ 7 个触发词
（仿佛/宛如/不禁/缓缓/微微/暗自/分明）—— 那不是归纳，而且「什么算 AI 味」本身
就是**代码代做的语义判断**。

现在由 AI 归纳，代码只做**不越界**的校验：

1. 关键词必须是**纯字面**（不含正则元字符）⇒ 无正则注入、无灾难性回溯；
2. 关键词**必须在本章正文里确实出现** ⇒ AI 编出来的模式一律丢弃；
3. 严重度必须在白名单内；
4. 长度上限（关键词 2..40，名字/依据各有限长）；
5. 与已知规则库不重复；
6. **三态**：AI 不可用 / 输出不合契约 ⇒ **不产生任何模式**（不编）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set

from pydantic import BaseModel, Field

#: 来源标记
SOURCE_AI = "ai"
SOURCE_NONE = ""

#: 允许的严重度
ALLOWED_SEVERITIES = ("info", "warning", "critical")

#: 关键词长度区间
MIN_KEYWORD_LEN = 2
MAX_KEYWORD_LEN = 40

#: 正则元字符 —— 关键词里出现任何一个就判定「不是字面量」⇒ 丢弃
_REGEX_METACHARS = set("\\.^$*+?()[]{}|")


class AntiAiPatternInductionItem(BaseModel):
    """一条归纳结果（契约形状）。

    ``keyword`` **必填**：它是这条规则的唯一身份，没有它这条归纳无意义。
    严重度只做类型约束（``info``/``warning``/``critical`` 的白名单在
    ``validate_induced_patterns`` 里逐条判），不写死枚举 ——
    否则一条非法严重度会让**整份**输出作废，与逐条丢弃的口径打架。
    """

    keyword: str = Field(description="字面关键词（必须在本章正文里出现过）")
    name: str = Field(default="", description="给这个模式起的短名")
    severity: str = Field(default="info", description="info / warning / critical")
    category: str = Field(default="", description="分类（默认「本书特有」）")
    reason: str = Field(default="", description="为什么它算 AI 味（一句话）")


class AntiAiPatternInductionLlmPayload(BaseModel):
    """LLM 返回的归纳结果。

    形状必须**真的约束得住**：``patterns`` 之前是 ``List[Dict[str, Any]]``，
    等于没约束 ⇒ 契约校验形同虚设（实测：跳过 ``validate_output``
    整套守卫也不红）。现在每条至少要有 ``keyword``。
    """

    patterns: List[AntiAiPatternInductionItem] = Field(default_factory=list)


@dataclass(frozen=True)
class InducedPattern:
    """一条通过校验的归纳结果。"""

    keyword: str
    name: str = ""
    severity: str = "info"
    category: str = "本书特有"
    reason: str = ""


def is_literal_keyword(keyword: str) -> bool:
    """是否是**纯字面**关键词（不含正则元字符、不含空白）。"""
    if not keyword or len(keyword) < MIN_KEYWORD_LEN or len(keyword) > MAX_KEYWORD_LEN:
        return False
    if keyword != keyword.strip():
        return False
    if any(ch.isspace() for ch in keyword):
        return False
    return not (_REGEX_METACHARS & set(keyword))


def validate_induced_patterns(
    data: Optional[Dict[str, Any]],
    *,
    content: str,
    known_keywords: Optional[Iterable[str]] = None,
) -> List[InducedPattern]:
    """把 LLM 输出收成通过校验的归纳结果。

    - 形状不对 ⇒ 空列表（**未评估**，不编）
    - 关键词非字面 / 超长 / 非法严重度 ⇒ 丢弃
    - **关键词不在正文里出现 ⇒ 丢弃**（AI 编的）
    - 与已知规则库重复 ⇒ 丢弃
    """
    if not isinstance(data, dict):
        return []
    raw = data.get("patterns")
    if not isinstance(raw, list):
        return []

    known: Set[str] = {str(k) for k in (known_keywords or [])}
    text = content or ""
    out: List[InducedPattern] = []
    seen: Set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        keyword = str(item.get("keyword") or "").strip()
        if not is_literal_keyword(keyword):
            continue
        if keyword in known or keyword in seen:
            continue
        if keyword not in text:
            # AI 编出来的模式：正文里根本没有 ⇒ 丢弃
            continue
        severity = str(item.get("severity") or "info").strip().lower()
        if severity not in ALLOWED_SEVERITIES:
            continue
        seen.add(keyword)
        out.append(
            InducedPattern(
                keyword=keyword,
                name=str(item.get("name") or "").strip()[:60],
                severity=severity,
                category=str(item.get("category") or "本书特有").strip()[:30] or "本书特有",
                reason=str(item.get("reason") or "").strip()[:200],
            )
        )
    return out
