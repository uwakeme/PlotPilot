"""E-01a：「七段执行剧本」的段标题必须**只有一处**定义。

核实：登记表 E-01 的 V2 修正指出真正的 SSOT 在 `chapter_plan_renderer.py`，
而 `chapter_planning_policy.py` 的 marker 常量是**另一份平行清单**
⇒「哪七段」写了两遍，改一处就会与另一处对不上。

⇒ 现在段标题 + 计数单位由
`chapter_planning_policy.CHAPTER_EXECUTION_PLAN_SECTIONS` **唯一定义**，
marker 常量由它**派生**，渲染器从它取标题。

本文件钉住三件事：
1. marker 常量确实由那张表派生（不是手写的第二份）；
2. 渲染器源码里**不再**出现段标题字面量（重复真的没了）；
3. **渲染结果逐字节不变**（本笔是纯重构，不许改模型看到的东西）。
"""
from __future__ import annotations

import ast
from pathlib import Path

from application.blueprint.services.chapter_plan_renderer import (
    render_chapter_execution_plan,
)
from application.blueprint.services.chapter_planning_policy import (
    CHAPTER_EXECUTION_PLAN_MARKERS,
    CHAPTER_EXECUTION_PLAN_SECTIONS,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
RENDERER = REPO_ROOT / "application" / "blueprint" / "services" / "chapter_plan_renderer.py"

EXPECTED_MARKERS = (
    "一、开篇切入点",
    "二、场景转换列表",
    "三、关键对话",
    "四、剧情事件链",
    "五、角色关键决策",
    "六、爽点/反转设计",
    "七、主角状态变化",
)


def test_markers_are_derived_from_the_single_sections_table():
    """marker 常量由那张表派生 ⇒ 只有一个事实源。"""
    assert CHAPTER_EXECUTION_PLAN_MARKERS == tuple(
        marker for marker, _unit in CHAPTER_EXECUTION_PLAN_SECTIONS
    )
    # 顺带把「哪七段」的内容钉住（改了标题就是改了模型看到的骨架）
    assert CHAPTER_EXECUTION_PLAN_MARKERS == EXPECTED_MARKERS
    assert len(CHAPTER_EXECUTION_PLAN_SECTIONS) == 7


def test_markers_are_not_a_hand_written_second_copy():
    """⚠ 只比「值相等」不够 —— 手写的第二份清单**值也相等**。

    ⇒ 这里断言源码里 marker 常量是**派生**的（生成器表达式），
    而不是手写的元组字面量。
    """
    policy_path = (
        REPO_ROOT / "application" / "blueprint" / "services" / "chapter_planning_policy.py"
    )
    tree = ast.parse(policy_path.read_text(encoding="utf-8"))
    value = None
    for item in tree.body:
        if not isinstance(item, ast.Assign):
            continue
        if any(
            getattr(target, "id", None) == "CHAPTER_EXECUTION_PLAN_MARKERS"
            for target in item.targets
        ):
            value = item.value
            break

    assert value is not None, "找不到 CHAPTER_EXECUTION_PLAN_MARKERS 的赋值"
    assert not isinstance(value, ast.Tuple), (
        "marker 常量又变回手写的元组了 ⇒ 第二份清单回来了"
    )
    # 允许 `tuple(<生成器表达式>)` / `<生成器表达式>` 两种派生写法
    inner = value.args[0] if isinstance(value, ast.Call) and value.args else value
    assert isinstance(inner, ast.GeneratorExp), (
        "marker 常量必须由 CHAPTER_EXECUTION_PLAN_SECTIONS 派生"
    )


def test_renderer_no_longer_contains_section_titles():
    """渲染器源码里**不再**出现段标题字面量（第二份清单真的删了）。"""
    source = RENDERER.read_text(encoding="utf-8")
    offenders = [marker for marker in EXPECTED_MARKERS if marker in source]
    assert not offenders, (
        f"渲染器里仍写死了段标题（第二份清单）：{offenders}"
    )


def test_renderer_derives_titles_from_the_sections_table():
    """渲染器确实**引用**那张表（不是碰巧没有字面量）。"""
    tree = ast.parse(RENDERER.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported.add(alias.name)
    assert "CHAPTER_EXECUTION_PLAN_SECTIONS" in imported, (
        "渲染器没有引用 CHAPTER_EXECUTION_PLAN_SECTIONS ⇒ 标题来源没统一"
    )


def test_rendered_plan_is_byte_identical_to_the_previous_behavior():
    """纯重构：渲染结果**逐字节**不变（模型看到的东西不许动）。"""
    plan = {
        "opening_entry": "雨夜推门",
        "scene_transitions": [{"scene": "客栈", "location": "大漠", "cast": "主角"}],
        "key_dialogues": [
            {"speaker": "甲", "line": "一"},
            {"speaker": "乙", "line": "二"},
        ],
        "event_chain": [{"event": "刺杀"}],
        "character_decisions": [{"decision": "反击"}],
        "payoff_reversals": [{"payoff": "反转"}],
        "protagonist_state_change": {"伤势": "加重"},
    }

    rendered = render_chapter_execution_plan(plan)

    expected = "\n".join(
        [
            "一、开篇切入点：",
            "雨夜推门",
            "二、场景转换列表：",
            "客栈 → 大漠 | 主角 |",
            "三、关键对话（2组）：",
            "对话1：甲→一",
            "对话2：乙→二",
            "四、剧情事件链（1个事件）：",
            "事件1：刺杀",
            "五、角色关键决策：",
            "反击",
            "六、爽点/反转设计：",
            "payoff：反转",
            "七、主角状态变化：",
            "伤势：加重",
        ]
    )
    assert rendered == expected


def test_empty_sections_render_the_placeholder():
    """空段仍渲染「（待补充）」（不许因为重构而少一段）。"""
    rendered = render_chapter_execution_plan({"opening_entry": "开场"})

    assert rendered.count("（待补充）") == 6
    assert "一、开篇切入点：" in rendered
    assert "七、主角状态变化：" in rendered
