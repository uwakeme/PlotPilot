"""每章目标字数：API、持久化、体量推导与全托管链路共用夹逼，避免各层上限不一致。

I-05（补）
----------
此前「每章字数」的兜底散落在 **19 处**（``or 2500``），而
:func:`clamp_chapter_target_words` 只在 4 处被调用 ⇒
**那 19 处兜底全部绕过了 500..20000 的钳制**
（库里存了 99999 也照用）⇒ 这比「默认值不统一」更严重。

⇒ 现在有**两个口径**（⚠ 语义不同，**不是**区间不同 ⇒ 两个函数并存是对的）：
- :func:`resolve_chapter_words` —— **clamp 口径**（取值 → 夹逼 → 兜底 2500）：
  「这本书每章要写多少字」这种**真要拿来写**的场合用它；
- :func:`resolve_chapter_words_for_display` —— **display 口径**（取值 → 夹逼 →
  **没设就写 0**）：**回显 / 回填**场合用它 ——
  没设就该显示「未设置」，写 2500 会**像作者设的**（那是不诚实的回显）。
⇒ ⚠ 两者只差「没设时写 0 还是 2500」；**别**在 display 处用 clamp 口径。

⚠ 复审 B4：这条「唯一入口」此前**并不唯一** ——
``engine/runtime/writing_delegate.py`` 的**默认生产路径**
（``engine_daemon`` 默认 ``get_story_pipeline_mode() ∈ {writing, full}``）
仍在用 ``or runner.DEFAULT_TARGET_WORDS`` ⇒ 也绕过钳制 ⇒ 已一并改走本入口。

⚠ 与「体量档默认 2000」**不是同一件事**：
体量档的 2000 是**档位元数据**（``V1_LENGTH_TIERS[*]["default_chapter_words"]``），
是「用户选了体量档 ⇒ 按这个档的字数推章数」；
而 2500 是「**什么都没设**时的通用兜底」。
两者服务**不同场景** ⇒ 不合并，各自保留。
"""
from __future__ import annotations

from typing import Any, Optional

CHAPTER_TARGET_WORDS_MIN = 500
CHAPTER_TARGET_WORDS_MAX = 20_000

#: 通用兜底（用户没设每章字数时用）。
#:
#: ⚠ 取值 **2500** = 改造前那 19 处 ``or 2500`` 的值 ⇒ **逐位一致**。
DEFAULT_CHAPTER_WORDS = 2500


def clamp_chapter_target_words(w: int) -> int:
    return max(CHAPTER_TARGET_WORDS_MIN, min(CHAPTER_TARGET_WORDS_MAX, int(w)))


def resolve_chapter_words_for_display(value: Any) -> int:
    """变量回填用的「每章目标字数」—— **没设就写 0**（不是兜底值）。

    ⚠ 复审 O2：这两处此前写的是 ``or 0``（没设 ⇒ 0）⇒ 提示词里
    「每章字数」= 0，读者知道**作者没设**。
    ⇒ 改成走唯一入口后，没设变成了 2500 ⇒ 提示词里**像作者设的 2500**
    ⇒ 那是**不诚实的回显**。
    ⇒ 所以：**没设 ⇒ 写 0**；设了 ⇒ 写**夹逼后**的值（越界值不再进提示词）。

    与 :func:`resolve_chapter_words` 的区别只有一个：**没设时写 0 还是 2500**。
    """
    if value is None:
        return 0
    try:
        if isinstance(value, bool):
            raise TypeError
        number = int(float(str(value).strip()))
    except (TypeError, ValueError, AttributeError, OverflowError):
        return 0
    if number <= 0:
        return 0
    return clamp_chapter_target_words(number)


def resolve_chapter_words(
    value: Any,
    *,
    default: int = DEFAULT_CHAPTER_WORDS,
) -> int:
    """解析「每章目标字数」——**唯一入口**。

    规则：
    1. 读得到合法正数 ⇒ **先夹逼**（500..20000）⇒ 返回；
    2. 读不到 / 非法 / 非正数 ⇒ 用 ``default``（本身也要夹逼）。

    ⚠ 「先夹逼再兜底」的顺序很重要：
    先兜底的话，``99999`` 这种越界值会绕过上限
    （此前那 19 处正是这个毛病）。

    ⚠ 复审 S2：解析走 ``float`` 再取整 ——
    此前用 ``int(str(value))`` ⇒ ``3000.0`` 会因 ``int("3000.0")`` 抛异常
    而落到兜底 2500 ⇒ **比改造前更差**（改造前 ``or 2500`` 会保留 3000.0）。

    Args:
        value: 原始值（可能来自 DB / dict / 属性 / 字符串）。
        default: 读不到时的兜底（默认 2500）。
    """
    try:
        if value is None or isinstance(value, bool):
            raise TypeError
        number = int(float(str(value).strip()))
    except (TypeError, ValueError, AttributeError, OverflowError):
        number = 0
    if number <= 0:
        number = int(default or DEFAULT_CHAPTER_WORDS)
    return clamp_chapter_target_words(number)
