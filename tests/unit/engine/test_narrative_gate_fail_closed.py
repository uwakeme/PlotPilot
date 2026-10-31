"""第 66 批守卫：B-3 —— 叙事三态的「默认放行口」必须**全部**堵上。

背景（独立审查报告 ``review-briefs/2026-10-30-review-report-85c0f726-closeout.md`` B-3）
------------------------------------------------------------------------------
``engine/runtime/audit_delegate.py::_resolve_narrative_gate`` 原本写的是
``drift_result.get("narrative_sync_ok", True)`` ⇒ **缺键 = 已同步**。

而章后管线的**异常回退路径**（legacy）返回的结果里**根本没有**这个键
⇒ 「没跑出来」被判成「跑出来成功」⇒
  ① ``novel.last_audit_narrative_ok`` 落库成 1 ⇒ 面板显示「叙事已同步」；
  ② 同时 ``hard_narrative=False`` ⇒ **章后硬闸在这条路径上不拦**。

同一函数里超时路径是对的（``None`` → ``unassessed``）⇒ 口径分裂。

本文件钉住
----------
1. **缺键 ⇒ 未评估**（fail-closed），不许默认 True；
2. legacy 回退路径**显式**声明 ``narrative_sync_ok=None``；
3. **从未审计**的书不许自称「已同步」（实体默认值 ⇒ 未评估 ⇒ 落库 NULL）；
4. API 读侧不许把「缺键」默认成 True（四个消费点）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine.runtime.audit_delegate import (
    _narrative_shared_value,
    _resolve_narrative_gate,
)


# ─────────────────────── 1) 判定层：缺键 ⇒ 未评估 ───────────────────────


def test_a_missing_key_means_unassessed_not_synced():
    """⚠ 缺键 = **未评估**，不是「已同步」。

    ⚠ 这是 B-3 的核心：旧默认是 ``True`` ⇒ 章后管线的异常回退路径
    （legacy）返回的结果里没有这个键 ⇒ 「没测出来」被说成「测出来成功」。
    """
    hard_narrative, evaluated, state = _resolve_narrative_gate({})

    assert state == "unassessed", f"缺键被判成了 {state!r}（旧默认是 'ok'）"
    assert evaluated is False, "缺键却宣称「已评估」"
    assert hard_narrative is True, "缺键时章后硬闸必须照拦（fail-closed）"


def test_the_three_states_are_distinguishable():
    """三态必须**互不相同**：已同步 / 明确失败 / 未评估。"""
    assert _resolve_narrative_gate({"narrative_sync_ok": True}) == (False, True, "ok")
    assert _resolve_narrative_gate({"narrative_sync_ok": False}) == (True, True, "failed")
    assert _resolve_narrative_gate({"narrative_sync_ok": None}) == (True, False, "unassessed")


def test_the_shared_value_is_none_when_unassessed():
    """共享内存那一层也不许把「未评估」压成 False/True。"""
    assert _narrative_shared_value({}) is None
    assert _narrative_shared_value({"narrative_sync_ok": None}) is None
    assert _narrative_shared_value({"narrative_sync_ok": False}) is False
    assert _narrative_shared_value({"narrative_sync_ok": True}) is True


# ─────────────────────── 2) legacy 回退路径（生产形状） ───────────────────────


class _StubBackgroundTaskService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def submit_task(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _StubVoiceDriftService:
    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload if payload is not None else {
            "similarity_score": 0.93,
            "drift_alert": False,
        }

    def score_chapter(self, **kwargs) -> dict:
        return dict(self.payload)


def _legacy_host(voice_service):
    """构造一个**真的**能跑 ``_legacy_auditing_tasks_and_voice`` 的宿主。"""
    from engine.runtime.daemon_host import DaemonHostMixin

    host = DaemonHostMixin.__new__(DaemonHostMixin)
    host.background_task_service = _StubBackgroundTaskService()
    host.voice_drift_service = voice_service
    return host


def _novel_stub():
    return SimpleNamespace(novel_id=SimpleNamespace(value="n-1"))


def test_the_legacy_fallback_declares_unassessed_explicitly():
    """⚠ legacy 回退路径必须**显式**写 ``narrative_sync_ok=None``。

    ⚠ 为什么必须显式：这条路径 = 章后管线**没跑**（异常回退 / 管线压根没装配）
    ⇒ 叙事同步根本没被评估过 ⇒ 以前**缺这个键** ⇒ 被下游的旧默认说成「已同步」。
    """
    host = _legacy_host(_StubVoiceDriftService())

    result = host._legacy_auditing_tasks_and_voice(
        _novel_stub(), 1, "正文", SimpleNamespace(value="ch-1")
    )

    assert "narrative_sync_ok" in result, (
        "legacy 结果里没有这个键 ⇒ 下游只能靠「缺键默认」⇒ 一定会漂"
    )
    assert result["narrative_sync_ok"] is None, (
        f"legacy 结果把未评估写成了 {result['narrative_sync_ok']!r}"
    )


def test_the_legacy_fallback_without_voice_service_also_declares_unassessed():
    """⚠ 连文风服务都没有的那条分支，也必须显式声明未评估。"""
    host = _legacy_host(None)

    result = host._legacy_auditing_tasks_and_voice(
        _novel_stub(), 1, "正文", SimpleNamespace(value="ch-1")
    )

    assert result.get("narrative_sync_ok", "MISSING") is None


def test_the_legacy_result_never_reports_synced():
    """⚠ 端到端：legacy 结果喂进判定层 ⇒ 必须判「未评估」且硬闸照拦。"""
    host = _legacy_host(_StubVoiceDriftService())

    result = host._legacy_auditing_tasks_and_voice(
        _novel_stub(), 1, "正文", SimpleNamespace(value="ch-1")
    )

    hard_narrative, evaluated, state = _resolve_narrative_gate(result)
    assert state == "unassessed", f"legacy 结果被判成了 {state!r}"
    assert evaluated is False
    assert hard_narrative is True, "legacy 路径上章后硬闸不拦"
    assert _narrative_shared_value(result) is None


# ─────────────────────── 3) 实体默认值：从未审计 ⇒ 未评估 ───────────────────────


def test_a_never_audited_novel_is_unassessed():
    """⚠ 一本**从未审过**的书，默认必须是「未评估」，不是「已同步」。

    ⚠ 旧默认 True ⇒ 落库就是 1 ⇒ DB 层谎报「叙事已同步」
    （生产库实测：25 本书里 22 行「从未审计却 narrative_ok=1」）。
    """
    from domain.novel.entities.novel import Novel
    from domain.novel.value_objects.novel_id import NovelId

    novel = Novel(id=NovelId("n-1"), title="t", author="a", target_chapters=10)

    assert novel.last_audit_narrative_ok is None, (
        f"从未审计的书自称 {novel.last_audit_narrative_ok!r} ⇒ 落库会变成 1"
    )


def test_the_unassessed_value_is_written_as_null():
    """⚠ 未评估 ⇒ DB 列必须是 NULL（不是 0，也不是 1）。"""
    from infrastructure.persistence.database.sqlite_novel_repository import (
        narrative_ok_to_db,
    )

    assert narrative_ok_to_db(None) is None
    assert narrative_ok_to_db(True) == 1
    assert narrative_ok_to_db(False) == 0


# ─────────────────────── 4) API 读侧：四个消费点都不许默认 True ───────────────────────


def _stub_novel_without_narrative_flag():
    """一个**故意不带** ``last_audit_narrative_ok`` 的小说替身。"""
    return SimpleNamespace(
        target_chapters=10,
        target_words_per_chapter=2500,
        autopilot_status=SimpleNamespace(value="running"),
        current_stage=SimpleNamespace(value="writing"),
        last_audit_chapter_number=3,
        last_chapter_tension=70,
        last_audit_similarity=0.9,
        last_audit_drift_alert=False,
        last_audit_at="2026-10-30T00:00:00",
        last_audit_vector_stored=True,
        last_audit_foreshadow_stored=True,
        last_audit_triples_extracted=True,
        last_audit_quality_scores={},
        last_audit_issues=[],
    )


def test_the_api_does_not_default_the_narrative_flag_to_synced():
    """⚠ API 读侧缺键 ⇒ 必须给 ``None``（未评估），不是 ``True``（已同步）。"""
    from interfaces.api.v1.engine.autopilot_routes import _build_fallback_status

    status = _build_fallback_status(_stub_novel_without_narrative_flag())

    audit = status.get("last_chapter_audit")
    assert audit is not None, "降级路径没给出章后审阅快照（测试前提失效）"
    assert audit["narrative_sync_ok"] is None, (
        f"API 把「未评估」说成了 {audit['narrative_sync_ok']!r}"
    )


@pytest.mark.parametrize(
    "rel",
    [
        "interfaces/api/v1/engine/autopilot_routes.py",
        "engine/runtime/audit_delegate.py",
        "engine/runtime/daemon_host.py",
        # ⚠ 第 70 批（R-10）补进扫描范围：仓储层也有一处同款「缺键 ⇒ True」
        #   （`sqlite_novel_repository.py:120`）⇒ 实体默认值改了，但口子还在。
        "infrastructure/persistence/database/sqlite_novel_repository.py",
    ],
)
def test_no_backend_consumer_defaults_the_narrative_flag_to_true(rel: str):
    """⚠ 通则守卫：后端**任何**地方都不许再写「缺键 ⇒ True」。

    ⚠ 扫描的是**确切字面量**（``...narrative_sync_ok", True`` 这种），
    不是「文件里出现过 True」⇒ 变异掉其中一处会立刻红。

    ⚠ 第 66 批变异验证发现的洞：原先只扫 ``last_audit_narrative_ok``（小说实体那个键），
    而**章后管线结果**那个键叫 ``narrative_sync_ok`` ⇒ 变异打在后者的默认 True 上时
    这条守卫**不会红** ⇒ 现在两个键都扫。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[3] / rel).read_text(encoding="utf-8")

    offenders = [
        line.strip()
        for line in src.splitlines()
        if any(key in line for key in ("last_audit_narrative_ok", "narrative_sync_ok"))
        and "True" in line
    ]
    assert not offenders, (
        f"{rel} 里仍有「缺键 ⇒ True」的写法（三态被折叠）：{offenders}"
    )
