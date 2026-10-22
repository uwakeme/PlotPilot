"""A-02：阶段数与断点交回 AI，代码只留三条不变量。

改造前
------
``_normalize_stage_plan`` 有三处替 AI 做创作判断：

1. ``len(raw_items) != 5`` 直接 raise —— 阶段数写死；
2. ``_chapter_ranges`` 用硬编码 ``ratios = [0.15, 0.40, 0.70, 0.90, 1.0]`` 切分区间；
3. ``phase`` / ``label`` 按**下标**从 ``_PHASE_SCHEMA`` 强制回贴 ——
   AI 给了别的阶段名也会被改写成固定 5 个名字。

三条不变量（登记表 A-02「代码保留的不变量」列）
----------------------------------------------
I1 阶段数 ∈ [3, 8]
I2 断点单调递增且首尾相接（无空洞无重叠）
I3 首段起点 = 1；末段终点 ≥ 目标章数（计划必须覆盖到目标）

关于 I3 **不是**「末段终点 == 目标章数」：既有行为允许计划覆盖到比目标更大的章数
（见 ``test_normalize_plot_outline_uses_explicit_chapter_ranges``：目标 100 章，
阶段区间显式给到 500）。等值约束会与既有行为冲突且不必要。
"""
import pytest

from application.blueprint.services.setup_plot_outline_continuation import (
    normalize_setup_plot_outline_payload,
)
from application.blueprint.services.stage_plan_invariants import (
    MAX_STAGES,
    MIN_STAGES,
    chapter_ranges,
    validate_stage_plan_shape,
)


def _outline(stages):
    return {
        "main_story_overview": "概述",
        "expected_ending": "结局",
        "core_conflict": "冲突",
        "stage_plan": stages,
    }


def _stage(start, end, *, phase="", label="", summary="一段"):
    row = {"chapter_start": start, "chapter_end": end, "summary": summary}
    if phase:
        row["phase"] = phase
    if label:
        row["label"] = label
    return row


# ── I1 阶段数 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("count", [3, 4, 5, 6, 7, 8])
def test_any_count_in_range_is_accepted(count):
    """改造前只有 5 能过；现在 3-8 都接受。"""
    ranges = chapter_ranges(100, count)
    stages = [_stage(a, b) for a, b in ranges]
    out = normalize_setup_plot_outline_payload(_outline(stages), target_chapters=100)
    assert len(out["stage_plan"]) == count


@pytest.mark.parametrize("count", [1, 2, 9, 12])
def test_count_out_of_range_is_rejected(count):
    """越界的阶段数仍然必须被拦下 —— 这是不变量，不是创作自由度。"""
    ranges = chapter_ranges(200, min(count, 20))
    stages = [_stage(a, b) for a, b in ranges[:count]] if count <= 20 else []
    if not stages:
        pytest.skip("章数不足以构造该段数")
    with pytest.raises(ValueError):
        normalize_setup_plot_outline_payload(_outline(stages), target_chapters=200)


def test_range_constants():
    assert (MIN_STAGES, MAX_STAGES) == (3, 8)


# ── I2 断点形状 ─────────────────────────────────────────────────────────

def test_gap_between_stages_is_rejected():
    """空洞：第 2 段起点接不上第 1 段终点。"""
    stages = [_stage(1, 20), _stage(30, 60), _stage(61, 100)]
    with pytest.raises(ValueError, match="不相接"):
        normalize_setup_plot_outline_payload(_outline(stages), target_chapters=100)


def test_overlap_between_stages_is_rejected():
    """重叠：第 2 段起点早于第 1 段终点。"""
    stages = [_stage(1, 30), _stage(25, 60), _stage(61, 100)]
    with pytest.raises(ValueError):
        normalize_setup_plot_outline_payload(_outline(stages), target_chapters=100)


def test_first_stage_must_start_at_chapter_one():
    stages = [_stage(2, 30), _stage(31, 60), _stage(61, 100)]
    with pytest.raises(ValueError, match="首段起点"):
        normalize_setup_plot_outline_payload(_outline(stages), target_chapters=100)


# ── I3 覆盖范围 ─────────────────────────────────────────────────────────

def test_plan_shorter_than_target_is_rejected():
    """计划不许比目标短。"""
    stages = [_stage(1, 20), _stage(21, 40), _stage(41, 50)]
    with pytest.raises(ValueError, match="小于目标章数"):
        normalize_setup_plot_outline_payload(_outline(stages), target_chapters=100)


def test_explicit_ranges_within_the_book_are_preserved():
    """**显式**给的区间（落在书内）不被代码改写 —— 断点由 AI/作者决定。

    ⚠ 第 3 批二次复审 S5/Q4：这条原本是「计划可覆盖到比目标更大的章数
    （目标 100、显式给到 500）」—— 那个宽容是我在 A-02 实现时**自己放宽**的
    ⇒ 于是**显式越界区间**也合法（复审实测 `(41,100)(101,101)(102,102)`
    被接受落库）⇒ 「越界不可能再产生」这句话是**过度声称**。
    ⇒ 现在 I3 收紧为「末段终点 == 目标章数」（登记表 A-02 原文），
    本条改为钉住**真正要保住的性质**：显式区间不被改写。
    """
    stages = [_stage(1, 20), _stage(21, 60), _stage(61, 100)]
    out = normalize_setup_plot_outline_payload(_outline(stages), target_chapters=100)
    assert [(s["chapter_start"], s["chapter_end"]) for s in out["stage_plan"]] == [
        (1, 20),
        (21, 60),
        (61, 100),
    ]


def test_plan_longer_than_target_is_rejected():
    """计划**超出**目标章数 ⇒ 拒绝（I3 收紧后的新行为）。

    这正是复审实测能被静默落库的那一类
    （`(1,20)(21,40)(41,100)(101,101)(102,102)`，目标 100 章）。
    """
    stages = [_stage(1, 20), _stage(21, 120), _stage(121, 500)]
    with pytest.raises(ValueError, match="超过目标章数"):
        normalize_setup_plot_outline_payload(_outline(stages), target_chapters=100)


def test_plan_with_a_stage_outside_the_book_is_rejected():
    """有一段完全落在书外 ⇒ 拒绝（复审实测能被接受的那个形状）。"""
    stages = [_stage(1, 20), _stage(21, 40), _stage(41, 100), _stage(101, 101), _stage(102, 102)]
    with pytest.raises(ValueError, match="超过目标章数"):
        normalize_setup_plot_outline_payload(_outline(stages), target_chapters=100)


# ── AI 的 phase / label 不再被按下标改写 ─────────────────────────────────

def test_custom_phase_name_is_preserved():
    """**A-02 核心**：AI 给了自己的阶段名，不得被回贴成固定的 5 个名字。

    取样点必须**避开** ``_PHASE_ALIASES`` —— 别名归一化是正常卫生工作，
    不是「按下标强制回贴」。（`setup`/`resolution` 恰好是别名，会被归一到
    opening/ending，那是另一回事，由 test_phase_alias_still_canonicalises 覆盖。）
    """
    stages = [
        _stage(1, 33, phase="prologue", label="铺陈期"),
        _stage(34, 66, phase="confrontation", label="转折期"),
        _stage(67, 100, phase="aftermath", label="收束期"),
    ]
    out = normalize_setup_plot_outline_payload(_outline(stages), target_chapters=100)
    got = [s["phase"] for s in out["stage_plan"]]
    assert got == ["prologue", "confrontation", "aftermath"], got
    assert [s["label"] for s in out["stage_plan"]] == ["铺陈期", "转折期", "收束期"]


def test_phase_alias_still_canonicalises():
    """已知别名仍归一化，避免下游拿到同义异名。"""
    stages = [
        _stage(1, 33, phase="open"),
        _stage(34, 66, phase="development"),
        _stage(67, 100, phase="ending"),
    ]
    out = normalize_setup_plot_outline_payload(_outline(stages), target_chapters=100)
    assert [s["phase"] for s in out["stage_plan"]] == [
        "opening", "development", "ending"]


def test_missing_phase_gets_positional_fallback_not_fixed_five():
    """AI 没给阶段名时按位置兜底，而不是套用那 5 个固定名字。"""
    stages = [_stage(1, 33), _stage(34, 66), _stage(67, 100)]
    out = normalize_setup_plot_outline_payload(_outline(stages), target_chapters=100)
    phases = [s["phase"] for s in out["stage_plan"]]
    assert phases == ["stage_1", "stage_2", "stage_3"], phases


# ── AI 给的区间优先于兜底 ───────────────────────────────────────────────

def test_ai_supplied_ranges_win_over_fallback():
    ranges = chapter_ranges(100, 3)
    stages = [_stage(1, 10), _stage(11, 20), _stage(21, 100)]
    out = normalize_setup_plot_outline_payload(_outline(stages), target_chapters=100)
    assert [(s["chapter_start"], s["chapter_end"]) for s in out["stage_plan"]] == [
        (1, 10), (11, 20), (21, 100)]
    assert ranges != [(1, 10), (11, 20), (21, 100)], "兜底值恰好撞上了，换一组再验"


def test_missing_ranges_fall_back_to_even_split():
    stages = [{"summary": "一段"}, {"summary": "二段"}, {"summary": "三段"}]
    out = normalize_setup_plot_outline_payload(_outline(stages), target_chapters=90)
    got = [(s["chapter_start"], s["chapter_end"]) for s in out["stage_plan"]]
    assert got == chapter_ranges(90, 3)
    assert got[-1][1] == 90


# ── 兜底函数的向后兼容 ─────────────────────────────────────────────────

def test_five_stage_fallback_keeps_original_ratios():
    """5 段兜底必须与改造前**逐值一致** —— 否则既有行为被悄悄改了。"""
    assert chapter_ranges(80, 5) == [
        (1, 12), (13, 32), (33, 56), (57, 72), (73, 80)]


def test_ranges_always_cover_the_whole_book_exactly_once():
    for n in range(MIN_STAGES, MAX_STAGES + 1):
        for total in (n, n + 1, 20, 80, 500):
            r = chapter_ranges(total, n)
            assert r[0][0] == 1, (n, total, r)
            assert r[-1][1] == total, (n, total, r)
            for i in range(len(r) - 1):
                assert r[i][1] + 1 == r[i + 1][0], (n, total, r)
            for a, b in r:
                assert a <= b, (n, total, r)


def test_stage_count_larger_than_chapters_degrades_gracefully():
    """章数少于段数时不崩，退化成每段 1 章。"""
    r = chapter_ranges(3, 5)
    assert len(r) == 3
    assert r[0][0] == 1 and r[-1][1] == 3


# ── validate_stage_plan_shape 单测 ──────────────────────────────────────

def test_shape_validator_reports_gap():
    plan = [{"chapter_start": 1, "chapter_end": 10},
            {"chapter_start": 20, "chapter_end": 30},
            {"chapter_start": 31, "chapter_end": 40}]
    assert any("不相接" in e for e in validate_stage_plan_shape(plan))


def test_shape_validator_reports_short_plan():
    plan = chapter_ranges(100, 3)
    rows = [{"chapter_start": a, "chapter_end": b} for a, b in plan]
    rows[-1]["chapter_end"] = 50
    assert any("小于目标章数" in e
               for e in validate_stage_plan_shape(rows, target_chapters=100))


def test_shape_validator_passes_a_good_plan():
    rows = [{"chapter_start": a, "chapter_end": b} for a, b in chapter_ranges(100, 4)]
    assert validate_stage_plan_shape(rows, target_chapters=100) == []


def test_shape_validator_rejects_empty():
    assert validate_stage_plan_shape([]) == ["stage_plan 为空"]
