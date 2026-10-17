"""幕内伏笔配对校验器（C-01 兜底安全网）。

契约来源：`infrastructure/ai/prompt_packages/nodes/planning-act/user.md:29,36`
—— 提示词早就把伏笔节奏写成硬约束，但此前**无任何代码校验**。
实测生产库 105 幕全部合规 ⇒ 模型一直遵守，代码从不校验。

本文件把每条规则各钉一条用例，并额外验证接线：
`validate_lightweight_act_plan` 现在会把配对错误一并返回。
"""
import pytest

from application.blueprint.services.chapter_planning_policy import (
    validate_lightweight_act_plan,
)
from application.blueprint.services.foreshadow_pairing import (
    FORESHADOW_ACTIONS,
    validate_foreshadow_pairing,
)


def _ch(actions):
    """按给定动作序列造一整幕的章节计划。"""
    return [{"number": i + 1, "foreshadow_action": a} for i, a in enumerate(actions)]


# ── R1 动作取值 ────────────────────────────────────────────────────────

def test_all_four_documented_actions_are_accepted():
    assert FORESHADOW_ACTIONS == {"plant", "resolve", "plant_and_resolve", "none"}
    plan = _ch(["plant", "plant", "resolve", "plant_and_resolve"])
    assert validate_foreshadow_pairing(plan) == []


def test_missing_action_is_an_error_not_a_silent_default():
    """缺失绝不按 ``none`` 猜 —— 提示词写的是「必选其一」。"""
    plan = [{"number": 1}, {"number": 2, "foreshadow_action": "resolve"}]
    errors = validate_foreshadow_pairing(plan)
    assert any("missing foreshadow_action" in e for e in errors), errors


def test_unknown_action_value_is_rejected():
    plan = _ch(["plant", "resolve"])
    plan[1]["foreshadow_action"] = "plant_resolve"      # 拼写近似但非法
    errors = validate_foreshadow_pairing(plan)
    assert any("invalid foreshadow_action" in e for e in errors), errors


def test_blank_string_counts_as_missing():
    plan = _ch(["plant", "   ", "resolve"])
    errors = validate_foreshadow_pairing(plan)
    assert any("missing foreshadow_action" in e for e in errors), errors


# ── R2 none 上限 ───────────────────────────────────────────────────────

def test_three_none_chapters_exceeds_the_limit():
    errors = validate_foreshadow_pairing(_ch(["none", "none", "none"]))
    assert any("none count 3 exceeds limit 2" in e for e in errors), errors


def test_two_none_chapters_is_allowed_when_not_adjacent():
    """R2 与 R3 是两条独立规则：2 章 none 合法，但**不能相邻**。"""
    errors = validate_foreshadow_pairing(
        _ch(["plant", "none", "resolve", "none", "plant_and_resolve"])
    )
    assert errors == [], errors


# ── R3 不可连续 2 章 none ──────────────────────────────────────────────

def test_two_consecutive_none_chapters_are_rejected():
    errors = validate_foreshadow_pairing(
        _ch(["plant", "none", "none", "resolve", "plant_and_resolve"])
    )
    assert any("consecutive none" in e for e in errors), errors


# ── R4 末章必须回收 ────────────────────────────────────────────────────

def test_last_chapter_must_resolve_or_plant_and_resolve():
    errors = validate_foreshadow_pairing(_ch(["plant", "plant"]))
    assert any("last chapter must be resolve or plant_and_resolve" in e
               for e in errors), errors


def test_none_as_last_chapter_is_rejected():
    errors = validate_foreshadow_pairing(_ch(["plant", "resolve", "none"]))
    assert any("last chapter" in e for e in errors), errors


# ── R5 种了就要收（本幕内聚合） ─────────────────────────────────────────

def test_planting_without_any_resolving_in_act_is_rejected():
    errors = validate_foreshadow_pairing(_ch(["plant", "plant", "plant"]))
    assert any("never resolves any" in e for e in errors), errors


def test_plant_and_resolve_counts_as_both():
    errors = validate_foreshadow_pairing(
        _ch(["plant", "plant_and_resolve"])
    )
    assert errors == [], errors


# ── 接线：校验链真的把配对错误带出来了 ──────────────────────────────────

def _full_chapter(number: int, action: str) -> dict:
    return {
        "number": number,
        "title": f"第{number}章",
        "main_event": "推进主事件",
        "handoff_from_previous": "承接前章后果",
        "handoff_to_next": "留下下一章钩子",
        "foreshadow_action": action,
    }


def test_act_plan_validation_surfaces_pairing_errors():
    """接线守卫：去掉接线这条就会红 —— 那正是「建了不用」的空洞交付。"""
    plan = [_full_chapter(i + 1, "plant") for i in range(4)]
    errors = validate_lightweight_act_plan(plan, expected_count=4)

    assert errors, "校验链没有带出伏笔配对错误 ⇒ 校验器没被接上"
    assert any("foreshadow" in e or "none count" in e or "never resolves" in e
               for e in errors), errors


def test_act_plan_validation_passes_a_legal_plan():
    """反向守卫：合规计划不能被误杀（实测 105 幕全部合规）。"""
    plan = [
        _full_chapter(1, "plant"),
        _full_chapter(2, "plant"),
        _full_chapter(3, "resolve"),
        _full_chapter(4, "plant_and_resolve"),
    ]
    assert validate_lightweight_act_plan(plan, expected_count=4) == []


def test_non_list_input_is_rejected_without_crashing():
    assert validate_foreshadow_pairing(None) == ["chapters must be a list"]
    assert validate_foreshadow_pairing({"a": 1}) == ["chapters must be a list"]


def test_non_object_chapter_does_not_crash():
    errors = validate_foreshadow_pairing(["not-a-dict"])
    assert any("must be an object" in e for e in errors), errors


def test_empty_plan_is_not_reported_as_violation():
    """空列表没有「末章」可言，不该凭空造一条 R4。"""
    assert validate_foreshadow_pairing([]) == []
