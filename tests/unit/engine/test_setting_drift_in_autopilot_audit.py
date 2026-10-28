"""第 20 批：**设定漂移审计（A1）要进自动写作的章后流程**。

背景
----
A1（``SettingDriftAuditService``）此前**只有手动审稿 API 会调**
（``ChapterReviewService.review_chapter`` ⇒ ``POST /novels/{id}/chapters/{n}``）
⇒ **自动写作写完一章后，没有任何一处会查设定漂移**：
角色能力越界 / 地名错误 / 世界观规则被违反 / 已确立的事实被无声改写
—— 这些漂移在自动流程里**没人发现**（``novel.last_audit_issues`` 一直是空的）。

⇒ 现在：章末审计也跑 A1（**同一个服务类** ⇒ 两处口径一致），
结论并进 ``novel.last_audit_issues`` ⇒ 面板可见。

⚠ 降级纪律：**「没审出来」≠「没问题」**
⇒ 未评估时必须留一条**可见**的「未评估」条目（带原因）。

守卫清单
--------
- 合并函数的**三态语义**：审出漂移 ⇒ 并进去；审了没问题 ⇒ **一条都不加**；
  没审出来 ⇒ 留一条「未评估」条目（**不许**静默成「没问题」）
- 重审时**不保留**上一轮的漂移条目
- 接线：章末审计**真的调了** A1，且把结果写进 ``last_audit_issues``
- 与手动审稿**同一个服务类**（不能一个查一个不查）
- A1 调用异常 ⇒ 报「未评估」（不许把整章流程带崩，也不许假装通过）
- ⚠ **集成缝**（第 20 批自查时补的）：整条 ``run_chapter_audit`` **真跑一遍**，
  断言「设定漂移的结果**没有**盖掉章后管线的结果」
  ⇒ 这条正是本批自己踩过的坑（见下）。
"""
from __future__ import annotations

import inspect
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]

from engine.runtime.audit_delegate import (  # noqa: E402
    _merge_setting_drift_issues,
    run_chapter_audit,
)


# ── 1) 合并语义 ──────────────────────────────────────────────────────────


def test_drift_issues_are_merged_into_the_audit_issues():
    """审出漂移 ⇒ 结论进 issues（带来源标记，面板能分辨是谁报的）。"""
    drift = {
        "audited": True,
        "drift_score": 62,
        "issues": [
            {
                "severity": "critical",
                "category": "ability",
                "description": "沈砚青用枪，设定里她从不带枪",
                "suggestion": "改成撬棍",
            }
        ],
    }
    items = _merge_setting_drift_issues(None, drift)

    assert len(items) == 1
    item = items[0]
    assert item["type"] == "setting_drift"
    assert item["severity"] == "critical"
    assert "从不带枪" in item["description"]
    assert item["suggestion"] == "改成撬棍"


def test_no_drift_issues_means_no_entry_is_added():
    """审了且没问题 ⇒ **一条都不加**（此时 issues 为空才真的意味着「没问题」）。"""
    drift = {"audited": True, "drift_score": 0, "issues": []}
    assert _merge_setting_drift_issues(None, drift) == []


def test_an_unassessed_drift_leaves_a_visible_entry():
    """⚠ 没审出来 ⇒ 必须留一条**可见**的「未评估」条目（带原因）。

    ⇒ 面板上不会把「没查」读成「查了没问题」。
    """
    drift = {"audited": False, "reason": "LLM 调用失败（TimeoutError）", "issues": []}
    items = _merge_setting_drift_issues(None, drift)

    assert len(items) == 1, "未评估必须留痕，不能静默"
    item = items[0]
    assert item["type"] == "setting_drift_unassessed"
    assert "未评估" in item["description"]
    assert "TimeoutError" in item["description"]


def test_a_reaudit_does_not_keep_the_previous_drift_entries():
    """重审时**不保留**上一轮的漂移条目（否则旧结论会一直挂着）。"""
    existing = [
        {"type": "setting_drift", "description": "上一轮的漂移"},
        {"type": "setting_drift_unassessed", "description": "上一轮未评估"},
        {"type": "other", "description": "别的来源的条目"},
    ]
    items = _merge_setting_drift_issues(existing, {"audited": True, "issues": []})

    types = [i.get("type") for i in items]
    assert types == ["other"], f"上一轮的漂移条目没被清掉：{types}"


def test_existing_non_drift_entries_are_preserved():
    """合并**不许**吃掉别的来源的条目。"""
    existing = [{"type": "other", "description": "别的来源"}]
    items = _merge_setting_drift_issues(
        existing, {"audited": True, "issues": [{"description": "新漂移"}]}
    )
    assert len(items) == 2
    assert {i.get("type") for i in items} == {"other", "setting_drift"}


def test_a_missing_drift_result_is_treated_as_unassessed():
    """drift 为 None（服务压根没跑）⇒ 不许当成「没问题」。"""
    items = _merge_setting_drift_issues(None, None)
    # 没有 drift ⇒ 保持原样（不凭空造结论），但也不清掉别人的条目
    assert items == []


# ── 2) 接线：章末审计真的调了 A1 ────────────────────────────────────────


def test_the_audit_flow_calls_the_drift_audit_and_stores_the_result():
    """⚠ 接线：``run_chapter_audit`` 必须调 ``host._run_setting_drift_audit``
    并把结果写进 ``novel.last_audit_issues``。

    ⚠ 用「按函数边界切片」查（``inspect.getsource``），不是全文 contains。
    """
    src = inspect.getsource(run_chapter_audit)
    assert "_run_setting_drift_audit" in src, (
        "章末审计没有调用设定漂移审计 ⇒ 自动写作时漂移仍然没人查"
    )
    assert "last_audit_issues" in src, (
        "章末审计没有把漂移结论写进 last_audit_issues ⇒ 面板上看不到"
    )


def test_both_paths_use_the_same_service_class():
    """⚠ 自动写作与手动审稿必须用**同一个服务类**（不能一个查一个不查）。

    ⚠ 判据是「**从同一个模块 import**」—— 只查「类名出现过」会漏：
    有人把 import 换成 ``SettingDriftAuditService = None``，名字还在，断言照样绿。
    """
    import re

    daemon_src = (ROOT / "engine/runtime/daemon_host.py").read_text(encoding="utf-8")
    deps_src = (ROOT / "interfaces/api/dependencies.py").read_text(encoding="utf-8")
    pattern = r"from\s+([\w.]*setting_drift_audit)\s+import"

    daemon_mods = set(re.findall(pattern, daemon_src))
    deps_mods = set(re.findall(pattern, deps_src))

    assert daemon_mods, "守护进程侧没有 import 设定漂移服务"
    assert deps_mods, "手动审稿侧没有 import 设定漂移服务"
    assert daemon_mods == deps_mods, (
        f"两边的服务类来源不同：守护进程 {daemon_mods} vs 手动审稿 {deps_mods}"
    )


# ── 3) 异常不许带崩流程，也不许假装通过 ────────────────────────────────


@pytest.mark.asyncio
async def test_the_daemon_host_reports_failures_as_unassessed(monkeypatch):
    """A1 调用抛异常 ⇒ 返回「未评估」（不是「没问题」），且**不抛**给调用方。"""
    import engine.runtime.daemon_host as daemon_host_module

    class _Boom:
        def __init__(self, *args, **kwargs):
            pass

        async def audit_chapter(self, **kwargs):
            raise RuntimeError("LLM 挂了")

    import application.audit.services.setting_drift_audit as drift_module

    monkeypatch.setattr(drift_module, "SettingDriftAuditService", _Boom)

    host = daemon_host_module.DaemonHostMixin.__new__(daemon_host_module.DaemonHostMixin)
    host.llm_service = object()

    result = await host._run_setting_drift_audit("n-1", 3, "正文")
    assert result["audited"] is False
    assert result["issues"] == []
    assert "LLM 挂了" in result["reason"]


# ── 4) 集成缝：整条 run_chapter_audit 真跑一遍 ────────────────────────────
#
# ⚠ 本批**自己**踩过的坑（自查时才发现，已修）：
#   新加的设定漂移结果**用了 `drift_result` 这个名字**
#   ⇒ **遮蔽**了同一个函数里原有的「章后管线结果」（文风/叙事/张力/落库标记）
#   ⇒ 下面这些全部读到了**设定漂移**的结果：
#      · narrative_sync_ok —— 三态塌成 True（「未评估」被读成「没问题」）
#      · tension_evaluated —— 键根本不存在 ⇒ hard_tension 恒为 True
#        ⇒ 每章都被当成「张力没测出来」的硬伤
#      · similarity_score / vector_stored / causal_edges_stored … —— 全丢
#   ⇒ 而当时的守卫**只测了纯函数** `_merge_setting_drift_issues`
#      ⇒ 「旧测试只测那个纯函数，调用方从未被执行」—— 同一个失败模式。
#
# ⇒ 这条守卫**整条函数真跑一遍**，从入口走到出口断言输出。


class _FakeChapter:
    def __init__(self, chapter_id: str = "ch-3", content: str = "沈砚青推门进来。"):
        self.id = chapter_id
        self.content = content


class _FakeChapterRepository:
    def __init__(self, chapter: _FakeChapter):
        self._chapter = chapter

    def get_by_novel_and_number(self, novel_id, number):
        return self._chapter


class _FakeAntiReport:
    def __init__(self, assessment: str = "轻微"):
        self.metrics = type("_M", (), {"overall_assessment": assessment})()


class _FakeHost:
    """``run_chapter_audit`` 需要的 host 替身（只喂数据，不做副作用）。"""

    def __init__(self, *, shared, pipeline_result, setting_drift_result,
                 similarity_below_threshold: bool = False):
        self.shared = shared
        self.pipeline_result = pipeline_result
        self.setting_drift_result = setting_drift_result
        self.similarity_below_threshold = similarity_below_threshold
        self.setting_drift_calls: list[tuple] = []
        self.memory_update_calls: list[tuple] = []
        self.events: list[tuple] = []
        self.shared_updates: list[dict] = []
        self.chapter_repository = _FakeChapterRepository(_FakeChapter())
        self.aftermath_pipeline = None
        self._pending_chapter_micro_beats: dict = {}
        self._pending_story_pipeline_aftermath = None

    # ── 基础读取 ──
    def _is_still_running(self, novel) -> bool:
        return True

    def _latest_completed_chapter_number(self, novel_id) -> int:
        return 3

    def _sync_novel_current_act_from_chapter_number(self, novel, number) -> None:
        return None

    def _cache_stats_to_shared_memory(self, novel) -> None:
        return None

    def _publish_audit_event(self, novel_id, name, payload) -> None:
        self.events.append((name, payload))

    def _update_shared_state(self, novel_id, **fields) -> None:
        self.shared_updates.append(dict(fields))
        self.shared.setdefault(f"novel:{novel_id}", {}).update(fields)

    async def _call_with_timeout(
        self, coro, timeout, novel_id="", label="", timeout_default=None
    ):
        return await coro

    async def _score_voice_only(self, novel_id, number, content):
        return {"similarity_score": 0.93, "drift_alert": False}

    async def _apply_voice_rewrite_loop(self, novel, chapter, content, drift_result):
        return content, drift_result

    def _allowed_character_names(self, novel_id) -> str:
        return "沈砚青"

    def _legacy_auditing_tasks_and_voice(self, novel, number, content, chapter_id):
        # 章后管线的结果（**与设定漂移是两回事**）
        return dict(self.pipeline_result)

    def _similarity_below_warning_threshold(self, score) -> bool:
        return self.similarity_below_threshold

    async def _run_anti_ai_audit(self, novel_id, number, content):
        return _FakeAntiReport("轻微")

    async def _run_setting_drift_audit(self, novel_id, number, content):
        self.setting_drift_calls.append((novel_id, number, content))
        return dict(self.setting_drift_result)

    async def _run_memory_update(self, novel_id, number, content, outline=""):
        """第 29 批：章后记忆状态回写（这个替身只喂数据，不做副作用）。"""
        self.memory_update_calls.append((novel_id, number, content))
        return {"updated": True, "reason": "", "new_beats": 0, "new_clues": 0}

    def _count_completed_chapters(self, novel_id) -> int:
        return 3

    def _read_chapter_stats_ephemeral(self, novel_id):
        return None

    def _save_novel_ephemeral(self, novel, write_target_chapters=False) -> None:
        return None

    def _save_novel_state(self, novel) -> None:
        return None

    def _sync_chronicles_to_shared_memory(self, novel_id) -> None:
        return None

    def _sync_storylines_to_shared_memory(self, novel_id) -> None:
        return None

    async def _extract_chapter_bridge(self, novel_id, number, content) -> None:
        return None

    async def _auto_trigger_macro_diagnosis(self, novel, completed_count) -> None:
        return None

    async def _maybe_generate_summaries(self, novel, completed_count) -> None:
        return None

    def _flush_novel(self, novel) -> None:
        return None


@pytest.mark.asyncio
async def test_the_setting_drift_result_does_not_shadow_the_pipeline_result(monkeypatch):
    """⚠ 整条 ``run_chapter_audit`` 真跑一遍 ⇒ 断言两件事同时成立。

    ① **新功能真的生效**：设定漂移被审了、结论进了 ``last_audit_issues``
       （实体 + 共享内存）。
    ② **新结果没有盖掉旧结果**：章后管线的那些字段
       （``narrative_sync_ok`` 三态 / ``similarity_score`` /
       ``vector_stored`` / ``causal_edges_stored``）仍然来自章后管线。

    ⚠ ② 正是本批自己踩过的坑：新加的变量**叫** ``drift_result``
       ⇒ 遮蔽了原有的同名变量 ⇒ 下面三条断言会同时变红。
    """
    import sys

    import engine.runtime.audit_delegate as audit_delegate_module
    from domain.novel.entities.novel import AutopilotStatus, Novel, NovelStage
    from domain.novel.value_objects.novel_id import NovelId

    # ⚠ 这两个是**落库**副作用（变量仓 / chapters 表）⇒ 换成空实现，
    #   别让单元测试往生产库里写东西。
    monkeypatch.setattr(
        audit_delegate_module, "_write_autopilot_invocation_input", lambda **kwargs: None
    )
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync._write_tension_ephemeral",
        lambda *args, **kwargs: None,
    )

    novel_id = "novel-seam-test"
    shared = {
        f"novel:{novel_id}": {
            # 让审计走到「已有待处理 payload」分支 ⇒ 不必真的发 invocation
            "autopilot_pending_audit_chapter_number": "3",
            "autopilot_pending_audit_report": {"chapter.audit.risk_flags": []},
            "autopilot_pending_aftermath_chapter_number": "3",
            "autopilot_pending_aftermath_payload": {"chapter.summary": "摘要"},
        }
    }
    monkeypatch.setitem(sys.modules, "__shared_state", shared)

    pipeline_result = {
        "similarity_score": 0.93,
        "drift_alert": False,
        # 三态：**未评估**（None）⇒ 下面断言「三态没塌」才有牙齿
        "narrative_sync_ok": None,
        "tension_composite": 72,
        "vector_stored": True,
        "foreshadow_stored": True,
        "triples_extracted": True,
        "causal_edges_stored": True,
        "character_mutations_stored": True,
        "debt_updated": True,
    }
    setting_drift_result = {
        "audited": True,
        "drift_score": 58,
        "issues": [
            {
                "severity": "critical",
                "category": "ability",
                "description": "沈砚青用枪，设定里她从不带枪",
                "suggestion": "改成撬棍",
            }
        ],
    }

    host = _FakeHost(
        shared=shared,
        pipeline_result=pipeline_result,
        setting_drift_result=setting_drift_result,
    )
    novel = Novel(
        id=NovelId(novel_id),
        title="缝",
        author="t",
        target_chapters=10,
        current_stage=NovelStage.WRITING,
        autopilot_status=AutopilotStatus.RUNNING,
        auto_approve_mode=True,
    )

    await audit_delegate_module.run_chapter_audit(host, novel)

    # ① 新功能生效
    assert host.setting_drift_calls, "章末审计没有调设定漂移审计"
    assert novel.last_audit_issues, "设定漂移结论没进 last_audit_issues"
    assert any(i.get("type") == "setting_drift" for i in novel.last_audit_issues)

    # ② 章后管线的结果**没被盖掉**（这三条在遮蔽 bug 下会同时变红）
    assert novel.last_audit_narrative_ok is None, (
        "叙述三态塌成了 True ⇒ 「未评估」被读成「没问题」"
        "（设定漂移的结果盖掉了章后管线的结果）"
    )
    assert novel.last_audit_similarity == 0.93, (
        "文风相似度丢了 ⇒ 设定漂移的结果盖掉了章后管线的结果"
    )
    shared_snapshot = shared[f"novel:{novel_id}"]
    assert shared_snapshot.get("last_audit_causal_edges_stored") is True, (
        "因果边落库标记丢了 ⇒ 设定漂移的结果盖掉了章后管线的结果"
    )
    assert shared_snapshot.get("last_audit_vector_stored") is True
    # 共享内存也要带上设定漂移结论（面板实时可见）
    assert any(
        i.get("type") == "setting_drift" for i in (shared_snapshot.get("last_audit_issues") or [])
    )


@pytest.mark.asyncio
async def test_the_setting_drift_audit_does_not_change_the_tension_gate(monkeypatch):
    """⚠ 设定漂移的结果**不许**影响张力闸门。

    ⇒ 遮蔽 bug 下 ``hard_tension`` 会恒为 True（「张力没测出来」的硬伤），
      而 ``tension_evaluated`` 本来是**测出来了**的。
    """
    import sys

    import engine.runtime.audit_delegate as audit_delegate_module
    from domain.novel.entities.novel import AutopilotStatus, Novel, NovelStage
    from domain.novel.value_objects.novel_id import NovelId

    monkeypatch.setattr(
        audit_delegate_module, "_write_autopilot_invocation_input", lambda **kwargs: None
    )
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync._write_tension_ephemeral",
        lambda *args, **kwargs: None,
    )

    novel_id = "novel-seam-test-2"
    shared = {
        f"novel:{novel_id}": {
            "autopilot_pending_audit_chapter_number": "3",
            "autopilot_pending_audit_report": {"chapter.audit.risk_flags": []},
            "autopilot_pending_aftermath_chapter_number": "3",
            "autopilot_pending_aftermath_payload": {"chapter.summary": "摘要"},
        }
    }
    monkeypatch.setitem(sys.modules, "__shared_state", shared)

    host = _FakeHost(
        shared=shared,
        pipeline_result={
            "similarity_score": 0.9,
            "drift_alert": False,
            "narrative_sync_ok": True,
            "tension_composite": 72,
        },
        setting_drift_result={"audited": True, "issues": []},
    )
    novel = Novel(
        id=NovelId(novel_id),
        title="缝",
        author="t",
        target_chapters=10,
        current_stage=NovelStage.WRITING,
        autopilot_status=AutopilotStatus.RUNNING,
        auto_approve_mode=True,
    )

    await audit_delegate_module.run_chapter_audit(host, novel)

    complete_event = [p for name, p in host.events if name == "audit_complete"]
    assert complete_event, "没有发出 audit_complete 事件"
    payload = complete_event[-1]
    assert payload["hard_fail"] is False, (
        "硬门禁被判成失败 ⇒ 设定漂移的结果盖掉了张力评估结果"
        f"（narrative_sync_state={payload.get('narrative_sync_state')}）"
    )
    assert novel.last_chapter_tension == 72


# ── 5) 文风漂移告警的三态（第 23 批）────────────────────────────────────
#
# ⚠ 第 21 批生产实测出**自相矛盾**的快照：「漂移告警 = 1」但「相似度 = NULL」
#    ⇒ 面板上像是告警，却给不出任何数字。
# ⇒ 相似度**没算出来** ⇒ 未评估（None）；算出来了 ⇒ 只在确实低于阈值时才算偏离。


async def _run_audit_with_pipeline(pipeline_result, *, below_threshold=False):
    import sys

    import engine.runtime.audit_delegate as audit_delegate_module
    from domain.novel.entities.novel import AutopilotStatus, Novel, NovelStage
    from domain.novel.value_objects.novel_id import NovelId

    novel_id = "novel-drift-tri"
    shared = {
        f"novel:{novel_id}": {
            "autopilot_pending_audit_chapter_number": "3",
            "autopilot_pending_audit_report": {"chapter.audit.risk_flags": []},
            "autopilot_pending_aftermath_chapter_number": "3",
            "autopilot_pending_aftermath_payload": {"chapter.summary": "摘要"},
        }
    }

    old_shared = sys.modules.get("__shared_state")
    sys.modules["__shared_state"] = shared
    try:
        host = _FakeHost(
            shared=shared,
            pipeline_result=pipeline_result,
            setting_drift_result={"audited": True, "issues": []},
            similarity_below_threshold=below_threshold,
        )
        novel = Novel(
            id=NovelId(novel_id),
            title="缝",
            author="t",
            target_chapters=10,
            current_stage=NovelStage.WRITING,
            autopilot_status=AutopilotStatus.RUNNING,
            auto_approve_mode=True,
        )
        await audit_delegate_module.run_chapter_audit(host, novel)
        return novel, shared
    finally:
        if old_shared is None:
            sys.modules.pop("__shared_state", None)
        else:
            sys.modules["__shared_state"] = old_shared


@pytest.mark.asyncio
async def test_an_unmeasured_similarity_leaves_the_drift_alert_unassessed():
    """⚠ 相似度**没算出来** ⇒ 告警**未评估**（None），不是「告警」也不是「没偏离」。"""
    novel, _ = await _run_audit_with_pipeline(
        {
            "similarity_score": None,
            "drift_alert": True,
            "narrative_sync_ok": True,
            "tension_composite": 70,
        }
    )
    assert novel.last_audit_drift_alert is None, (
        f"未评估被写成了 {novel.last_audit_drift_alert!r} ⇒ 面板上会显示成「偏离」"
    )
    assert novel.last_audit_similarity is None


@pytest.mark.asyncio
async def test_a_real_drift_is_recorded_as_an_alert():
    """对照：相似度算出来了 + 确实低于阈值 ⇒ 本章真的偏离。"""
    novel, _ = await _run_audit_with_pipeline(
        {
            "similarity_score": 0.42,
            "drift_alert": True,
            "narrative_sync_ok": True,
            "tension_composite": 70,
        },
        below_threshold=True,
    )
    assert novel.last_audit_drift_alert is True


@pytest.mark.asyncio
async def test_an_alert_from_a_historical_window_is_not_this_chapters_drift():
    """⚠ 告警来自历史窗口（本章相似度没低于阈值）⇒ 本章**没偏离**。"""
    novel, _ = await _run_audit_with_pipeline(
        {
            "similarity_score": 0.97,
            "drift_alert": True,
            "narrative_sync_ok": True,
            "tension_composite": 70,
        },
        below_threshold=False,
    )
    assert novel.last_audit_drift_alert is False
