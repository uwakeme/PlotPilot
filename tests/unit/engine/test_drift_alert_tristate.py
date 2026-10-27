"""第 23 批：文风漂移告警的**三态**（True=偏离 / False=没偏离 / None=未评估）。

背景
----
第 21 批生产实测出**自相矛盾**的快照：「漂移告警 = 1」但「相似度 = NULL」
⇒ 面板上像是告警，却给不出任何数字。

根因有两层：
  ① 告警**可能来自历史窗口**（本章相似度并未低于阈值）——
     代码自己也知道（日志写「文风告警来自历史窗口，当前章节相似度未低于阈值」）；
  ② 「相似度**没算出来**」被当成了「没低于阈值」（``_similarity_below_warning_threshold(None)``
     返回 False）。

⇒ 现在：相似度**没算出来** ⇒ 未评估（None）；算出来了 ⇒ 只在**确实**低于阈值时才算偏离。

守卫清单
--------
- 三态取值（纯函数）
- 落库往返：None ⇒ **原样 NULL** ⇒ 读回来还是 None（不是 False）
- 共享内存：None ⇒ 原样 None
- API：三态**透传**（不许被 ``bool()`` 压平）
"""
from __future__ import annotations

import pytest

from engine.runtime.audit_delegate import _drift_alert_shared_value


# ── 1) 三态取值（纯函数）────────────────────────────────────────────────


def test_an_unmeasured_similarity_is_undecided_not_false():
    """⚠ 相似度**没算出来** ⇒ 未评估（None）—— 不是「没偏离」。"""
    assert _drift_alert_shared_value({"drift_alert": True}, False) is None
    assert _drift_alert_shared_value({"drift_alert": False}, False) is None
    assert _drift_alert_shared_value({}, False) is None


def test_a_real_drift_is_reported():
    """相似度算出来了 + 低于阈值 + 有告警 ⇒ 本章真的偏离。"""
    assert _drift_alert_shared_value(
        {"drift_alert": True, "similarity_score": 0.42}, True
    ) is True


def test_no_alert_means_no_drift():
    assert _drift_alert_shared_value(
        {"drift_alert": False, "similarity_score": 0.42}, True
    ) is False


def test_an_alert_from_a_historical_window_is_not_this_chapters_drift():
    """⚠ 有告警但本章相似度**没低于阈值** ⇒ 本章没偏离（告警来自历史窗口）。"""
    assert _drift_alert_shared_value(
        {"drift_alert": True, "similarity_score": 0.97}, False
    ) is False


# ── 2) 落库往返：None 必须原样存 NULL ──────────────────────────────────


def test_the_undecided_value_survives_the_repository_round_trip(tmp_path):
    """⚠ None 落库 ⇒ 读回来还是 None（压成 False 就与「没偏离」同形了）。"""
    from infrastructure.persistence.database.sqlite_novel_repository import (
        SqliteNovelRepository,
    )
    from infrastructure.persistence.database.connection import DatabaseConnection
    from domain.novel.entities.novel import Novel, NovelStage
    from domain.novel.value_objects.novel_id import NovelId

    db = DatabaseConnection(str(tmp_path / "novels.db"))
    try:
        repo = SqliteNovelRepository(db)
        novel = Novel(
            id=NovelId("n-tristate"),
            title="三态",
            author="t",
            target_chapters=4,
            current_stage=NovelStage.WRITING,
            last_audit_chapter_number=3,
            last_audit_similarity=None,
            last_audit_drift_alert=None,
        )
        repo.save(novel)

        loaded = repo.get_by_id(NovelId("n-tristate"))
        assert loaded is not None
        assert loaded.last_audit_drift_alert is None, (
            "未评估被压成了 False ⇒ 与「本章没偏离」同形了"
        )
    finally:
        db.close_all()


def test_a_real_drift_survives_the_repository_round_trip(tmp_path):
    """对照：True / False 必须**如实**往返。"""
    from infrastructure.persistence.database.sqlite_novel_repository import (
        SqliteNovelRepository,
    )
    from infrastructure.persistence.database.connection import DatabaseConnection
    from domain.novel.entities.novel import Novel, NovelStage
    from domain.novel.value_objects.novel_id import NovelId

    db = DatabaseConnection(str(tmp_path / "novels2.db"))
    try:
        repo = SqliteNovelRepository(db)
        for expected in (True, False):
            novel = Novel(
                id=NovelId(f"n-{expected}"),
                title="三态",
                author="t",
                target_chapters=4,
                current_stage=NovelStage.WRITING,
                last_audit_chapter_number=3,
                last_audit_similarity=0.5,
                last_audit_drift_alert=expected,
            )
            repo.save(novel)
            loaded = repo.get_by_id(NovelId(f"n-{expected}"))
            assert loaded is not None
            assert loaded.last_audit_drift_alert is expected
    finally:
        db.close_all()


# ── 3) 落盘字段（共享内存 / patch 都走它）：三态原样透出 ───────────────


def test_the_save_fields_keep_the_undecided_value():
    """⚠ 落盘字段必须原样带 None（压成 0 ⇒ 面板上会显示成「没偏离」）。"""
    from engine.runtime.daemon_host import _build_novel_save_fields
    from domain.novel.entities.novel import Novel, NovelStage
    from domain.novel.value_objects.novel_id import NovelId

    novel = Novel(
        id=NovelId("n-tri"),
        title="t",
        author="a",
        target_chapters=4,
        current_stage=NovelStage.WRITING,
    )
    novel.last_audit_chapter_number = 3
    novel.last_audit_drift_alert = None

    fields = _build_novel_save_fields(novel)
    assert fields["last_audit_drift_alert"] is None, (
        "未评估被压成 0 ⇒ 与「本章没偏离」同形了"
    )


# ── 4) API：三态透传（不许被 bool() 压平）──────────────────────────────


def test_the_api_passes_the_three_states_through():
    """⚠ API 以前一律 ``bool(...)`` ⇒ 「未评估」被压成 False。"""
    from interfaces.api.v1.engine.autopilot_routes import _drift_alert_tri_state

    assert _drift_alert_tri_state(True, 0.5) is True
    assert _drift_alert_tri_state(False, 0.5) is False
    assert _drift_alert_tri_state(1, 0.5) is True
    assert _drift_alert_tri_state(0, 0.5) is False
    assert _drift_alert_tri_state(None, 0.5) is None
    assert _drift_alert_tri_state(None, None) is None


def test_the_api_does_not_report_an_alert_without_a_similarity_score():
    """⚠ 相似度**没有** ⇒ 一律「未评估」。

    ⇒ 顺带把历史遗留的「告警=1 + 分数为空」的老行纠正过来
    （第 21 批生产实测出来的自相矛盾状态）。
    """
    from interfaces.api.v1.engine.autopilot_routes import _drift_alert_tri_state

    assert _drift_alert_tri_state(True, None) is None, (
        "没有相似度却报「告警」⇒ 面板上像是告警，却给不出任何数字"
    )
    assert _drift_alert_tri_state(1, None) is None
