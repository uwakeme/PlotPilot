"""第 47 批：N-1 章「**短章给全文**」，且这个阈值**独立于**章末保留量。

背景
----
「近章原文」这块（N-1：上一章）此前**已经**会在章长不大时给全文 ——
但那个判定是**隐式**耦合在 ``head_chars + tail_chars`` 上的：

    if len(raw) <= head_chars + tail_chars:  → 给全文

⇒ ⇒ ``tail_chars`` 的含义是「章末保留多少字」，**与「要不要给全文」无关**；
把两者绑在一起 ⇒ 谁一调 ``tail_chars``，就悄悄把「给全文」变成「给首尾」
（或反过来）⇒ ⇒ **两个含义互相牵制**。

⇒ ⇒ 现在：全文阈值**独立**（``full_text_max_chars``，默认 3000 字，
覆盖生产书 1540~2476 的章长）⇒ 「给全文」与「章末保留多少」**互不影响**。

⚠ 守卫纪律：期望值**写死字面量**；不依赖 DB（纯文本函数）。
"""
from __future__ import annotations

from application.engine.services.context_budget_allocator import ContextBudgetAllocator
from application.engine.services.recent_chapter_context import (
    DEFAULT_PREV_FULL_TEXT_MAX_CHARS,
    excerpt_immediate_previous_chapter,
)

#: 口径值（写死 ⇒ 改被测常量必红）
FULL_TEXT_MAX_CHARS = 3000
HEAD_CHARS = 300
TAIL_CHARS = 2000

#: 中段独有标记（用来判断「中段有没有被送出去」）
MIDDLE_MARK = "【中段独有的一句】"


def _chapter(length: int) -> str:
    """造一章：头 + 填充 + **中段标记** + 填充 + 尾（长度可控）。

    ⚠ 中段标记必须**距首尾都远于** ``head_chars`` / ``tail_chars``
    ⇒ 否则它会落进 head 或 tail 里 ⇒ ⇒「中段有没有被送出去」这个判据就失真了。
    ⇒ ⇒ 两侧**对称**填充。
    """
    head = "头" * 300
    tail = "尾" * 300
    middle = MIDDLE_MARK + ("中" * 300)
    filler = max(length - len(head) - len(middle) - len(tail), 0)
    left = filler // 2
    right = filler - left
    return head + ("填" * left) + middle + ("填" * right) + tail


def test_the_full_text_cap_is_pinned():
    """⚠ 口径写死 ⇒ 改被测常量必红。"""
    assert DEFAULT_PREV_FULL_TEXT_MAX_CHARS == FULL_TEXT_MAX_CHARS
    assert ContextBudgetAllocator.PREV_CHAPTER_FULL_TEXT_MAX_CHARS == FULL_TEXT_MAX_CHARS


def test_a_short_previous_chapter_is_delivered_whole():
    """⚠ 短章 ⇒ **给全文**（模型要看得见上一章的**中段**，不只首尾）。"""
    content = _chapter(2000)

    excerpt = excerpt_immediate_previous_chapter(
        content, head_chars=HEAD_CHARS, tail_chars=TAIL_CHARS
    )

    assert MIDDLE_MARK in excerpt, "短章的中段没送出去 ⇒ 只给了首尾"
    assert "上一章全文" in excerpt, excerpt[:80]


def test_a_long_previous_chapter_gets_head_and_tail():
    """⚠ 长章 ⇒ 章首略览 + 章末节选，且**明确标注**中间被省略。"""
    content = _chapter(6000)

    excerpt = excerpt_immediate_previous_chapter(
        content, head_chars=HEAD_CHARS, tail_chars=TAIL_CHARS
    )

    assert "章首略览" in excerpt, excerpt[:80]
    assert "章末节选" in excerpt, excerpt[:80]
    assert "……" in excerpt, "长章被截了却没标注省略 ⇒ 模型以为看到的是全部"
    assert MIDDLE_MARK not in excerpt, "长章的中段不该送出去"


def test_the_full_text_threshold_does_not_depend_on_tail_chars():
    """⚠ **本批的核心**：调「章末保留多少」**不许**改变「要不要给全文」。

    ⚠ 此前判定是 ``len(raw) <= head_chars + tail_chars`` ⇒ 隐式耦合
    ⇒ ⇒ 改 tail_chars 就会悄悄改判定 ⇒ ⇒ 两个含义互相牵制。
    """
    content = _chapter(2000)

    with_big_tail = excerpt_immediate_previous_chapter(
        content, head_chars=HEAD_CHARS, tail_chars=TAIL_CHARS
    )
    with_small_tail = excerpt_immediate_previous_chapter(
        content, head_chars=HEAD_CHARS, tail_chars=50
    )

    assert ("上一章全文" in with_big_tail) == ("上一章全文" in with_small_tail), (
        "改 tail_chars 改变了「给不给全文」的判定 ⇒ 阈值仍然与它耦合"
    )
    assert "上一章全文" in with_big_tail, with_big_tail[:80]


def test_the_allocator_passes_the_independent_threshold():
    """⚠ 分配器必须把**独立**的阈值传下去（否则又退回隐式耦合）。

    ⚠ 判据要**两个**都查：内容（中段在不在）**且**形态（是不是「全文」那种）
    ⇒ 否则「退回到 head+tail 隐式判定」这个变异**抓不到**
    （短章仍会给全文，只是标签变了）。
    """
    content = _chapter(2000)
    allocator = ContextBudgetAllocator()

    excerpt = allocator._excerpt_immediate_previous_chapter(content)

    assert MIDDLE_MARK in excerpt, "分配器这一层没给全文 ⇒ 阈值没传对"
    assert "上一章全文" in excerpt, (
        f"分配器这一层走的是 head+tail 隐式判定 ⇒ 阈值仍与 tail_chars 耦合：{excerpt[:60]}"
    )
