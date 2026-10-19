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

#: 故事线预估收尾章的**模型值**上限（章）。
#: 超出它的值（典型如 10**400）不可能是真实创作判断，原样落库只会在产物里
#: 放一个看着像「模型判断」、实则毫无意义的数字 ⇒ 视同不可用走 fallback。
#:
#: ⚠ 它**只约束模型给的值**，不约束 current / 兜底 / 抬升三条路径 ——
#: 那三条路径必须守住「收尾章 > 当前章」这条不变量，而 current 可能
#: （理论上）已经超过本上限。第十轮复审：曾用 min(current+step, MAX) 一刀切，
#: 结果 current ≥ MAX 时 end ≤ current，不变量被破坏、note 出现「+-5000 章」。
#: 不变量优先于上限。
MAX_STORYLINE_END = 10_000

SOURCE_AI = "ai"
SOURCE_FALLBACK = "fallback"
#: 模型给了值，但**被代码改写**（抬到不变量允许的下界）才算最终值。
#: ⚠ 不能并进 SOURCE_AI —— 上一轮把 source 落库之后，「ai」会在产物里
#: 声称「第 11 章是模型判断的」，而模型给的是 8。那是**落库后的假来源**，
#: 比落日志时的不精确严重得多：它会被下游当成可信 provenance 消费。
SOURCE_AI_ADJUSTED = "ai_adjusted"

#: note 里回显原始值的最大字符数（第九轮复审 §3.4）。
#: ``10**400`` 的 repr 是 401 位数字，整段塞进 note 会把 registry JSON /
#: Storyline 的 note 字段撑成几百字节 —— 而 provenance 只需要「模型给了个
#: 荒谬的大数」这个事实，不需要完整数值。
_BRIEF_MAX_CHARS = 40


def _brief(raw: Any, limit: int = _BRIEF_MAX_CHARS) -> str:
    """回显原始值，但截断到 ``limit`` 字符并标注省略量。

    ⚠ 不能直接用 ``{raw!r}``：``10**400`` 会产出 401 位数字的 note。

    ⚠⚠ 第十一轮复审：``repr(raw)`` **本身也可能抛异常** ——
    CPython 3.11+ 对 ``int → str`` 有 4300 位硬限制（``sys.set_int_max_str_digits``），
    ``repr(10**5000)`` 直接 ``ValueError: Exceeds the limit (4300 digits)``。
    而本函数在**三条 note 通道**的公共路径上：offset / plant / storyline
    全被它打穿成 ValueError，违反「裁决器对任何输入都不抛」这条契约
    （调用方的 except 只会记 warning ⇒ 该章状态更新被静默跳过）。

    修法：先用 ``bit_length()``（不涉及字符串转换，永远安全）描述超大整数，
    根本不去 repr 它。
    """
    if isinstance(raw, int) and not isinstance(raw, bool):
        # bit_length 不做 int→str 转换，对任意大的 int 都安全
        digits_est = (raw.bit_length() * 302 // 1000) + 1   # log10(2)*bits ≈ 0.301
        if raw.bit_length() > 4096:
            return f"<超大整数 约 {digits_est} 位>"
    try:
        text = repr(raw)
    except Exception as e:
        # ⚠ 第十一轮复审：不能只捕 (ValueError, RecursionError) ——
        # `__repr__` 是用户代码，可以抛**任意**异常（RuntimeError 等）。
        # 这里只做「把值转成可读文本」这一件纯展示的事，
        # 绝不该因为它失败而把异常抛给裁决流程。
        # （这里也**不该**用 BaseException —— KeyboardInterrupt 要能透出。）
        return f"<无法 repr 的值 类型={type(raw).__name__}: {type(e).__name__}>"
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…（共 {len(text)} 字符，已截断）"


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
    整数钳位同样如此：「被钳到 [2, 30]」的末位数字是 30 而不是实际采用值 2，
    下游按末位读会读错 —— 措辞必须是「…被钳到 2（区间 [2,30]）」。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_RESOLVE_OFFSET, SOURCE_FALLBACK, "模型未给出回收偏移，按兜底 5 章"

    # ⚠ 转换与有限性校验必须在同一个 try 内 —— 分开放会让 NaN/Inf
    # 在 int() 处抛出去，而调用方的 except 只会记 warning，
    # 结果是整章伏笔状态更新被静默跳过（数据丢失，不是崩溃）。
    try:
        numeric = float(raw)
        if not math.isfinite(numeric):
            raise ValueError(f"non-finite: {_brief(raw)}")
        truncated_to = int(numeric) if numeric != int(numeric) else None
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_RESOLVE_OFFSET, SOURCE_FALLBACK, (
            f"模型给出的回收偏移不可用（{_brief(raw)}），按兜底 5 章"
        )

    # ⚠ `bool` 是 `int` 的子类，`float(True)` == 1.0。模型输出 JSON 里出现
    # `true` 是可能的，所以这里不是脏值 —— 但 note 必须自述**类型**，
    # 否则 provenance 会误导读者以为模型给的是数字 1。
    is_bool = isinstance(raw, bool)

    if truncated_to is not None:
        # 先 int 截断，再钳位 —— 两者都可能改变取值。
        bounded = max(RESOLVE_OFFSET_MIN, min(RESOLVE_OFFSET_MAX, truncated_to))
        if bounded != truncated_to:
            return bounded, SOURCE_AI_ADJUSTED, (
                f"模型给出的回收偏移 {_brief(raw)} 不是整数，先截断为 "
                f"{truncated_to}、再钳到 {bounded}（采用值非模型原值）"
            )
        return bounded, SOURCE_AI_ADJUSTED, (
            f"模型给出的回收偏移 {_brief(raw)} 不是整数，已截断为 "
            f"{bounded}（采用值非模型原值）"
        )

    value = int(numeric)
    bounded = max(RESOLVE_OFFSET_MIN, min(RESOLVE_OFFSET_MAX, value))
    if bounded != value:
        # ⚠ 第九轮复审：必须先点明**采用值**再给区间，否则末位数字是 30，
        # 而实际采用的是 2。下游按末位读会读成 30。
        # ⚠ 同时用 `_brief` 而非 `{value}`：`float("1e308")` 有限、`int()` 得
        # 308 位整数，写进 note 会让 provenance 文本膨胀到几百字符。
        what = f"（布尔 {raw!r}，按数值）" if is_bool else ""
        return bounded, SOURCE_AI_ADJUSTED, (
            f"模型给出的回收偏移 {_brief(value)}{what} 被钳到 {bounded}"
            f"（区间 [{RESOLVE_OFFSET_MIN}, {RESOLVE_OFFSET_MAX}]，采用值非模型原值）"
        )
    # ⚠ 没有「bool 落在合法区间内」的分支：`float(True)` == 1.0 恒被钳到
    # RESOLVE_OFFSET_MIN（2），所以 bool 永远走上面的钳位分支。
    # （第十轮复审指出曾有一个这样的分支 —— 不可达的死代码，
    #  留着会让人以为「True 可能原样通过」。）
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

    def _fallback(why: str) -> Tuple[int, str, str]:
        """兜底路径。

        ⚠ 第十轮复审：上一版写 ``min(current + step, MAX_STORYLINE_END)``，
        在 ``current >= MAX`` 时返回 ``end <= current`` —— 直接违反本模块
        声明的「收尾章 > 当前章」不变量，还产出「按兜底 +-5000 章」这种
        自相矛盾的 note（负的跨度）。

        **不变量优先于上限**：MAX 是防「模型给出荒谬大数」的护栏，
        而 current 是代码自己维护的写作进度，不该被护栏按下去。
        现实章数远小于 MAX（长篇也就几千章），该分支实际上不可达，
        但一旦可达就必须自洽 —— 不变量是**任何分支**都必须守的。
        """
        step = max(int(span), 1)
        return current + step, SOURCE_FALLBACK, f"{why}（按兜底 +{step} 章）"

    if ai_end is None or (isinstance(ai_end, str) and not ai_end.strip()):
        return _fallback("模型未给出预估收尾章")

    try:
        numeric = float(ai_end)
        if not math.isfinite(numeric):
            raise ValueError(f"non-finite: {_brief(ai_end)}")
        truncated_to = int(numeric) if numeric != int(numeric) else None
    except (TypeError, ValueError, OverflowError):
        return _fallback(f"模型给出的预估收尾章不可用（{_brief(ai_end)}）")

    if truncated_to is not None:
        # ⚠ float 被代码截断 ⇒ 最终取值非模型原值 ⇒ 标 adjusted，不标 ai。
        value = truncated_to
        source, why = SOURCE_AI_ADJUSTED, (
            f"模型给出的预估收尾章 {_brief(ai_end)} 不是整数，已截断为 {truncated_to}"
            f"（采用值非模型原值）"
        )
    else:
        value = int(numeric)
        source, why = SOURCE_AI, f"模型给出的预估收尾章 {value}"

    # ⚠ 第十轮复审：bool 是 int 子类，`float(True)` == 1.0。
    # note 必须自述类型，否则读者以为模型给的是数字 1（与 offset 侧对齐）。
    # ⚠ bool 值必然 ≤ current（True→1、False→0），所以它**总是**走抬升分支 ——
    # 只在 else 分支里写根本到不了，这里单独算一个标记供抬升分支用。
    is_bool = isinstance(ai_end, bool)
    if is_bool:
        why = f"模型给出的是布尔 {ai_end!r}（按数值 {value} 采纳），不是收尾章"

    if value > MAX_STORYLINE_END:
        # 超出任何现实小说的跨度 ⇒ 视同不可用，而不是原样落库。
        # ⚠ 第十轮复审：`1e308` 的 int() 结果是 309 位，必须走 `_brief`。
        return _fallback(
            f"模型给出的预估收尾章 {_brief(value)} 超出上限 {MAX_STORYLINE_END}"
        )

    if value <= current:
        # 不变量：收尾章必须晚于当前章。
        # ⚠ 抬升后的值**不是**模型给的值 ⇒ 标 SOURCE_AI_ADJUSTED，不标 SOURCE_AI。
        # source 已落库（storylines.estimated_chapter_end_source），标错就是在
        # 产物里伪造 provenance。
        # ⚠ 不加 min(…, MAX)：那会让 current ≥ MAX 时返回 end <= current
        # （「抬到第 10000 章」而 current 是 15000 —— 实际是降），
        # 同样违反不变量。不变量优先于上限，与 `_fallback` 保持一致。
        return current + 1, SOURCE_AI_ADJUSTED, (
            f"{why}；该值不晚于当前章 {current}，"
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
