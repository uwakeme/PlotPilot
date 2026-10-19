"""G-01 / G-03：伏笔回收节奏与故事线收尾章 —— AI 优先，兜底显式标注。

背景
----
两条都是同一种病：**AI 通道完好，代码用一个常数顶替，且顶替后无从分辨**。

G-01（伏笔回收偏移）
    prompt 本就要求模型给 ``suggested_resolve_offset``，代码也确实读了
    （``chapter_narrative_sync.py:605``）。但模型省略时静默回落成固定的 5，
    与「模型说 5」在数据上完全一样 —— 于是「模型判断 5 章后回收」与
    「模型没说话」被压成同一个值。

G-03（故事线预估收尾章）
    ``estimated_chapter_end = chapter_number + 12`` / ``+ 20``，
    两个不同位置各写一个常数，同样无从分辨是模型给的还是代码补的。

本模块提供的正是 B-03 那套模板的复用：**取值 + 来源标记 + 不变量校验**。

不变量（登记表「代码保留的不变量」列）
----------------------------------------
G-01 回收章 > 埋设章
G-03 收尾章 > 当前章

⚠️ 三态红线
-----------
「模型没给」与「模型给的就是这个值」必须可区分。
``source`` 字段就是为此存在 —— 兜底时它等于 ``"fallback"``，
调用方与后续消费者据此知道这个数字**不是模型的判断**。
"""
from __future__ import annotations

import math
from typing import Any, Optional, Tuple

#: 回收偏移的既有区间（沿用改造前的 2-30 章限制）
RESOLVE_OFFSET_MIN = 2
RESOLVE_OFFSET_MAX = 30

#: 兜底回收偏移（沿用改造前的 5）
DEFAULT_RESOLVE_OFFSET = 5

#: 故事线预估收尾章的兜底跨度
DEFAULT_STORYLINE_SPAN = 12

#: 故事线预估收尾章的绝对上限（章）。
#: 超出它的值（典型如 10**400）不可能是真实创作判断，原样落库只会在产物里
#: 放一个看着像「模型判断」、实则毫无意义的数字 ⇒ 视同不可用走 fallback。
MAX_STORYLINE_END = 10_000

SOURCE_AI = "ai"
SOURCE_FALLBACK = "fallback"
#: 模型给了值，但**被代码改写**（抬到不变量允许的下界）才算最终值。
#: ⚠ 不能并进 SOURCE_AI —— 上一轮把 source 落库之后，「ai」会在产物里
#: 声称「第 11 章是模型判断的」，而模型给的是 8。那是**落库后的假来源**，
#: 比落日志时的不精确严重得多：它会被下游当成可信 provenance 消费。
SOURCE_AI_ADJUSTED = "ai_adjusted"


def resolve_offset(raw: Any) -> Tuple[int, str, str]:
    """裁决伏笔回收偏移。返回 ``(偏移, 来源, 说明)``。

    - 模型给了且在 [2,30] 内 ⇒ 采用，来源 ``"ai"``
    - 模型给了但**被钳位** ⇒ 来源 ``"ai_adjusted"``：最终取值不是模型原值
    - 模型没给 / 给了脏值 / 给了非有限数 ⇒ 用兜底 5，来源 ``"fallback"``

    ⚠ 钳位不能标 ``"ai"``。该 source 会随
    ``Foreshadowing.resolve_offset_source`` 落 registry JSON；只消费 source 的
    下游会以为「第 2 章是模型说的」，而模型给的是 0。落库后的假来源比
    日志里措辞不精确严重得多。与 :func:`resolve_storyline_end` 的抬升同一条原则。

    ⚠ 非有限值（NaN / ±Inf / 1e400）必须走 ``fallback`` 而不是钳位。
    它们**比较运算全是 False**（``max(2, min(30, nan))`` 的结果取决于参数顺序），
    且 ``int(float('nan'))`` 直接抛 ValueError / OverflowError。
    改造前用 ``int(raw)`` 在 try 内，因此这些值会安全回落；改成 ``float(raw)``
    后转换被挪到 try 外 ⇒ 崩溃回归。这里把「转换」与「有限性校验」一并收进 try，
    恢复旧语义：任何不可用数值都记 ``fallback``，绝不把代码异常抛给调用方。

    ⚠ note 必须自述**最终采用值**。``1.9`` 截断得 1、再钳到 2，若只写
    「已截断为 1」，落 registry 的就是与实际取值矛盾的假 provenance。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_RESOLVE_OFFSET, SOURCE_FALLBACK, "模型未给出回收偏移，按兜底 5 章"

    # ⚠ 转换与有限性校验必须在同一个 try 内 —— 分开放会让 NaN/Inf
    # 在 int() 处抛出去，而调用方的 except 只会记 warning，
    # 结果是整章伏笔状态更新被静默跳过（数据丢失，不是崩溃）。
    try:
        numeric = float(raw)
        if not math.isfinite(numeric):
            raise ValueError(f"non-finite: {raw!r}")
        truncated_to = int(numeric) if numeric != int(numeric) else None
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_RESOLVE_OFFSET, SOURCE_FALLBACK, (
            f"模型给出的回收偏移不可用（{raw!r}），按兜底 5 章"
        )

    if truncated_to is not None:
        # 先 int 截断，再钳位 —— 两者都可能改变取值。
        bounded = max(RESOLVE_OFFSET_MIN, min(RESOLVE_OFFSET_MAX, truncated_to))
        if bounded != truncated_to:
            return bounded, SOURCE_AI_ADJUSTED, (
                f"模型给出的回收偏移 {raw!r} 不是整数，先截断为 {truncated_to}、"
                f"再钳到 {bounded}（采用值非模型原值）"
            )
        return bounded, SOURCE_AI_ADJUSTED, (
            f"模型给出的回收偏移 {raw!r} 不是整数，已截断为 {bounded}（采用值非模型原值）"
        )

    value = int(numeric)
    bounded = max(RESOLVE_OFFSET_MIN, min(RESOLVE_OFFSET_MAX, value))
    if bounded != value:
        return bounded, SOURCE_AI_ADJUSTED, (
            f"模型给出的回收偏移 {value} 被钳到 [{RESOLVE_OFFSET_MIN}, {RESOLVE_OFFSET_MAX}]"
            f"（采用值非模型原值）"
        )
    return bounded, SOURCE_AI, "采用模型给出的回收偏移"


def resolve_storyline_end(
    chapter_number: int,
    ai_end: Any = None,
    *,
    span: int = DEFAULT_STORYLINE_SPAN,
) -> Tuple[int, str, str]:
    """裁决故事线预估收尾章。返回 ``(收尾章, 来源, 说明)``。

    不变量：收尾章 > 当前章。模型给了就用模型的（钳到至少 +1 章）。

    ⚠ 与 :func:`resolve_offset` 同一条纪律，三点对齐：
    1. **非有限值走 fallback**：``int(float('inf'))`` / ``float(10**400)`` 抛
       OverflowError，而调用方 `except` 只会记 warning ⇒ 该章故事线状态更新
       被静默跳过。转换与有限性校验必须收在同一个 try 内。
    2. **超大整数不再原样返回**：``10**400`` 曾直接成为 estimated_chapter_end
       且标 ``ai`` —— 一个「第 10**400 章收尾」的值看着像模型判断，其实毫无意义。
       超过 :data:`MAX_STORYLINE_END` 记 ``fallback``（不静默改写成别的值）。
    3. **float 截断标 adjusted**：``11.9 → 11`` 是代码改写过取值，与钳位同性质。
    """
    current = max(int(chapter_number), 0)
    if ai_end is None or (isinstance(ai_end, str) and not ai_end.strip()):
        return current + max(int(span), 1), SOURCE_FALLBACK, (
            f"模型未给出预估收尾章，按兜底 +{max(int(span), 1)} 章"
        )

    try:
        numeric = float(ai_end)
        if not math.isfinite(numeric):
            raise ValueError(f"non-finite: {ai_end!r}")
        truncated_to = int(numeric) if numeric != int(numeric) else None
    except (TypeError, ValueError, OverflowError):
        return current + max(int(span), 1), SOURCE_FALLBACK, (
            f"模型给出的预估收尾章不可用（{ai_end!r}），按兜底 +{max(int(span), 1)} 章"
        )

    if truncated_to is not None:
        # ⚠ float 被代码截断 ⇒ 最终取值非模型原值 ⇒ 标 adjusted，不标 ai。
        value = truncated_to
        source, why = SOURCE_AI_ADJUSTED, (
            f"模型给出的预估收尾章 {ai_end!r} 不是整数，已截断为 {truncated_to}"
            f"（采用值非模型原值）"
        )
    else:
        value = int(numeric)
        source, why = SOURCE_AI, f"模型给出的预估收尾章 {value}"

    if value > MAX_STORYLINE_END:
        # 超出任何现实小说的跨度 ⇒ 视同不可用，而不是原样落库。
        return current + max(int(span), 1), SOURCE_FALLBACK, (
            f"模型给出的预估收尾章 {value} 超出上限 {MAX_STORYLINE_END}，"
            f"按兜底 +{max(int(span), 1)} 章"
        )

    if value <= current:
        # 不变量：收尾章必须晚于当前章。
        # ⚠ 抬升后的值**不是**模型给的值 ⇒ 标 SOURCE_AI_ADJUSTED，不标 SOURCE_AI。
        # source 已落库（storylines.estimated_chapter_end_source），标错就是在
        # 产物里伪造 provenance。
        return current + 1, SOURCE_AI_ADJUSTED, (
            f"模型给出的收尾章 {value} 不晚于当前章 {current}，"
            f"已由代码抬到第 {current + 1} 章（取值非模型原值）"
        )
    return value, source, why


def check_resolve_after_plant(planted_chapter: int, resolve_chapter: int) -> Optional[str]:
    """G-01 不变量：回收章必须晚于埋设章。越界返回错误描述，否则 None。"""
    if resolve_chapter <= planted_chapter:
        return (
            f"回收章 {resolve_chapter} 不晚于埋设章 {planted_chapter}"
            f"（伏笔不能在同一章种下又回收）"
        )
    return None
