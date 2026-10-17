"""B-03：幕级章数「校验告警，不改写」。

改造前
------
``_rebalance_act_chapters`` 把 LLM 给的 ``estimated_chapters`` 当成**权重**，
等比缩放到 ``target_chapters`` 后**直接覆盖**原值，且**没有任何告警分支** ——
AI 对每幕章数的判断在落库前被静默改写。

登记表 B-03 要求：保留 LLM 原值；总数不符时**告警而非改写**。

核心断言
--------
**AI 的原值必须活下来。** 只断言「返回了一个报告」是测不出这个的 ——
旧实现也会返回东西。断言必须落在「各幕的 estimated_chapters 一个都没变」。
"""
import pytest

from application.blueprint.services.act_chapter_budget import (
    check_act_chapter_budget,
    read_act_chapters,
)
from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
)


def _svc():
    return object.__new__(ContinuousPlanningService)


# ── 核心：原值不被改写 ─────────────────────────────────────────────────

def test_llm_values_survive_by_default():
    """**核心红线**：默认调用后，各幕章数必须一字不动。"""
    svc = _svc()
    acts = [{"estimated_chapters": 10}, {"estimated_chapters": 5}, {"estimated_chapters": 3}]
    svc._rebalance_act_chapters(acts, 20)
    assert [a["estimated_chapters"] for a in acts] == [10, 5, 3]


def test_mismatch_produces_a_warning():
    svc = _svc()
    acts = [{"estimated_chapters": 10}, {"estimated_chapters": 5}]
    report = svc._rebalance_act_chapters(acts, 20)
    assert report["ok"] is False
    assert report["total"] == 15
    assert report["delta"] == -5
    assert "保留 LLM 原值" in report["warning"], report["warning"]


def test_no_warning_when_already_balanced():
    svc = _svc()
    acts = [{"estimated_chapters": 12}, {"estimated_chapters": 8}]
    report = svc._rebalance_act_chapters(acts, 20)
    assert report["ok"] is True
    assert report["warning"] == ""


def test_normalize_true_restores_old_behaviour():
    """显式开启归一时，等比守恒行为仍在（供需要严格守恒的场景）。"""
    svc = _svc()
    acts = [{"estimated_chapters": 10}, {"estimated_chapters": 5}, {"estimated_chapters": 3}]
    svc._rebalance_act_chapters(acts, 20, normalize=True)
    assert sum(a["estimated_chapters"] for a in acts) == 20


def test_empty_acts_is_safe():
    svc = _svc()
    acts = []
    report = svc._rebalance_act_chapters(acts, 20)
    assert report["act_count"] == 0
    assert report["total"] == 0
    assert acts == []


# ── 纯函数 ─────────────────────────────────────────────────────────────

def test_report_shape():
    acts = [{"estimated_chapters": 10}, {"estimated_chapters": 5}, {"estimated_chapters": 0}]
    rep = check_act_chapter_budget(acts, 20)
    assert set(rep) == {"total", "target", "delta", "ok", "act_count", "missing", "warning"}
    assert rep["missing"] == 1, "未给章数的幕要被数出来"


def test_read_handles_string_numbers():
    """LLM 常返回字符串（既有测试里就有）。"""
    assert read_act_chapters({"estimated_chapters": "6"}) == 6
    assert read_act_chapters({"estimated_chapters": None}) == 0
    assert read_act_chapters({"estimated_chapters": "abc"}) == 0
    assert read_act_chapters({}) == 0
    assert read_act_chapters({"estimated_chapters": -3}) == 0


def test_mixed_string_and_int():
    acts = [{"estimated_chapters": "10"}, {"estimated_chapters": 10}]
    rep = check_act_chapter_budget(acts, 20)
    assert rep["total"] == 20
    assert rep["ok"] is True


def test_report_does_not_mutate_input():
    acts = [{"estimated_chapters": 10}, {"estimated_chapters": 5}]
    check_act_chapter_budget(acts, 20)
    assert [a["estimated_chapters"] for a in acts] == [10, 5]


# ── 不变量：总和必须等于 target（现在是告警，不再是改写） ────────────────

def test_the_invariant_is_now_reported_not_enforced():
    """设计意图的直接断言：矛盾时**报告**，不**纠正**。

    登记表写「总和必须等于 target_chapters（校验告警，不改写）」。
    若有人日后把行为改回静默归一，这条会红。
    """
    svc = _svc()
    acts = [{"estimated_chapters": 100}, {"estimated_chapters": 100}]
    report = svc._rebalance_act_chapters(acts, 20)
    assert sum(a["estimated_chapters"] for a in acts) == 200
    assert report["ok"] is False
    assert report["delta"] == 180


def test_legacy_wrapper_is_gone():
    """不该留一个无人调用的并行入口 —— 那是「不新增平行概念」的红线。"""
    assert not hasattr(ContinuousPlanningService, "_legacy_rebalance_act_chapters")
