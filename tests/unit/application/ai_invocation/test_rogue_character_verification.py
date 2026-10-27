"""第 22 批：K 类·越界角色的确定性核对 —— **接线与端到端**。

⚠ 这组守卫**真跑续处理钩子**（``execute_continuation`` ⇒ ``autopilot_audit``）
⇒ 从「LLM 接受的内容」一路走到「返回的 ``chapter.audit.risk_flags``」，
覆盖**接线**（审计调用点有没有把白名单传下去）。

守卫清单
--------
- 白名单内的角色被报成 K ⇒ **驳回**（不再冒充越界角色）
- 驳回**留痕**（不许静默丢）且与真越界**可分辨**
- 白名单外的角色 ⇒ **确认**保留 ⇒ **K 类的正例在这一层可验**
- 白名单取不到 ⇒ 不下结论（保留原样）
- 其它类别（A/B/J…）**不受影响**
"""
from __future__ import annotations

import json

import pytest

from application.ai_invocation.autopilot import continuations
from application.ai_invocation.autopilot.continuations import (
    _merge_risk_flags,
    _whitelist_variants,
)
from application.ai_invocation.continuation import ContinuationContext, execute_continuation
from application.ai_invocation.dtos import InvocationPolicy
from application.audit.services.character_whitelist_check import (
    REJECTED_CATEGORY,
    expand_whitelist,
)

WHITELIST_ENTRIES = ["沈砚青", "聂九枝（艺名玉惊鸿）", "贺九（贺松龄，人称九爷）"]
VARIANTS = sorted(expand_whitelist(WHITELIST_ENTRIES))


class _FakeSession:
    def __init__(self, novel_id: str, chapter_number: int):
        self.context = {"novel_id": novel_id, "chapter_number": chapter_number}
        self.policy = InvocationPolicy.DIRECT
        self.session_id = "session-test"
        self.continuation = type("_C", (), {"handler_key": "autopilot_audit"})()


class _FakeDecision:
    def __init__(self, accepted_content: str):
        self.accepted_content = accepted_content


def _run_audit(hits: list[dict]) -> list[dict]:
    """真跑一次审计续处理 ⇒ 返回 risk_flags。"""
    continuations.register_autopilot_continuations()
    payload = {"total_hits": len(hits), "hits": hits, "overall_assessment": "轻微"}
    ctx = ContinuationContext(
        session=_FakeSession("novel-test", 1),
        decision=_FakeDecision(json.dumps(payload, ensure_ascii=False)),
    )
    result = execute_continuation(ctx)
    return list(result.get("chapter.audit.risk_flags") or [])


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch):
    """⚠ 代码通道会去读正文 ⇒ 换成空的假库（不许碰真库）。"""
    import infrastructure.persistence.database.connection as connection_module

    class _EmptyDb:
        def fetch_one(self, sql, params=()):
            return None

    monkeypatch.setattr(
        connection_module, "get_database", lambda *a, **k: _EmptyDb()
    )


# ── 1) 端到端：白名单内的角色被驳回 ──────────────────────────────────────


def test_a_whitelisted_character_is_not_reported_as_a_rogue_one(monkeypatch):
    """⚠ 模型把白名单内角色报成越界 ⇒ 面板上不该出现一个假越界角色。"""
    monkeypatch.setattr(
        continuations, "_whitelist_variants", lambda novel_id: list(VARIANTS)
    )

    flags = _run_audit(
        [
            {
                "category": "K",
                "pattern": "聂九枝",
                "position": "中段",
                "severity": "critical",
                "replacement": "改成白名单内角色",
            }
        ]
    )

    assert not [f for f in flags if f.get("category") == "K"], flags
    rejected = [f for f in flags if f.get("category") == REJECTED_CATEGORY]
    assert rejected, f"驳回没有留痕（静默丢了结论）：{flags}"
    assert rejected[0].get("severity") == "info", rejected
    assert rejected[0].get("verified") == "rejected_by_whitelist_check", rejected


def test_the_rejection_is_distinguishable_from_a_real_rogue_character(monkeypatch):
    """⚠ 驳回项与真越界**必须可分辨**（面板上要能看出哪个是真问题）。"""
    monkeypatch.setattr(
        continuations, "_whitelist_variants", lambda novel_id: list(VARIANTS)
    )

    flags = _run_audit(
        [
            {"category": "K", "pattern": "聂九枝", "position": "中段", "severity": "critical"},
            {"category": "K", "pattern": "柳三娘", "position": "后段", "severity": "critical"},
        ]
    )

    real = [f for f in flags if f.get("category") == "K"]
    assert len(real) == 1, flags
    assert real[0].get("text") == "柳三娘", real
    assert real[0].get("verified") == "confirmed_out_of_whitelist", real
    assert real[0].get("severity") == "critical", "真越界的严重性不许被降级"


def test_a_genuinely_rogue_character_survives_the_check(monkeypatch):
    """⚠ **K 类的正例在这一层可验**（生产上验不到的那一半，这里能验）。"""
    monkeypatch.setattr(
        continuations, "_whitelist_variants", lambda novel_id: list(VARIANTS)
    )

    flags = _run_audit(
        [
            {
                "category": "K",
                "pattern": "柳三娘",
                "position": "后段",
                "severity": "critical",
                "replacement": "改成白名单内角色",
            }
        ]
    )

    real = [f for f in flags if f.get("category") == "K"]
    assert len(real) == 1, flags
    assert real[0].get("text") == "柳三娘", real
    assert real[0].get("verified") == "confirmed_out_of_whitelist", real


def test_an_alias_of_a_whitelisted_character_is_also_rejected(monkeypatch):
    """别名同样要驳回（「玉惊鸿」不在裸名列表里 ⇒ 模型容易误报）。"""
    monkeypatch.setattr(
        continuations, "_whitelist_variants", lambda novel_id: list(VARIANTS)
    )

    flags = _run_audit(
        [{"category": "K", "pattern": "玉惊鸿", "position": "前段", "severity": "warning"}]
    )

    assert not [f for f in flags if f.get("category") == "K"], flags
    assert [f for f in flags if f.get("category") == REJECTED_CATEGORY], flags


# ── 2) 白名单取不到 ⇒ 不下结论 ──────────────────────────────────────────


def test_nothing_is_rejected_when_the_whitelist_is_unavailable(monkeypatch):
    """⚠ 白名单取不到 ⇒ **不许臆测**（否则会把真越界当成误报删掉）。"""
    monkeypatch.setattr(continuations, "_whitelist_variants", lambda novel_id: [])

    flags = _run_audit(
        [{"category": "K", "pattern": "沈砚青", "position": "中段", "severity": "critical"}]
    )

    real = [f for f in flags if f.get("category") == "K"]
    assert len(real) == 1, flags
    assert not [f for f in flags if f.get("category") == REJECTED_CATEGORY], flags


# ── 3) 其它类别不受影响 ─────────────────────────────────────────────────


def test_the_other_categories_are_untouched(monkeypatch):
    """⚠ 核对**只**作用于 K 类 ⇒ A/B/J… 一条都不许被改写。"""
    monkeypatch.setattr(
        continuations, "_whitelist_variants", lambda novel_id: list(VARIANTS)
    )

    flags = _run_audit(
        [
            {"category": "A", "pattern": "沈砚青眼中微亮", "position": "中段", "severity": "warning"},
            {"category": "B", "pattern": "一字一顿", "position": "前段", "severity": "info"},
            {"category": "J", "pattern": "声痕水响", "position": "后段", "severity": "info"},
        ]
    )

    assert [f.get("category") for f in flags] == ["A", "B", "J"], flags
    assert all("verified" not in f for f in flags), flags


# ── 4) 白名单本身取自 Bible（同源）───────────────────────────────────────


def test_the_whitelist_variants_come_from_the_bible(monkeypatch):
    """⚠ 白名单必须与生成侧**同源**（Bible 的角色名单）⇒ 不会两边分叉。"""
    import infrastructure.persistence.database.connection as connection_module
    import infrastructure.persistence.database.sqlite_bible_repository as bible_module

    class _FakeCharacter:
        def __init__(self, name: str):
            self.name = name

    class _FakeBible:
        characters = [_FakeCharacter(n) for n in WHITELIST_ENTRIES]

    class _FakeRepo:
        def __init__(self, db):
            pass

        def get_by_novel_id(self, novel_id):
            return _FakeBible()

    monkeypatch.setattr(connection_module, "get_database", lambda *a, **k: object())
    monkeypatch.setattr(bible_module, "SqliteBibleRepository", _FakeRepo)

    variants = _whitelist_variants("novel-test")

    assert "沈砚青" in variants
    assert "玉惊鸿" in variants, "别名没被展开"
    assert "九爷" in variants, "别名没被展开"


def test_the_whitelist_lookup_survives_a_broken_database(monkeypatch):
    """取不到白名单 ⇒ 返回空 ⇒ 核对不下结论（不许把审计搞崩）。"""
    import infrastructure.persistence.database.connection as connection_module

    def _boom(*args, **kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(connection_module, "get_database", _boom)

    assert _whitelist_variants("novel-test") == []


# ── 5) 合并函数本身的默认行为（不传白名单 ⇒ 与旧行为一致）───────────────


def test_the_merge_keeps_its_old_behaviour_without_a_whitelist():
    """⚠ 不传白名单 ⇒ 与第 16 批的行为**完全一致**（老调用方不受影响）。"""
    payload = {
        "hits": [
            {"category": "K", "pattern": "柳三娘", "position": "后段", "severity": "critical"}
        ]
    }
    merged = _merge_risk_flags(payload, [])
    assert len(merged) == 1, merged
    assert merged[0]["category"] == "K"
    assert "verified" not in merged[0]
