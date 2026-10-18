"""G-01 / G-03：伏笔回收节奏与故事线收尾章 —— AI 优先，兜底显式标注。

两条同源：**AI 通道完好，代码用一个常数顶替，且顶替后无从分辨。**

- G-01：prompt 本就要求 ``suggested_resolve_offset``，代码也读了；
  模型省略时静默回落成固定的 5，与「模型说 5」在数据上完全一样。
- G-03：``estimated_chapter_end = chapter_number + 12`` / ``+ 20``，
  两个位置各写一个常数，同样分不清是模型给的还是代码补的。

核心断言
--------
**「模型没给」与「模型给的就是这个值」必须可区分。**
只断言「取值等于 5」是测不出来的 —— 两种情况取值相同，
区别只在 ``source`` 字段上。
"""
import pytest

from application.world.services.foreshadow_pacing import (
    DEFAULT_RESOLVE_OFFSET,
    RESOLVE_OFFSET_MAX,
    RESOLVE_OFFSET_MIN,
    SOURCE_AI,
    SOURCE_AI_ADJUSTED,
    SOURCE_FALLBACK,
    check_resolve_after_plant,
    resolve_offset,
    resolve_storyline_end,
)


# ── G-01 回收偏移 ──────────────────────────────────────────────────────

def test_ai_offset_is_used_and_marked_as_ai():
    offset, source, note = resolve_offset(12)
    assert offset == 12
    assert source == SOURCE_AI
    assert "模型给出" in note


def test_missing_offset_falls_back_but_is_marked():
    """**核心**：取值仍是 5，但来源必须是 ``fallback`` —— 可与「模型说 5」区分。"""
    offset, source, note = resolve_offset(None)
    assert offset == DEFAULT_RESOLVE_OFFSET == 5
    assert source == SOURCE_FALLBACK, "模型没给却被标成 ai"
    assert "未给出" in note


def test_blank_and_zero_like_values_are_fallbacks():
    for raw in ("", "   ", None):
        _, source, _ = resolve_offset(raw)
        assert source == SOURCE_FALLBACK, raw


def test_junk_offset_is_fallback_not_zero():
    """脏值不得变成 0 章后回收（那等于当章种当章收）。"""
    offset, source, _ = resolve_offset("十几章以后")
    assert offset == DEFAULT_RESOLVE_OFFSET
    assert source == SOURCE_FALLBACK


def test_offset_clamped_to_existing_band():
    """沿用改造前的 2-30 章限制，但钳制仍算「模型给了」。"""
    low, source_low, _ = resolve_offset(0)
    assert low == RESOLVE_OFFSET_MIN
    assert source_low == SOURCE_AI

    high, source_high, _ = resolve_offset(999)
    assert high == RESOLVE_OFFSET_MAX
    assert source_high == SOURCE_AI


def test_string_numbers_are_accepted():
    offset, source, _ = resolve_offset("15")
    assert offset == 15
    assert source == SOURCE_AI


# ── G-01 不变量：回收章 > 埋设章 ───────────────────────────────────────

def test_invariant_rejects_same_chapter():
    assert check_resolve_after_plant(10, 10) is not None
    assert check_resolve_after_plant(10, 9) is not None


def test_invariant_accepts_later_chapter():
    assert check_resolve_after_plant(10, 11) is None


def test_ai_offset_always_satisfies_invariant():
    """任何 AI 给的偏移经钳制后，回收章必然晚于埋设章（偏移 ≥ 2）。"""
    for raw in (0, 1, 2, 15, 999, "7"):
        offset, _, _ = resolve_offset(raw)
        assert check_resolve_after_plant(10, 10 + offset) is None, raw


# ── G-03 故事线收尾章 ─────────────────────────────────────────────────

def test_ai_end_chapter_is_used():
    end, source, _ = resolve_storyline_end(10, 40)
    assert end == 40
    assert source == SOURCE_AI


def test_missing_end_falls_back_but_is_marked():
    end, source, note = resolve_storyline_end(10)
    assert end == 10 + 12
    assert source == SOURCE_FALLBACK
    assert "未给出" in note


def test_end_not_after_current_is_lifted():
    """不变量：收尾章必须晚于当前章。模型给了 5 而当前 10 ⇒ 抬到 11。

    ⚠ 抬升后的值**不是**模型给的值 ⇒ source 是 `ai_adjusted` 而非 `ai`。
    第三轮复审指出：上一轮把 source 落库后，这里标 `ai` 会在产物里
    声称「第 11 章是模型判断的」，而模型给的是 5 —— 落库后的假来源。
    """
    end, source, note = resolve_storyline_end(10, 5)
    assert end == 11
    assert source == SOURCE_AI_ADJUSTED
    assert source != SOURCE_AI
    assert "不晚于" in note


def test_end_equal_to_current_is_lifted():
    end, _, _ = resolve_storyline_end(10, 10)
    assert end == 11


def test_junk_end_falls_back():
    end, source, _ = resolve_storyline_end(10, "很后面")
    assert end == 22
    assert source == SOURCE_FALLBACK


# ── 接线：生产代码真的走了这条路径 ─────────────────────────────────────

def test_chapter_narrative_sync_uses_the_shared_pacing_module():
    """防止有人日后又写回 ``chapter_number + 12`` 这种裸常数。"""
    import ast
    from pathlib import Path

    src = Path(
        r"D:\WorkSpace\projects\PlotPilot"
        r"\application\world\services\chapter_narrative_sync.py"
    ).read_text(encoding="utf-8")
    assert "estimated_chapter_end=chapter_number + 12" not in src, (
        "G-03 第一处硬估回来了"
    )
    assert "estimated_chapter_end=chapter_number + 20" not in src, (
        "G-03 第二处硬估回来了"
    )
    # G-01 的裸 `h.get("suggested_resolve_offset", 5)` 也不该回来
    assert 'h.get("suggested_resolve_offset", 5)' not in src, (
        "G-01 的默认值 5 又被内联回来了 —— 那样会与「模型说 5」无法区分"
    )
    ast.parse(src)


def test_both_sources_are_distinguishable_for_the_same_value():
    """同一个取值 5，AI 给的与兜底的必须能分辨 —— 这是本条的全部意义。"""
    ai_offset, ai_source, _ = resolve_offset(5)
    fb_offset, fb_source, _ = resolve_offset(None)
    assert ai_offset == fb_offset == 5
    assert ai_source != fb_source, "两种来源被压成了同一个标记"
