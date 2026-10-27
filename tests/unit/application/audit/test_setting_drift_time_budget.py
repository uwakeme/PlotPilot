"""第 23 批：设定漂移审计的**单章时间预算**（A1 成本护栏）。

背景
----
第 21 批生产实测：单段 A1 调用 **312 秒**（``glm-5.3-flash``），
而长章会切成最多 ``_MAX_SEGMENTS`` 段 ⇒ 慢 provider 下**一章可能拖 20 分钟**
（4 段 × 5 分钟），且它挂在**章末关键路径**上（整章实测 28 分钟）。

⇒ 现在有**单章**时间预算：预算耗尽 ⇒ 停止送审剩余段 ⇒
   - 结论显式报「覆盖不全」（``coverage="partial"`` / ``audited=False``）⇒ **不报假通过**
   - **已审出的结论照样保留**（不把已经查到的丢掉）

⚠ 守卫纪律：预算相关测试**不依赖真实时钟**（`_now()` 可被替换）⇒ 不慢机器上偶发失败。
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from application.audit.services import setting_drift_audit as sda
from application.audit.services.setting_drift_audit import (
    DEFAULT_TIME_BUDGET_SECONDS,
    SettingDriftAuditService,
)


def _bible():
    bible = MagicMock()
    ws = MagicMock()
    ws.category = "力量体系"
    ws.content = "修为分九境，不可越境战斗"
    loc = MagicMock()
    loc.name = "临渊城"
    loc.description = "北境边城，常年冰封"
    loc.location_type = "city"
    bible.world_settings = [ws]
    bible.locations = [loc]
    return bible


def _service():
    svc = SettingDriftAuditService(llm_service=MagicMock(), bible_repository=MagicMock())
    svc._bible_repository.get_by_novel_id = MagicMock(return_value=_bible())
    return svc


def _capture(svc, responses):
    calls = []

    async def _fake(variables):
        calls.append(variables)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    svc._invoke = _fake
    return calls


def _ok(score=0, issues=None):
    return json.dumps({"drift_score": score, "issues": issues or []})


def _fake_clock(monkeypatch, step: float = 1.0):
    """把 ``_now()`` 换成**每次调用前进 step 秒**的假时钟 ⇒ 预算测试不靠真实时间。"""
    state = {"t": 0.0}

    def _now() -> float:
        state["t"] += step
        return state["t"]

    monkeypatch.setattr(sda, "_now", _now)
    return state


# ── 1) 预算充足 ⇒ 行为与旧版完全一致 ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_generous_budget_behaves_exactly_like_before(monkeypatch):
    """预算够用 ⇒ 全部段都送审 ⇒ coverage=full / audited=True。"""
    svc = _service()
    calls = _capture(svc, [_ok(score=10)])
    _fake_clock(monkeypatch, step=0.1)
    text = "段落内容。" * 1500  # 7500 字 ⇒ 2 段

    result = await svc.audit_chapter("n-1", 3, text, time_budget=600.0)

    assert len(calls) == 2, calls
    assert result["audited"] is True
    assert result["coverage"] == "full"
    assert result["drift_score"] == 10


@pytest.mark.asyncio
async def test_no_budget_means_unlimited(monkeypatch):
    """⚠ 不传预算（0）⇒ 与旧行为一致（手动审稿路径走的就是这条）。"""
    svc = _service()
    calls = _capture(svc, [_ok(score=10)])
    _fake_clock(monkeypatch, step=1000.0)  # 时钟狂跳也不该影响「不限预算」
    text = "段落内容。" * 1500

    result = await svc.audit_chapter("n-1", 3, text)

    assert len(calls) == 2, calls
    assert result["coverage"] == "full"


# ── 2) 预算耗尽 ⇒ 停止送审，但**不报假通过** ────────────────────────────


@pytest.mark.asyncio
async def test_the_budget_stops_the_remaining_segments(monkeypatch):
    """预算耗尽 ⇒ 剩下的段**不送审**（不能继续烧 LLM）。"""
    svc = _service()
    calls = _capture(svc, [_ok(score=10)])
    _fake_clock(monkeypatch, step=1.0)
    text = "段落内容。" * 1500  # 7500 字 ⇒ 2 段

    # 时钟每次前进 1 秒：deadline=t0+budget；第 1 段 remaining=budget-1，第 2 段=budget-2
    result = await svc.audit_chapter("n-1", 3, text, time_budget=1.5)

    assert len(calls) == 1, f"预算耗尽后还送了 {len(calls)} 段：{calls}"
    assert result["audited"] is False
    assert result["coverage"] == "partial"
    assert result["drift_score"] is None


@pytest.mark.asyncio
async def test_the_exhausted_budget_is_explicitly_reported(monkeypatch):
    """⚠ 理由里必须写明是**时间预算**（不能写成「章节过长」——那是另一回事）。"""
    svc = _service()
    _capture(svc, [_ok(score=10)])
    _fake_clock(monkeypatch, step=1.0)

    result = await svc.audit_chapter("n-1", 3, "段落内容。" * 1500, time_budget=1.5)

    assert "预算" in str(result.get("reason")), result
    assert "过长" not in str(result.get("reason")), result


@pytest.mark.asyncio
async def test_the_findings_from_audited_segments_are_kept(monkeypatch):
    """⚠ 预算耗尽时**已审出的结论照样保留**（不把查到的丢掉）。"""
    svc = _service()
    _capture(
        svc,
        [
            _ok(
                score=70,
                issues=[
                    {
                        "severity": "critical",
                        "category": "ability",
                        "description": "第一段就违反了能力锁",
                        "suggestion": "改",
                    }
                ],
            )
        ],
    )
    _fake_clock(monkeypatch, step=1.0)

    result = await svc.audit_chapter("n-1", 3, "段落内容。" * 1500, time_budget=1.5)

    assert result["audited"] is False
    assert result["segments_audited"] == 1
    assert [i["description"] for i in result["issues"]] == ["第一段就违反了能力锁"]


# ── 3) 单段超时 ⇒ 该段不算审成，其余段照旧 ──────────────────────────────


@pytest.mark.asyncio
async def test_a_slow_segment_times_out_and_the_rest_keeps_going(monkeypatch):
    """⚠ 单段超预算 ⇒ 该段**不算审成**（不许当成审过了）。"""
    svc = _service()

    async def _slow(variables):
        if variables["segment_index"] == 1:
            raise TimeoutError()
        return _ok(score=5)

    svc._invoke = _slow
    _fake_clock(monkeypatch, step=1.0)

    result = await svc.audit_chapter("n-1", 3, "段落内容。" * 1500, time_budget=600.0)

    assert result["audited"] is False
    assert result["coverage"] == "partial"
    assert result["segments_audited"] == 1
    assert "超时" in str(result.get("reason")), result


# ── 4) 预算是**整章共享**的（不是每段一份） ─────────────────────────────


@pytest.mark.asyncio
async def test_the_budget_is_shared_across_all_segments(monkeypatch):
    """⚠ 预算是**整章**的 ⇒ 每段各拿一份预算 = 没有上限（那才是 bug）。"""
    svc = _service()
    calls = _capture(svc, [_ok(score=1)])
    _fake_clock(monkeypatch, step=1.0)
    text = "段落内容。" * 2500  # 12500 字 ⇒ 3 段

    # 每段 1 秒、总共 2.5 秒 ⇒ 第 3 段时预算已尽 ⇒ 只能审 2 段
    result = await svc.audit_chapter("n-1", 3, text, time_budget=2.5)

    assert len(calls) == 2, f"预算是整章共享的，不该审 {len(calls)} 段"
    assert result["segments_audited"] == 2
    assert result["segments_total"] == 3


# ── 5) 预算常量本身 ─────────────────────────────────────────────────────


def test_the_default_budget_is_a_positive_number():
    """默认预算必须**大于 0**（0 = 不限，等于没护栏）。"""
    assert DEFAULT_TIME_BUDGET_SECONDS > 0
