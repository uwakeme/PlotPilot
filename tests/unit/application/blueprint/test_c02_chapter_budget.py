"""C-02：每幕章数的兜底链收口（观测优先 / 公式兜底 / 显式「未评估」）。

登记表的 C-02 写的是「每幕章数兜底」：兜底链 ``custom or suggested or _default_cpa``，
其中 ``_default_cpa`` **恒取公式阶梯表**（``continuous_planning_service._act_budget_context``
里的 ``params["chapters_per_act"]``），且目标章数读不到时静默按 100 章算。
核实后病因有三处：

1. **口径分裂**：同一个 ``_act_budget_context`` 里 ``total_acts`` 是观测优先，
   ``chapters_per_act`` 却 100% 取公式 ⇒ 一个函数两套口径。
2. **两条链**：规划侧（``continuous_planning_service``）与守护进程侧
   （``engine/runtime/act_planning_delegate``）各写一条兜底链，公式基准还不一样
   （DB 读 vs 内存值 + 硬编码 100）⇒ 同一本书在两条路径上算出的每幕章数可能不同。
3. **静默降级**：读库失败只 ``logger.debug``，读者以为拿到的是真实值。

本文件的守卫钉住修完之后的口径：
**观测优先 → 公式兜底 → 显式「未评估」**，且两条链共用同一份实现。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.blueprint.services.continuous_planning_service import (
    ActPlanningChapterBudgetUnavailable,
    ContinuousPlanningService,
)
from application.blueprint.services.structure_budget import (
    CHAPTERS_PER_ACT_FORMULA,
    CHAPTERS_PER_ACT_OBSERVED,
    CHAPTERS_PER_ACT_UNASSESSED,
    observe_chapters_per_act,
    resolve_chapters_per_act,
)


# ======================================================================
#  替身
# ======================================================================


def _node(node_id, number, node_type, parent_id, **extra):
    return SimpleNamespace(
        id=node_id,
        number=number,
        node_type=SimpleNamespace(value=node_type),
        parent_id=parent_id,
        **extra,
    )


def _tree_repo(nodes):
    repo = Mock()
    repo.get_tree_sync = Mock(return_value=SimpleNamespace(nodes=list(nodes)))
    return repo


def _svc(nodes, *, target_chapters=120, novel_id="n-1"):
    novel_repository = Mock()
    novel_repository.get_by_id = Mock(
        return_value=SimpleNamespace(target_chapters=target_chapters, id=novel_id)
    )
    return ContinuousPlanningService(
        story_node_repo=_tree_repo(nodes),
        chapter_element_repo=Mock(),
        llm_service=Mock(),
        novel_repository=novel_repository,
    )


def _act_with_chapters(act_no: int, chapter_count: int, *, novel_id="n-1"):
    """一幕 + 它底下的若干章节节点。"""
    nodes = [_node(f"act-{act_no}", act_no, "act", "vol-1")]
    for i in range(chapter_count):
        nodes.append(_node(f"ch-{act_no}-{i}", i + 1, "chapter", f"act-{act_no}"))
    return nodes


# ======================================================================
#  1. 共享口径：观测优先 → 公式兜底 → 显式未评估
# ======================================================================


def test_shared_resolver_prefers_observation_over_formula():
    """有观测值时**以观测值为准**，公式不参与取大。"""
    value, source = resolve_chapters_per_act(target_chapters=120, observed_chapters_per_act=3)
    assert value == 3
    assert source == CHAPTERS_PER_ACT_OBSERVED


def test_shared_resolver_falls_back_to_formula_without_observation():
    """没有观测值时用公式阶梯表（既有语义保留）。"""
    value, source = resolve_chapters_per_act(target_chapters=120)
    assert source == CHAPTERS_PER_ACT_FORMULA
    assert value == 6  # calculate_structure_params(120)["chapters_per_act"]


def test_shared_resolver_reports_unassessed_instead_of_inventing_a_number():
    """目标不可用且没观测 ⇒ **不给数字**，显式「未评估」。"""
    value, source = resolve_chapters_per_act(target_chapters=None)
    assert value is None
    assert source == CHAPTERS_PER_ACT_UNASSESSED

    value, source = resolve_chapters_per_act(target_chapters=0)
    assert value is None
    assert source == CHAPTERS_PER_ACT_UNASSESSED


def test_observe_chapters_per_act_averages_and_ceils():
    """观测值 = 各幕章数的平均（向上取整）—— max 会被离群值带偏。"""
    nodes = _act_with_chapters(1, 5) + _act_with_chapters(2, 8)
    # (5 + 8) / 2 = 6.5 → 向上取整 7
    assert observe_chapters_per_act(nodes) == 7


def test_observe_chapters_per_act_returns_zero_without_chapters():
    """没有章节节点 ⇒ 0（= 没观测到，不是「每幕 0 章」）。"""
    assert observe_chapters_per_act([]) == 0
    assert observe_chapters_per_act([_node("act-1", 1, "act", "vol-1")]) == 0


# ======================================================================
#  2. 规划侧：与总幕数同一套口径（观测优先）+ 来源可见
# ======================================================================


def test_act_budget_context_uses_observed_chapters_per_act():
    """规划侧：每幕章数**取观测值**（此前 100% 取公式）。"""
    nodes = _act_with_chapters(1, 4) + _act_with_chapters(2, 6)
    svc = _svc(nodes)

    ctx = svc._act_budget_context("n-1", 2)

    assert ctx["chapters_per_act"] == 5  # (4+6)/2
    assert ctx["chapters_per_act_source"] == CHAPTERS_PER_ACT_OBSERVED


def test_act_budget_context_reports_formula_source_without_observation():
    """反向守卫：没有观测时用公式（既有语义不许被改坏）。"""
    svc = _svc([_node("act-1", 1, "act", "vol-1")])

    ctx = svc._act_budget_context("n-1", 1)

    assert ctx["chapters_per_act"] == 6
    assert ctx["chapters_per_act_source"] == CHAPTERS_PER_ACT_FORMULA


def test_act_budget_context_never_renders_none_into_brief():
    """目标未知时 brief 里不许出现「None 章」（那是把「没说话」伪装成信号）。"""
    svc = _svc([], target_chapters=None)
    svc.novel_repository.get_by_id = Mock(return_value=SimpleNamespace(target_chapters=None))

    ctx = svc._act_budget_context("n-1", 1)

    assert ctx["chapters_per_act"] is None
    assert ctx["chapters_per_act_source"] == CHAPTERS_PER_ACT_UNASSESSED
    assert "None" not in ctx["brief"]


def test_act_budget_context_unassessed_when_novel_repository_missing():
    """仓储不可用 ⇒ 目标**未评估**（不许静默退回 100 章基准）。

    「目标未知」与「目标是 100」必须可分辨：后者是一个真实的目标值，
    前者算不出每幕章数（显式「未评估」）。
    """
    svc = _svc([])
    svc.novel_repository = None

    ctx = svc._act_budget_context("n-1", 1)

    assert ctx["target_chapters"] is None
    assert ctx["chapters_per_act"] is None
    assert ctx["chapters_per_act_source"] == CHAPTERS_PER_ACT_UNASSESSED


def test_act_budget_context_unassessed_when_target_read_raises():
    """读目标章数抛异常 ⇒ 目标**未评估**（不许静默退回 100 章基准）。"""
    svc = _svc([])
    svc.novel_repository.get_by_id = Mock(side_effect=RuntimeError("db is locked"))

    ctx = svc._act_budget_context("n-1", 1)

    assert ctx["target_chapters"] is None
    assert ctx["chapters_per_act"] is None
    assert ctx["chapters_per_act_source"] == CHAPTERS_PER_ACT_UNASSESSED


@pytest.mark.asyncio
async def test_resolve_act_planning_chapter_count_refuses_to_invent_a_number():
    """每幕章数算不出来 ⇒ **抛错**（跳过该幕规划），不返回虚构数字。"""
    svc = _svc([])
    svc.novel_repository.get_by_id = Mock(return_value=SimpleNamespace(target_chapters=None))
    svc.story_node_repo.get_by_id = AsyncMock(
        return_value=SimpleNamespace(
            id="act-1", novel_id="n-1", number=1, suggested_chapter_count=None
        )
    )

    with pytest.raises(ActPlanningChapterBudgetUnavailable):
        await svc.resolve_act_planning_chapter_count("act-1")


# ======================================================================
#  3. 守护进程侧：同一条口径（观测优先 / 未评估就跳过）
# ======================================================================


@pytest.mark.asyncio
async def test_delegate_uses_observed_chapters_per_act(monkeypatch):
    """守护进程侧与规划侧**同一套口径**：有观测时用观测值，不用公式。"""
    from domain.novel.entities.novel import NovelStage
    from engine.runtime import act_planning_delegate

    volumes = [_node("vol-1", 1, "volume", "part-1", title="卷一")]
    acts = [_node("act-1", 1, "act", "vol-1", suggested_chapter_count=None)]
    chapters = [
        _node(f"ch-{i}", i, "chapter", "act-1") for i in range(1, 4)
    ]  # 观测：3 章/幕
    nodes = volumes + acts + chapters

    host = Mock()
    host._is_still_running.return_value = True
    host.story_node_repo.get_by_novel = AsyncMock(return_value=nodes)
    host.story_node_repo.get_children_sync.return_value = []
    host._report_llm_failure = Mock()
    host._flush_novel = Mock()
    host._update_shared_state = Mock()

    captured = {}

    async def _fake_request(host, novel, *, target_act, chapter_budget):
        captured["chapter_budget"] = chapter_budget
        return SimpleNamespace(
            status="failed",
            payload={},
            session_id="",
            operation="autopilot.act.plan",
            node_key="planning-act",
            autopilot_pause_reason="",
        )

    monkeypatch.setattr(act_planning_delegate, "_read_shared_state", lambda _nid: {})
    monkeypatch.setattr(act_planning_delegate, "_request_act_invocation", _fake_request)

    novel = Mock()
    novel.novel_id.value = "n-1"
    novel.current_act = 0
    novel.target_chapters = 120  # 公式会算 5 幕/幕
    novel.current_stage = NovelStage.ACT_PLANNING

    await act_planning_delegate.run_act_planning(host, novel)

    assert captured.get("chapter_budget") == 3, "必须用观测到的 3 章/幕，而不是公式的 5"


@pytest.mark.asyncio
async def test_delegate_skips_act_planning_when_budget_unassessed(monkeypatch):
    """目标章数不可用且没有观测 ⇒ **跳过本幕规划**，不调 LLM、不编数字。"""
    from domain.novel.entities.novel import NovelStage
    from engine.runtime import act_planning_delegate

    volumes = [_node("vol-1", 1, "volume", "part-1", title="卷一")]
    acts = [_node("act-1", 1, "act", "vol-1", suggested_chapter_count=None)]
    nodes = volumes + acts

    host = Mock()
    host._is_still_running.return_value = True
    host.story_node_repo.get_by_novel = AsyncMock(return_value=nodes)
    host.story_node_repo.get_children_sync.return_value = []
    host._report_llm_failure = Mock()
    host._flush_novel = Mock()
    host._update_shared_state = Mock()

    request_invocation = AsyncMock()
    monkeypatch.setattr(act_planning_delegate, "_read_shared_state", lambda _nid: {})
    monkeypatch.setattr(act_planning_delegate, "_request_act_invocation", request_invocation)

    novel = Mock()
    novel.novel_id.value = "n-1"
    novel.current_act = 0
    novel.target_chapters = None  # 目标未设置 ⇒ 算不出每幕章数
    novel.current_stage = NovelStage.ACT_PLANNING

    await act_planning_delegate.run_act_planning(host, novel)

    request_invocation.assert_not_awaited()
    assert host._report_llm_failure.called, "跳过必须让用户看得见（失败摘要）"


# ======================================================================
#  4. API 契约：每幕章数「未评估」⇒ 503（不是 404，也不是 500）
# ======================================================================


@pytest.mark.asyncio
async def test_act_chapters_route_returns_503_when_budget_unassessed():
    """C-02 的 API 契约：每幕章数未评估 ⇒ 503（404 会谎称「幕不存在」）。"""
    from fastapi import HTTPException

    from interfaces.api.v1.blueprint.continuous_planning_routes import (
        generate_act_chapters,
    )

    service = Mock()
    service.plan_act_chapters = AsyncMock(
        side_effect=ActPlanningChapterBudgetUnavailable("每幕章数未评估")
    )

    with pytest.raises(HTTPException) as exc_info:
        await generate_act_chapters(
            act_id="act-1",
            request=SimpleNamespace(chapter_count=None),
            service=service,
        )

    assert exc_info.value.status_code == 503
    assert "未评估" in str(exc_info.value.detail)
