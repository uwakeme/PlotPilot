"""ContextBudgetAllocator：紧邻上一章章末承接摘录。"""

from application.engine.services.context_budget_allocator import ContextBudgetAllocator


def test_excerpt_empty():
    alloc = ContextBudgetAllocator()
    assert alloc._excerpt_immediate_previous_chapter("") == ""
    assert alloc._excerpt_immediate_previous_chapter("   ") == ""


def test_excerpt_short_gives_the_whole_chapter():
    """⚠ 第 47 批：短章（≤ ``PREV_CHAPTER_FULL_TEXT_MAX_CHARS``）⇒ **给全文**。

    ⚠ 意图不变：内容**整段保留**、**不**给章首略览；
    只是标签从「章末节选」改成「上一章全文」（此前名不副实 ⇒ 读者会以为只给了尾）。
    """
    alloc = ContextBudgetAllocator()
    text = "x" * 800
    out = alloc._excerpt_immediate_previous_chapter(text)
    assert "上一章全文，供本章开头承接" in out
    assert "章首略览" not in out
    assert text in out


def test_excerpt_long_head_and_tail():
    alloc = ContextBudgetAllocator()
    # 必须长于全文阈值（PREV_CHAPTER_FULL_TEXT_MAX_CHARS = 3000）才会同时输出
    # 章首略览与章末长节选（第 47 批：此前是 head_n + tail_n = 2300）
    # ⚠ 尾段长度必须 ≤ tail_n（2000），否则「结尾是 Z 段」这条断言就不成立
    text = "A" * 400 + "M" * 1600 + "Z" * 1500
    out = alloc._excerpt_immediate_previous_chapter(text)
    assert "章首略览" in out
    assert "章末节选，供本章开头承接" in out
    assert out.endswith("Z" * 1500)
    assert "A" * 250 in out
