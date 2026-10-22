"""A-03：主线候选的**数量**与**原型类型**交回 AI。

背景
----
登记表 A-03 写：「主线候选三模式固定映射」——三条候选按下标
idx0/idx1/idx2 固定映射 survival/conspiracy/anomaly 三种架构；
prompt 要求「必须恰好 3 个元素」，少于 3 条抛合同错误。

核实（2026-10-22）补充了两处登记表没写的事实
----------------------------------------------------
1. ``mode`` 映射**只在 AI 没填 ``main_axis`` 时**才生效
   ⇒ 它只影响**代码兜底文案**，不影响 AI 自己写的 ``main_axis``。
   危害比登记表暗示的小，但「按 idx 硬映射」这件事本身仍是代答。
2. 有**两处静默截断**（登记表完全没提）：
   - ``raw_list[:5]``：模型给 6 条以上 ⇒ 多出来的**直接消失**；
   - ``normalized[:3]``：模型给 4~5 条 ⇒ 返回给用户的**只有前 3 条**，
     多的两条无告警、无来源标记地丢失。
   ⇒ 这与 A-01 的「sublines_source 显式化」是同一类问题。

本笔做三件事
------------
1. **数量**由 AI 定（代码只保留下限与上限）；
2. **原型类型**优先用模型自报的 ``type``（模型没给才按位置兜底）；
3. 两处**静默截断**改为**如实记下**（``options_truncated``）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from application.blueprint.services.setup_main_plot_suggestion_service import (
    MAX_MAIN_PLOT_OPTIONS,
    MIN_MAIN_PLOT_OPTIONS,
    MainPlotSuggestionContractError,
    normalize_main_plot_options_data,
)


def _opt(idx: int, **overrides) -> dict:
    base = {
        "id": f"option_{idx}",
        "type": f"自定义原型{idx}",
        "title": f"方案{idx}",
        "logline": "一句话",
        "core_conflict": "谁 vs 谁",
        "starting_hook": "钩子",
        "main_axis": "主轴承诺",
        "opening_pressure": "外部压力",
        "forbidden_drift": "禁止漂移",
    }
    base.update(overrides)
    return base


def _ctx(**extra) -> dict:
    ctx = {"target_chapters": 100}
    ctx.update(extra)
    return ctx


# ── 数量：由 AI 定 ───────────────────────────────────────────────────

def test_three_options_are_accepted():
    out = normalize_main_plot_options_data({"plot_options": [_opt(1), _opt(2), _opt(3)]}, _ctx())
    assert len(out) == 3


def test_four_options_are_all_kept():
    """A-03：模型给 4 条 ⇒ **全部保留**（改造前被静默截断到 3 条）。

    ⚠ 这里断言用**字面量 4**，不引用 ``MAX_MAIN_PLOT_OPTIONS`` ——
    引用常量的话，常量一被改小断言就跟着变 ⇒ 守卫跟着实现跑（假绿）。
    """
    out = normalize_main_plot_options_data(
        {"plot_options": [_opt(1), _opt(2), _opt(3), _opt(4)]}, _ctx()
    )

    assert len(out) == 4, "第 4 条被静默丢弃了"
    assert [item["title"] for item in out] == ["方案1", "方案2", "方案3", "方案4"]


def test_five_options_are_all_kept():
    out = normalize_main_plot_options_data(
        {"plot_options": [_opt(i) for i in range(1, 6)]}, _ctx()
    )
    assert len(out) == 5


def test_six_options_are_all_kept():
    """6 条也要全留（上限是 6，不多不少）。"""
    out = normalize_main_plot_options_data(
        {"plot_options": [_opt(i) for i in range(1, 7)]}, _ctx()
    )
    assert len(out) == 6
    assert "options_truncated" not in out[0]


def test_too_few_options_are_rejected():
    """下限保留：少于 3 条没有「让作者挑」的意义。"""
    with pytest.raises(MainPlotSuggestionContractError, match="数量不足"):
        normalize_main_plot_options_data({"plot_options": [_opt(1), _opt(2)]}, _ctx())


def test_options_above_the_cap_report_the_dropped_count():
    """超过上限 ⇒ 记下丢了多少（**不许静默**）。

    ⚠ 断言用**字面量 8 / 6**，不引用常量 ——
    引用常量的话，常量一被改断言就跟着变 ⇒ 守卫跟着实现跑（假绿）。
    """
    payload = {"plot_options": [_opt(i) for i in range(1, 9)]}
    out = normalize_main_plot_options_data(payload, _ctx())

    assert len(out) == 6
    assert out[0]["options_truncated"] == 2


def test_no_truncation_marker_when_nothing_was_dropped():
    out = normalize_main_plot_options_data(
        {"plot_options": [_opt(1), _opt(2), _opt(3)]}, _ctx()
    )
    assert "options_truncated" not in out[0]


def test_dropped_count_is_never_negative():
    """丢弃数必须是**非负**的。

    ⚠ 这条守的是一个**真实踩过的 bug**：第一版实现里，丢弃数是在
    **截断之后**才算的 ⇒ ``len(normalized) - MAX`` 变成了**负数**
    （3 - 6 = -3）⇒ 落库里记着「丢弃了 -3 条」。
    ⇒ 变异 M2 把上限改成 3 时才暴露出来。
    """
    payload = {"plot_options": [_opt(i) for i in range(1, 9)]}
    out = normalize_main_plot_options_data(payload, _ctx())

    dropped = out[0].get("options_truncated")
    assert dropped is not None
    assert dropped >= 0, f"丢弃数不能是负数（实际 {dropped}）"
    assert dropped == 2


# ── 原型类型：优先用模型自报的 type ─────────────────────────────────

def test_declared_type_is_preserved():
    """模型自报的 ``type`` 原样保留（不再被按下标改写）。"""
    payload = {
        "plot_options": [
            _opt(1, type="关系反噬线"),
            _opt(2, type="身份置换线"),
            _opt(3, type="代价透支线"),
        ]
    }
    out = normalize_main_plot_options_data(payload, _ctx())

    assert [item["type"] for item in out] == ["关系反噬线", "身份置换线", "代价透支线"]


def test_type_is_not_rewritten_to_the_three_fixed_archetypes():
    """三条候选**都**不是那三个固定原型时，也不能被贴上去。"""
    payload = {
        "plot_options": [
            _opt(1, type="关系反噬线"),
            _opt(2, type="身份置换线"),
            _opt(3, type="代价透支线"),
        ]
    }
    out = normalize_main_plot_options_data(payload, _ctx())

    for item in out:
        assert item["type"] not in {"survival", "conspiracy", "anomaly"}


def test_fallback_text_uses_the_declared_type_not_the_positional_one():
    """AI 没填 ``main_axis`` 时，兜底文案用**它自报的** type，不是位置对应的原型。"""
    payload = {
        "plot_options": [
            _opt(1, type="关系反噬线", main_axis=""),
            _opt(2, type="身份置换线", main_axis=""),
            _opt(3, type="代价透支线", main_axis=""),
        ]
    }
    out = normalize_main_plot_options_data(payload, _ctx())

    # 兜底文案里应出现它自己的 type
    assert "关系反噬线" in out[0]["main_axis"]
    assert "生存求证线" not in out[0]["main_axis"]
    assert "黑箱揭露线" not in out[1]["main_axis"]
    assert "规则变数线" not in out[2]["main_axis"]


# ── prompt 侧：不再要求「恰好 3 个 / 顺序对应 A/B/C」 ───────────────

def test_prompt_no_longer_demands_exactly_three_in_fixed_order():
    root = Path(__file__).resolve().parents[4]
    system_md = (
        root / "infrastructure/ai/prompt_packages/nodes/planning-main-plot-option/system.md"
    ).read_text(encoding="utf-8")

    assert "必须恰好包含 3 个元素" not in system_md, "prompt 还在要求恰好 3 条"
    assert "顺序对应 A/B/C" not in system_md, "prompt 还在要求顺序对应固定三类"
    # 但**下限**要在（提示模型给 3~5 个）
    assert "3~5" in system_md or "3 到 5" in system_md


# ── 静默截断已消除 ─────────────────────────────────────────────────

def test_six_options_are_not_silently_dropped_to_five():
    """``raw_list[:5]`` 那处截断必须消失。

    ⚠ 断言用**字面量 8 / 6 / 2**，不引用常量。
    """
    payload = {"plot_options": [_opt(i) for i in range(1, 9)]}
    out = normalize_main_plot_options_data(payload, _ctx())

    assert len(out) == 6
    assert out[0]["options_truncated"] == 2
