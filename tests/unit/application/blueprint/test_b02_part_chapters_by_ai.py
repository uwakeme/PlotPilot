"""B-02：部级章数分配改由 **AI** 给（代码公式只作兜底）。

改造前
------
「全书怎么分给各部」是**代码代做**的：``_calculate_chapter_distribution``
按固定比例算（三部 25/50/25、四部 20/30/30/20、5+ 部首尾 20% 中间平分），
再把结果**逐部喂给模型**（``- 第N部：分配 X 章``）
⇒ 模型只能在一个既定分配里填内容，**不能自己决定**各部轻重。

而 precise 模式的提示词还明写「Part 和 Volume 只需填写 node_id、title、description」
⇒ 部级章数**只有代码知道**，AI 连说都说不了。

改造后
------
· 提示词只给**事实**：全书共 N 章、分给 P 部、总幕数、平均每幕；
  **每部多少章由 AI 定**（要求里写明「各部之和必须等于总章数」）；
· 骨架给 part 节点留**兜底** ``estimated_chapters``（公式值，AI 没给时用）；
· 合并后**核对**部级之和 ⇒ 不符只**告警**、**不改写**（与幕级 B-03 同纪律：
  纠正等于替 AI 做决定）。

⚠ 与 B-01 的关系
----------------
B-01 把「各部戏剧功能」从代码预设里解放出来，但把「各部章数分配」当成了
**代码该给的事实**。B-02 把这一项也交给 AI ——
**不变量仍是「各部章数之和 = target_chapters」**（登记表 B-02 的约束写的是
「同 B-01」，而 B-01 的约束是「各部章数之和 = target_chapters；部号连续」）。
"""
from __future__ import annotations

import pathlib
import re

import pytest
from unittest.mock import Mock

from application.blueprint.services.act_chapter_budget import (
    check_act_chapter_budget,
    check_level_chapter_budget,
)
from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
PRECISE_PROMPT_REL = (
    "infrastructure/ai/prompt_packages/nodes/planning-precise-macro"
)


def _svc() -> ContinuousPlanningService:
    return ContinuousPlanningService(
        story_node_repo=Mock(), chapter_element_repo=Mock(), llm_service=Mock()
    )


def _prompt(parts: int = 3, target: int = 120):
    svc = _svc()
    skeleton = {
        "parts": [
            {
                "node_id": f"P{i}",
                "volumes": [{"node_id": f"V{i}_1", "acts": [{"node_id": f"A{i}_1_1"}]}],
            }
            for i in range(1, parts + 1)
        ]
    }
    return svc._build_precise_macro_prompt(
        {"characters": [], "timeline_notes": []},
        target_chapters=target,
        structure_preference={
            "parts": parts, "volumes_per_part": 1, "acts_per_volume": 1
        },
        skeleton=skeleton,
    )


# ======================================================================
#  A. 提示词：分配交给 AI，事实仍在
# ======================================================================


def test_the_prompt_no_longer_dictates_the_per_part_allocation():
    """提示词里不再逐部给章数（那是代码代做的分配）。"""
    prompt = _prompt(3)

    assert "第1部：分配" not in prompt.system
    assert "第3部：分配" not in prompt.system
    assert not re.findall(r"第\d+部：分配 \d+ 章", prompt.system)


def test_the_prompt_still_gives_the_facts():
    """**事实**仍在：全书总章数、部数、总幕数、平均每幕。"""
    prompt = _prompt(3)

    assert "全书共 120 章" in prompt.system
    assert "分给 3 部" in prompt.system
    assert "总幕数：3 幕" in prompt.system
    assert "每部多少章由你定" in prompt.system


def test_the_prompt_asks_the_ai_for_the_part_chapters():
    """user.md 要求 Part 填 ``estimated_chapters``，并写明总和约束。"""
    user = (REPO_ROOT / PRECISE_PROMPT_REL / "user.md").read_bytes().decode("utf-8")

    assert "estimated_chapters" in user
    assert "Part 还要填写 estimated_chapters" in user
    assert "之和必须等于目标总章数" in user


# ======================================================================
#  B. 骨架兜底 + 合并
# ======================================================================


def test_the_skeleton_carries_a_fallback_chapter_count():
    """骨架的 part 节点带**兜底**章数（AI 没给时用）。"""
    svc = _svc()

    skeleton = svc._build_precise_structure_skeleton(
        120, {"parts": 3, "volumes_per_part": 1, "acts_per_volume": 1}
    )

    counts = [part.get("estimated_chapters") for part in skeleton["parts"]]
    assert len(counts) == 3
    assert all(isinstance(c, int) and c > 0 for c in counts)
    # 兜底值自洽：各部之和 = 目标章数
    assert sum(counts) == 120


def test_the_ai_part_chapters_win_over_the_fallback():
    """AI 给的部级章数**生效**（覆盖兜底）。"""
    svc = _svc()
    skeleton = svc._build_precise_structure_skeleton(
        120, {"parts": 3, "volumes_per_part": 1, "acts_per_volume": 1}
    )
    updates = {
        "node_updates": [
            {"node_id": "P1", "title": "起源", "description": "x", "estimated_chapters": 10},
            {"node_id": "P2", "title": "发展", "description": "x", "estimated_chapters": 20},
            {"node_id": "P3", "title": "决战", "description": "x", "estimated_chapters": 90},
        ]
    }

    merged = svc._merge_precise_structure_updates(
        skeleton, updates, 120, rebalance=False
    )

    assert [p["estimated_chapters"] for p in merged["parts"]] == [10, 20, 90]


def test_the_fallback_stays_when_the_ai_gives_nothing():
    """AI 没给部级章数 ⇒ 兜底值仍在（不丢）。"""
    svc = _svc()
    skeleton = svc._build_precise_structure_skeleton(
        120, {"parts": 3, "volumes_per_part": 1, "acts_per_volume": 1}
    )
    fallback = [p["estimated_chapters"] for p in skeleton["parts"]]

    merged = svc._merge_precise_structure_updates(
        skeleton, {"node_updates": []}, 120, rebalance=False
    )

    assert [p["estimated_chapters"] for p in merged["parts"]] == fallback


def test_a_mismatched_part_budget_is_reported_not_rewritten(caplog):
    """部级之和不符 ⇒ **告警**，但**不改写** AI 的原值（与幕级 B-03 同纪律）。"""
    import logging

    svc = _svc()
    skeleton = svc._build_precise_structure_skeleton(
        120, {"parts": 3, "volumes_per_part": 1, "acts_per_volume": 1}
    )
    updates = {
        "node_updates": [
            {"node_id": "P1", "title": "a", "description": "x", "estimated_chapters": 10},
            {"node_id": "P2", "title": "b", "description": "x", "estimated_chapters": 10},
            {"node_id": "P3", "title": "c", "description": "x", "estimated_chapters": 10},
        ]
    }

    with caplog.at_level(logging.WARNING):
        merged = svc._merge_precise_structure_updates(
            skeleton, updates, 120, rebalance=False
        )

    # AI 原值保留（没有被改写成 40/40/40）
    assert [p["estimated_chapters"] for p in merged["parts"]] == [10, 10, 10]
    assert any("部级章数预算" in r.message for r in caplog.records), (
        "部级章数之和不符却没有告警"
    )


# ======================================================================
#  C. 校验实现只有一份
# ======================================================================


def test_the_level_budget_check_is_shared_with_the_act_level():
    """部级与幕级用**同一份**实现（不写两份）。"""
    nodes = [{"estimated_chapters": 5}, {"estimated_chapters": 15}]

    act_report = check_act_chapter_budget(nodes, 20)
    level_report = check_level_chapter_budget(nodes, 20, level_label="幕")

    assert act_report["total"] == level_report["total"]
    assert act_report["ok"] == level_report["ok"]
    assert act_report["missing"] == level_report["missing"]
    # 幕级包装的返回形状**不变**（既有测试钉住了那 7 个键）
    assert set(act_report) == {
        "total", "target", "delta", "ok", "act_count", "missing", "warning"
    }
    assert act_report["act_count"] == 2


def test_the_part_level_warning_names_the_level():
    """部级告警要写清是「部级」（否则和幕级告警分不清）。"""
    report = check_level_chapter_budget(
        [{"estimated_chapters": 1}], 100, level_label="部"
    )

    assert "部级章数之和" in report["warning"]
    assert "1 个部" in report["warning"]
