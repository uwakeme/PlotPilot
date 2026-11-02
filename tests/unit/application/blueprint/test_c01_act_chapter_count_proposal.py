# -*- coding: utf-8 -*-
"""第 83 批（N-06）：C-01「AI 报『本幕只需 N 章』」的提案通道。

病根（第 80 批独立验收 N-06）
----------------------------
「每幕章数」此前**只有代码侧**的出口（custom → ``suggested_chapter_count`` → 公式），
提示词还把章数写成**硬约束**（「请为这一幕规划 {chapter_count} 个章节」
「必须严格输出 {chapter_count} 章」）⇒ **AI 没有任何「这一幕其实只需更少章」的出口**
⇒ 而「这一幕该多长」是创作判断（登记表 C-01 的「该谁定」列写着「AI」）
⇒ AI 若真按剧情只写了 3 章，校验器会判它「不完整/被截断」⇒ 规划失败。

本文件钉住
----------
1. **提案裁决器**：能采纳 / 该拒绝的四种情形（**拒绝必须给理由**）；
2. **行为级**：AI 报 3 章（预算 8）⇒ 规划**通过**（这正是此前被逼死的场景）；
3. **提示词**：不再把章数写成硬约束，且确实给出了提案字段；
4. **接线**：校验用的是**生效章数**，不是代码解析出来的参考章数。
"""
from __future__ import annotations

import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]   # ← 仓库根（tests/unit/application/blueprint/x.py ⇒ parents[4]）
USER_MD = ROOT / "infrastructure" / "ai" / "prompt_packages" / "nodes" / "planning-act" / "user.md"

sys.path.insert(0, str(ROOT))

from application.blueprint.services.chapter_planning_policy import (  # noqa: E402
    resolve_ai_chapter_count_proposal,
)


# ── 1. 提案裁决器 ───────────────────────────────────────────────────────


def test_no_proposal_keeps_the_code_budget():
    """⚠ AI 没报 ⇒ 用代码解析出来的章数（= 改造前的行为，逐位一致）。"""
    accepted, count, reason = resolve_ai_chapter_count_proposal(None, 8)
    assert accepted is False
    assert count == 8
    assert reason == ""


def test_a_smaller_proposal_is_accepted():
    """⚠ 「本幕只需 3 章」⇒ 采纳（这是 C-01 要的那个出口）。"""
    accepted, count, reason = resolve_ai_chapter_count_proposal(3, 8)
    assert accepted is True
    assert count == 3
    assert reason == ""


def test_the_same_count_is_accepted():
    """⚠ 报与参考值相同 ⇒ 采纳（无变化，但来源是 AI）。"""
    accepted, count, _reason = resolve_ai_chapter_count_proposal(8, 8)
    assert accepted is True
    assert count == 8


def test_a_larger_proposal_is_rejected_with_a_reason():
    """⚠ **不增章**（增章会破坏全书总量守恒 ⇒ 那是 J-01 的职责）。"""
    accepted, count, reason = resolve_ai_chapter_count_proposal(9, 8)
    assert accepted is False
    assert count == 8
    assert reason, "拒绝必须给理由（静默回落 = 「AI 说了但没人听见」）"


@pytest.mark.parametrize("bad", [0, -1, -5])
def test_a_non_positive_proposal_is_rejected_with_a_reason(bad):
    accepted, count, reason = resolve_ai_chapter_count_proposal(bad, 8)
    assert accepted is False
    assert count == 8
    assert reason


@pytest.mark.parametrize("bad", ["5", 5.0, None.__class__])
def test_a_non_integer_proposal_is_rejected_with_a_reason(bad):
    accepted, count, reason = resolve_ai_chapter_count_proposal(bad, 8)
    assert accepted is False
    assert count == 8
    assert reason


def test_a_boolean_proposal_is_not_treated_as_a_number():
    """⚠ ``True`` 是 ``int`` 的子类 ⇒ 用 ``isinstance(x, int)`` 会把 True 当 1 章。"""
    accepted, count, reason = resolve_ai_chapter_count_proposal(True, 8)
    assert accepted is False, "bool 被当成了章数 1"
    assert count == 8
    assert reason


# ── 2. 行为级：AI 报更少的章数 ⇒ 规划通过 ──────────────────────────────


def _chapter(number: int, total: int = 3) -> dict:
    """⚠ ``foreshadow_action`` 是配对校验器的必填项（否则会被判「缺字段」）。"""
    action = "plant_and_resolve" if number == total else "plant"
    return {
        "number": number,
        "title": f"第{number}章",
        "main_event": "推进主事件",
        "handoff_from_previous": "承接前章后果",
        "handoff_to_next": "留下下一章钩子",
        "required_threads": ["主线"],
        "foreshadow_action": action,
    }


def _service(monkeypatch, llm_payload: dict):
    """造一个「只走 plan_act_chapters 那一段」的假服务。"""
    from application.blueprint.services.continuous_planning_service import (
        ContinuousPlanningService,
    )

    service = ContinuousPlanningService.__new__(ContinuousPlanningService)
    service.story_node_repo = SimpleNamespace(
        get_by_id=_async_return(SimpleNamespace(id="act-1", novel_id="n1", number=1, title="第一幕", description="", content=""))
    )
    service.chapter_element_repo = None
    service.llm_service = None
    service.bible_service = None
    service.chapter_repository = None
    service.novel_repository = None

    monkeypatch.setattr(
        ContinuousPlanningService, "_get_bible_context", lambda self, *a, **k: {}
    )

    async def _fake_previous(self, act_node):
        return ""

    monkeypatch.setattr(
        ContinuousPlanningService, "_get_previous_acts_summary", _fake_previous
    )

    async def _fake_count(self, act_id, custom_chapter_count=None):
        return 8

    monkeypatch.setattr(
        ContinuousPlanningService, "resolve_act_planning_chapter_count", _fake_count
    )

    async def _fake_direction(self, **kwargs):
        return SimpleNamespace(act_direction="")

    monkeypatch.setattr(
        ContinuousPlanningService, "_resolve_act_direction", _fake_direction
    )

    def _fake_prompt(self, *a, **k):
        return SimpleNamespace(user="u", system="s")

    monkeypatch.setattr(
        ContinuousPlanningService, "_build_act_planning_prompt", _fake_prompt
    )

    async def _fake_stream(self, act_id, prompt, config):
        return json.dumps(llm_payload, ensure_ascii=False)

    monkeypatch.setattr(
        ContinuousPlanningService, "_stream_act_plan_llm_text", _fake_stream
    )
    return service


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


@pytest.mark.asyncio
async def test_the_ai_may_say_this_act_only_needs_fewer_chapters(monkeypatch):
    """⚠ **核心行为**：AI 报 3 章（预算 8）⇒ 规划**通过**。

    ⚠ 改造前：AI 返回 3 章而预算是 8 ⇒ 校验判「不完整/被截断」⇒ 规划失败
    ⇒ 「这一幕只需更少章」这个创作判断**没有出口**。
    """
    service = _service(
        monkeypatch,
        {"proposed_chapter_count": 3, "chapters": [_chapter(i) for i in range(1, 4)]},
    )

    result = await service.plan_act_chapters("act-1")

    assert result["success"] is True, (
        "AI 报「本幕只需 3 章」却被判失败 ⇒ C-01 的通道没建起来：%s"
        % result.get("validation_errors")
    )
    assert result["act_chapter_count"] == 3
    assert result["act_chapter_count_source"] == "ai", "来源没如实记（AI 定的却说是代码定的）"


@pytest.mark.asyncio
async def test_without_a_proposal_the_code_budget_still_applies(monkeypatch):
    """⚠ 对照组：AI 没报 ⇒ 仍按代码的章数校验（行为逐位不变）。"""
    service = _service(
        monkeypatch, {"chapters": [_chapter(i) for i in range(1, 6)]}
    )

    result = await service.plan_act_chapters("act-1")

    assert result["success"] is False, "5 章 vs 预算 8 章 ⇒ 仍应判不完整"
    assert result["act_chapter_count"] == 8
    assert result["act_chapter_count_source"] == "code"


@pytest.mark.asyncio
async def test_a_rejected_proposal_is_visible(monkeypatch):
    """⚠ 提案被拒 ⇒ 理由必须**看得见**（不静默回落）。"""
    service = _service(
        monkeypatch,
        {"proposed_chapter_count": 20, "chapters": [_chapter(i, 8) for i in range(1, 9)]},
    )

    result = await service.plan_act_chapters("act-1")

    assert result["success"] is True, "8 章 = 参考章数 ⇒ 计划本身合规"
    assert result["act_chapter_count"] == 8
    assert result["act_chapter_count_source"] == "code"
    assert result["act_chapter_count_rejection"], "拒绝理由不见了 ⇒ 「AI 说了但没人听见」"


@pytest.mark.asyncio
async def test_the_ai_must_be_self_consistent(monkeypatch):
    """⚠ 报了 3 章却给了 5 章 ⇒ 判失败（不许「章数与提案对不上」也放过）。"""
    service = _service(
        monkeypatch,
        {"proposed_chapter_count": 3, "chapters": [_chapter(i) for i in range(1, 6)]},
    )

    result = await service.plan_act_chapters("act-1")

    assert result["success"] is False, "提案与实际章数不一致却放过了"


# ── 3. 提示词：不再把章数写成硬约束 ────────────────────────────────────


def test_the_prompt_no_longer_forces_the_chapter_count():
    """⚠ 硬约束「必须严格输出 {chapter_count} 章」= 把创作判断按死。"""
    text = USER_MD.read_text(encoding="utf-8-sig")
    assert "必须严格输出 {chapter_count} 章" not in text, (
        "提示词仍把章数写成硬约束 ⇒ AI 没有「本幕只需 N 章」的出口"
    )
    assert "请为这一幕规划 {chapter_count} 个章节" not in text, (
        "提示词仍在命令式规定章数"
    )


def test_the_prompt_offers_the_proposal_field():
    """⚠ 提示词必须说明提案字段的名字（否则模型不知道往哪写）。"""
    text = USER_MD.read_text(encoding="utf-8-sig")
    assert "proposed_chapter_count" in text, "提示词没提提案字段 ⇒ 模型不会输出它"


# ── 4. 接线：校验用的是生效章数 ────────────────────────────────────────


def test_the_validation_uses_the_effective_count():
    """⚠ 接线守卫：校验那一行必须用**生效章数**，不是参考章数。"""
    from application.blueprint.services import continuous_planning_service as mod

    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8-sig")
    assert "resolve_ai_chapter_count_proposal(" in src, "提案裁决器没被接上"
    assert "expected_count=effective_count" in src, (
        "校验还在用参考章数 ⇒ AI 报了更少的章数也过不了"
    )
