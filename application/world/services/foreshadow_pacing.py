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

from typing import Any, Optional, Tuple

#: 回收偏移的既有区间（沿用改造前的 2-30 章限制）
RESOLVE_OFFSET_MIN = 2
RESOLVE_OFFSET_MAX = 30

#: 兜底回收偏移（沿用改造前的 5）
DEFAULT_RESOLVE_OFFSET = 5

#: 故事线预估收尾章的兜底跨度
DEFAULT_STORYLINE_SPAN = 12

SOURCE_AI = "ai"
SOURCE_FALLBACK = "fallback"


def resolve_offset(raw: Any) -> Tuple[int, str, str]:
    """裁决伏笔回收偏移。返回 ``(偏移, 来源, 说明)``。

    - 模型给了且可解析 ⇒ 钳到 [2, 30]，来源 ``"ai"``
    - 模型没给 / 给了脏值 ⇒ 用兜底 5，来源 ``"fallback"``，说明写清原因
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_RESOLVE_OFFSET, SOURCE_FALLBACK, "模型未给出回收偏移，按兜底 5 章"

    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RESOLVE_OFFSET, SOURCE_FALLBACK, f"模型给出的回收偏移不可解析（{raw!r}），按兜底 5 章"

    bounded = max(RESOLVE_OFFSET_MIN, min(RESOLVE_OFFSET_MAX, value))
    if bounded != value:
        return bounded, SOURCE_AI, (
            f"模型给出的回收偏移 {value} 被钳到 [{RESOLVE_OFFSET_MIN}, {RESOLVE_OFFSET_MAX}]"
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
    """
    current = max(int(chapter_number), 0)
    if ai_end is None or (isinstance(ai_end, str) and not ai_end.strip()):
        return current + max(int(span), 1), SOURCE_FALLBACK, (
            f"模型未给出预估收尾章，按兜底 +{max(int(span), 1)} 章"
        )

    try:
        value = int(ai_end)
    except (TypeError, ValueError):
        return current + max(int(span), 1), SOURCE_FALLBACK, (
            f"模型给出的预估收尾章不可解析（{ai_end!r}），按兜底 +{max(int(span), 1)} 章"
        )

    if value <= current:
        # 不变量：收尾章必须晚于当前章
        return current + 1, SOURCE_AI, (
            f"模型给出的收尾章 {value} 不晚于当前章 {current}，已抬到第 {current + 1} 章"
        )
    return value, SOURCE_AI, "采用模型给出的预估收尾章"


def check_resolve_after_plant(planted_chapter: int, resolve_chapter: int) -> Optional[str]:
    """G-01 不变量：回收章必须晚于埋设章。越界返回错误描述，否则 None。"""
    if resolve_chapter <= planted_chapter:
        return (
            f"回收章 {resolve_chapter} 不晚于埋设章 {planted_chapter}"
            f"（伏笔不能在同一章种下又回收）"
        )
    return None
