"""R0 / R1 的守卫（第 3 批三次复审）。

- **R0**：作者只填了第一段 ⇒ 后端把「作者写的」与「系统算的」**混用**
  ⇒ 首尾不相接 ⇒ 报错信息不透明（作者一头雾水）。
  ⇒ 现在：**要么全部显式、要么全部默认**，不许混用。
- **R1**：阶段数 > 目标章数 ⇒ ``ranges[index]`` IndexError
  ⇒ 不被路由的 ``except ValueError`` 捕获 ⇒ **HTTP 500**。
  ⇒ 现在：**夹紧**到放得下的段数（每阶段至少 1 章），并记日志；
  夹完仍不满足 I1（≥3 段）⇒ 明确的 ValueError（⇒ 400）。
"""
from __future__ import annotations

import pytest

from application.blueprint.services.setup_plot_outline_continuation import (
    normalize_setup_plot_outline_payload,
)


def _outline(stages):
    return {
        "main_story_overview": "概述",
        "expected_ending": "结局",
        "core_conflict": "冲突",
        "stage_plan": stages,
    }


def _stage(start=None, end=None, *, summary="一段"):
    row = {"summary": summary}
    if start is not None:
        row["chapter_start"] = start
    if end is not None:
        row["chapter_end"] = end
    return row


# ───────────────────────── R0 · 不许混用两套区间 ─────────────────────────


def test_mixed_explicit_and_default_ranges_are_rejected():
    """「有的段显式、有的段不填」⇒ 拒绝，且信息说清是哪一段。

    ⚠ 第 3 批三次复审 R0：此前混用会走到 I2，报一句
    「stage_plan[1] 起点 16 与上一段终点 20 不相接」——作者看不懂。
    """
    stages = [
        _stage(1, 20),
        _stage(summary="二段"),
        _stage(summary="三段"),
    ]
    with pytest.raises(ValueError, match="全部填写或全部不填"):
        normalize_setup_plot_outline_payload(_outline(stages), target_chapters=100)


def test_all_explicit_ranges_are_accepted():
    """全部显式 ⇒ 照收（不许因为「不许混用」把正常路径也拦了）。"""
    stages = [_stage(1, 20), _stage(21, 60), _stage(61, 100)]
    out = normalize_setup_plot_outline_payload(_outline(stages), target_chapters=100)
    assert [(s["chapter_start"], s["chapter_end"]) for s in out["stage_plan"]] == [
        (1, 20),
        (21, 60),
        (61, 100),
    ]


def test_no_explicit_ranges_falls_back_to_the_computed_ones():
    """全部不填 ⇒ 系统按目标章数分配（前端默认路径）。"""
    stages = [_stage(summary=f"第{i}段") for i in range(3)]
    out = normalize_setup_plot_outline_payload(_outline(stages), target_chapters=90)
    got = [(s["chapter_start"], s["chapter_end"]) for s in out["stage_plan"]]
    assert got[0][0] == 1
    assert got[-1][1] == 90


# ───────────────────────── R1 · 阶段数超过目标章数不崩 ─────────────────────────


def test_more_stages_than_chapters_is_clamped_not_crashed():
    """阶段数 > 目标章数 ⇒ **夹紧**（此前是 IndexError ⇒ 500）。

    ⚠ 默认路径必经：前端不再带区间 ⇒ 算出来的区间真的被用；
    而 `planning-plot-outline` 的旧形状固定 5 个子阶段，
    `target_chapters` 又只要求 >0 ⇒ 一本 1~4 章的书 + 5 段计划 = 必崩。
    """
    stages = [_stage(summary=f"第{i}段") for i in range(5)]
    out = normalize_setup_plot_outline_payload(_outline(stages), target_chapters=4)

    assert len(out["stage_plan"]) == 4, "阶段数应被夹到目标章数"
    assert out["stage_plan"][-1]["chapter_end"] == 4
    assert out["stage_plan"][0]["chapter_start"] == 1


def test_too_few_chapters_for_three_stages_is_a_clear_error():
    """夹到连 3 段都放不下 ⇒ **明确的 ValueError**（⇒ 400），不是 IndexError（⇒ 500）。"""
    stages = [_stage(summary=f"第{i}段") for i in range(5)]
    with pytest.raises(ValueError, match="阶段数"):
        normalize_setup_plot_outline_payload(_outline(stages), target_chapters=1)


def test_stage_count_beyond_the_limit_is_rejected_before_any_indexing():
    """阶段数超过 8 ⇒ 明确的 ValueError（不许走到取区间那一步才崩）。"""
    stages = [_stage(summary=f"第{i}段") for i in range(9)]
    with pytest.raises(ValueError, match="阶段数"):
        normalize_setup_plot_outline_payload(_outline(stages), target_chapters=100)
