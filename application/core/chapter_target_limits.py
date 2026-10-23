"""每章目标字数：API、持久化、体量推导与全托管链路共用夹逼，避免各层上限不一致。

I-05（补）
----------
此前「每章字数」的兜底散落在 **17 处**（``or 2500``），而
:func:`clamp_chapter_target_words` 只在 4 处被调用 ⇒
**那 17 处兜底全部绕过了 500..20000 的钳制**
（库里存了 99999 也照用）⇒ 这比「默认值不统一」更严重。

⇒ 现在：
- :data:`DEFAULT_CHAPTER_WORDS` = 2500（**与改造前逐位一致**）；
- :func:`resolve_chapter_words` 是**唯一**的解析入口
  （取值 → 夹逼 → 兜底），所有兜底都走它。

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
#: ⚠ 取值 **2500** = 改造前那 17 处 ``or 2500`` 的值 ⇒ **逐位一致**。
DEFAULT_CHAPTER_WORDS = 2500


def clamp_chapter_target_words(w: int) -> int:
    return max(CHAPTER_TARGET_WORDS_MIN, min(CHAPTER_TARGET_WORDS_MAX, int(w)))


def resolve_chapter_words(
    value: Any,
    *,
    default: int = DEFAULT_CHAPTER_WORDS,
) -> int:
    """解析「每章目标字数」——**唯一入口**。

    规则：
    1. 读得到合法正整数 ⇒ **先夹逼**（500..20000）⇒ 返回；
    2. 读不到 / 非法 / 非正数 ⇒ 用 ``default``（本身也要夹逼）。

    ⚠ 「先夹逼再兜底」的顺序很重要：
    先兜底的话，``99999`` 这种越界值会绕过上限
    （此前那 17 处正是这个毛病）。

    Args:
        value: 原始值（可能来自 DB / dict / 属性 / 字符串）。
        default: 读不到时的兜底（默认 2500）。
    """
    try:
        if value is None:
            raise TypeError
        number = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        number = 0
    if number <= 0:
        number = int(default or DEFAULT_CHAPTER_WORDS)
    return clamp_chapter_target_words(number)
