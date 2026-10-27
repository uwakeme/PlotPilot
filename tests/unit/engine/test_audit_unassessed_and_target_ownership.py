"""H + I：章后管线的「未评估 ≠ 失败」与目标章数的归属权（第 18 批）。

H —— 超时降级被当成「叙事失败」
--------------------------------
章后管线（``aftermath_pipeline``）在慢 provider 下会 **300s 超时**
⇒ ``_call_with_timeout`` 返回**显式默认值**。
此前那个默认值里写着 ``narrative_sync_ok: False`` ⇒
``hard_narrative = not bool(...)`` ⇒ **True** ⇒
**「没跑出来」被记成「测出严重」**（同章还会在日志/面板上写成「叙事未同步」）。

⚠ 这与同一个函数下面 anti-AI 侧**刚统一过**的口径分裂
（那里明写：「因『审计未评估』而停时，日志不会写成『Anti-AI严重』——那等于把『没测出来』
记成『测出严重』」）⇒ 两处对「没测出来」的处理不该分裂。

⇒ 现在：``narrative_sync_ok`` 是**三态**
（``True``=已同步 / ``False``=明确失败 / ``None``=**未评估**），
「未评估」与「失败」在**判定**、**日志**、**共享内存**、**面板**上都能分辨。

I —— 目标章数被陈旧副本覆盖
--------------------------
``target_chapters`` 是**规划侧**的字段，但它被三处在途流程写回：
① ``POST /autopilot/{id}/start`` 用**共享内存**里的旧值（快照！）⇒ 把用户刚改的值撤销；
② 审计收尾的**整体 save** 把实体里的**陈旧值**写回；
③ ``daemon_host._flush_novel`` ✅ **不**写它（增量 patch）—— 这条本来就是对的。

⇒ 现在：start 路由的权威来源是 **DB**（请求体显式传的值优先级最高），
审计只在**刻意上调**目标（终局预算 / 软目标续写）时才写它。
"""
from __future__ import annotations

import inspect

from engine.runtime.audit_delegate import (
    AFTERMATH_TIMEOUT_DEFAULT,
    _narrative_shared_value,
    _resolve_narrative_gate,
    run_chapter_audit,
)
from engine.runtime.daemon_host import (
    _build_novel_save_fields,
    _should_write_target_chapters,
)


# ── H：未评估 ≠ 失败 ────────────────────────────────────────────────────


def test_narrative_gate_distinguishes_ok_failed_and_unassessed():
    """三态必须可分辨：ok / failed / unassessed。"""
    hard, evaluated, state = _resolve_narrative_gate({"narrative_sync_ok": True})
    assert (hard, evaluated, state) == (False, True, "ok")

    hard, evaluated, state = _resolve_narrative_gate({"narrative_sync_ok": False})
    assert (hard, evaluated, state) == (True, True, "failed")

    hard, evaluated, state = _resolve_narrative_gate({"narrative_sync_ok": None})
    assert (hard, evaluated, state) == (True, False, "unassessed"), (
        "「未评估」必须可分辨 ⇒ 既不是「没问题」，也不是「失败」"
    )


def test_narrative_gate_treats_a_missing_key_as_ok():
    """缺键 = 评估过且没问题（保持旧口径的默认值语义）。"""
    hard, evaluated, state = _resolve_narrative_gate({})
    assert (hard, evaluated, state) == (False, True, "ok")


def test_the_aftermath_timeout_default_is_not_a_failure():
    """超时降级 ⇒ **未评估**（None），不是「失败」（False）。

    ⚠ 这条是 H 的核心：写成 False 就是把「没测出来」记成「测出严重」。
    """
    assert "narrative_sync_ok" in AFTERMATH_TIMEOUT_DEFAULT
    assert AFTERMATH_TIMEOUT_DEFAULT["narrative_sync_ok"] is None, (
        f"超时默认值把未评估写成了 {AFTERMATH_TIMEOUT_DEFAULT['narrative_sync_ok']!r}"
    )


def test_the_shared_value_is_none_when_unassessed():
    """共享内存承载三态 ⇒ 面板能分辨「没跑出来」与「跑出来失败」。"""
    assert _narrative_shared_value({"narrative_sync_ok": None}) is None
    assert _narrative_shared_value({"narrative_sync_ok": False}) is False
    assert _narrative_shared_value({"narrative_sync_ok": True}) is True


def test_the_audit_uses_the_shared_timeout_default():
    """接线：审计的章后管线超时必须用那个常量（不能自己再抄一份字面量）。

    ⚠ 抄一份字面量就会漂 —— 这正是「同一逻辑写两份 ⇒ 合并」那条纪律。
    """
    source = inspect.getsource(run_chapter_audit)
    assert "AFTERMATH_TIMEOUT_DEFAULT" in source, (
        "审计没有用共享的超时默认值 ⇒ 有人又抄了一份（改一处漂一处）"
    )
    assert '"narrative_sync_ok": False' not in source, (
        "审计里内联了 narrative_sync_ok=False ⇒ 超时又被当成「失败」了"
    )


# ── I：目标章数的归属权 ─────────────────────────────────────────────────


class _FakeNovel:
    """够用的假实体（只被字段构建函数读）。"""

    def __init__(self, target_chapters: int = 4):
        from domain.novel.entities.novel import NovelStage
        from domain.novel.value_objects.novel_id import NovelId

        self.novel_id = NovelId("novel-test")
        self.autopilot_status = type("_S", (), {"value": "running"})()
        self.current_stage = NovelStage.WRITING
        self.current_act = 1
        self.current_chapter_in_act = 1
        self.current_beat_index = 0
        self.current_auto_chapters = 3
        self.target_chapters = target_chapters
        self.target_words_per_chapter = 2500
        self.consecutive_error_count = 0
        self.last_chapter_tension = None
        self.beats_completed = 0
        self.auto_approve_mode = True
        self.last_audit_chapter_number = 3
        self.last_audit_similarity = None
        self.last_audit_drift_alert = False
        self.last_audit_narrative_ok = True
        self.last_audit_at = None
        self.last_audit_vector_stored = False
        self.last_audit_foreshadow_stored = False
        self.last_audit_triples_extracted = False
        self.last_audit_quality_scores = None
        self.last_audit_issues = None
        self.audit_progress = None


def test_the_save_fields_omit_target_chapters_when_not_writing_it():
    """审计**没刻意改**目标时，落盘不许写这个字段（否则撤销别人的改动）。"""
    fields = _build_novel_save_fields(_FakeNovel(4), write_target_chapters=False)
    assert "target_chapters" not in fields, (
        "落盘还在写 target_chapters ⇒ 会把陈旧值写回去（生产实测：PUT 4 → 审计收尾 → 变回 3）"
    )


def test_the_save_fields_include_target_chapters_when_writing_it():
    """审计**刻意上调**目标时（终局预算 / 软目标续写）必须写下去。"""
    fields = _build_novel_save_fields(_FakeNovel(9), write_target_chapters=True)
    assert fields["target_chapters"] == 9


def test_the_save_fields_keep_every_other_field_identical():
    """只该少一个字段 ⇒ 其余字段（张力、审计快照等）一个都不能少。"""
    with_tc = _build_novel_save_fields(_FakeNovel(4), write_target_chapters=True)
    without_tc = _build_novel_save_fields(_FakeNovel(4), write_target_chapters=False)
    assert set(with_tc) - set(without_tc) == {"target_chapters"}
    assert set(without_tc) == set(with_tc) - {"target_chapters"}
    for key in without_tc:
        assert without_tc[key] == with_tc[key], f"字段 {key} 在两种模式下不一致"


def test_target_chapters_is_written_only_when_the_audit_changed_it():
    """判定：只有实体里的目标与本轮开始时不同 ⇒ 审计**刻意**改过 ⇒ 才写。"""
    assert _should_write_target_chapters(9, 4) is True
    assert _should_write_target_chapters(4, 4) is False


def test_the_start_route_resolves_target_chapters_from_the_db():
    """start 路由：请求体优先，其次 **DB**。"""
    from interfaces.api.v1.engine.autopilot_routes import _resolve_start_target_chapters

    assert _resolve_start_target_chapters(body_target=12, db_target=4) == 12
    assert _resolve_start_target_chapters(body_target=None, db_target=4) == 4
    assert _resolve_start_target_chapters(body_target=None, db_target=0) == 1


def test_the_start_route_does_not_read_target_chapters_from_shared_memory():
    """接线：start 路由**不许**从共享内存取目标章数（那是快照）。

    ⚠ 共享内存里的目标章数是**旧值** ⇒ 用了它就会把用户刚改的值回写 DB。
    ⇒ 用「按函数边界切片」查，而不是全文 contains（避免命中别的函数）。
    """
    from interfaces.api.v1.engine import autopilot_routes

    source = inspect.getsource(autopilot_routes.start_autopilot)
    assert 'shared.get("target_chapters"' not in source, (
        "start 路由又从共享内存取目标章数 ⇒ 会用旧值覆盖用户刚改的值"
    )
    assert "_resolve_start_target_chapters" in source, (
        "start 路由没有走统一的目标章数解析 ⇒ 口径会分裂"
    )
