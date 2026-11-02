# -*- coding: utf-8 -*-
"""第 82 批（N-04 / N-05）：文风漂移的**窗口**与**「没评过」的存储语义**。

两条缺陷（第 80 批独立验收）
----------------------------
**N-04 窗口取错**：
``_check_drift_alert`` 用 ``list_by_novel(limit=...)``，而那个方法是
``ORDER BY chapter_number ASC LIMIT ?`` ⇒ 拿到的是**最早**的 N 章
⇒ 「最近 N 章连续低分」的告警，超过 N 章的书**永远反映第 1~N 章**。

**N-05 「没评过」被压成 0.0**：
``chapter_style_scores.similarity_score`` 是 ``NOT NULL DEFAULT 0.0``
⇒ 没有作者风格基准时，「**没评过**」被存成 0.0 ⇒ 与「真·相似度 0.0」**同形**
⇒ 下游那条 ``similarity_score IS NOT NULL`` 的过滤**恒真**
⇒ 无基准的章节被当成「相似度 0.0（远低于阈值 0.75）」
⇒ **每本书的漂移告警都常亮**（生产实测三本书全亮）。

修法：列改**可空**（存 None）+ 告警窗口改**最近 N 章**。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "infrastructure" / "persistence" / "database" / "schema.sql"

sys.path.insert(0, str(ROOT))


@pytest.fixture()
def db(tmp_path):
    """⚠ 用**临时文件**库（``:memory:`` 的初始化连接会被 close ⇒ 内存库随之消失）。"""
    from infrastructure.persistence.database.connection import DatabaseConnection

    database = DatabaseConnection(str(tmp_path / "drift.db"))
    # ⚠ chapter_style_scores.novel_id 有外键 ⇒ 先有书
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES ('n1', 'N', 'n1', 10)"
    )
    database.get_connection().commit()
    yield database
    database.close()


@pytest.fixture()
def score_repo(db):
    from infrastructure.persistence.database.sqlite_chapter_style_score_repository import (
        SqliteChapterStyleScoreRepository,
    )

    return SqliteChapterStyleScoreRepository(db)


class _NoFingerprint:
    """没有作者风格基准（生产实况：voice_fingerprint 0 行）。"""

    def get_by_novel(self, _novel_id, _pov_character_id=None):
        return None


def _service(score_repo):
    from application.analyst.services.voice_drift_service import VoiceDriftService

    return VoiceDriftService(score_repo=score_repo, fingerprint_repo=_NoFingerprint())


def _seed(score_repo, novel_id: str, scores: dict[int, float | None]) -> None:
    for chapter_number, similarity in scores.items():
        score_repo.upsert(
            novel_id=novel_id,
            chapter_number=chapter_number,
            adjective_density=0.3,
            avg_sentence_length=20.0,
            sentence_count=10,
            similarity_score=similarity,
        )


# ── N-04：窗口必须是「最近 N 章」 ───────────────────────────────────────


def test_the_recent_window_returns_the_latest_rows_in_ascending_order(score_repo):
    """⚠ 取**最近**的 N 章，且**正序**返回（调用方取尾部 ⇒ 拿到最新那几章）。"""
    _seed(score_repo, "n1", {i: 0.9 for i in range(1, 21)})

    rows = score_repo.list_recent_by_novel("n1", limit=5)
    assert [r["chapter_number"] for r in rows] == [16, 17, 18, 19, 20], (
        "取到的不是最近 5 章（或顺序反了）"
    )


def test_the_alert_fires_when_only_the_latest_chapters_drifted(score_repo):
    """⚠ **最近** 5 章连续低分 ⇒ 必须告警。

    ⚠ 旧实现看的是最早 5 章（这里是高分）⇒ **不告警** ⇒ 漏报。
    """
    scores = {i: 0.9 for i in range(1, 16)}      # 早 15 章都正常
    scores.update({i: 0.2 for i in range(16, 21)})  # 最近 5 章漂了
    _seed(score_repo, "n1", scores)

    assert _service(score_repo)._check_drift_alert("n1") is True, (
        "最近 5 章连续低分却没告警 ⇒ 窗口取的**不是最近**的章"
    )


def test_the_alert_stays_quiet_when_only_the_oldest_chapters_drifted(score_repo):
    """⚠ 最早 5 章漂了、最近 5 章正常 ⇒ **不该**告警。

    ⚠ 旧实现看的正是最早那批 ⇒ **会**误报。
    """
    scores = {i: 0.2 for i in range(1, 6)}       # 最早 5 章漂了
    scores.update({i: 0.9 for i in range(6, 21)})
    _seed(score_repo, "n1", scores)

    assert _service(score_repo)._check_drift_alert("n1") is False, (
        "只有最早几章漂移却告警 ⇒ 告警窗口取错了章"
    )


def test_the_alert_window_is_not_the_earliest_chapters(score_repo):
    """⚠ 结构性钉住：告警**不许**用 ``list_by_novel``（那是「最早 N 章」）。"""
    src = (
        ROOT / "application" / "analyst" / "services" / "voice_drift_service.py"
    ).read_text(encoding="utf-8-sig")
    body = src.split("def _check_drift_alert", 1)[1].split("def ", 1)[0]
    assert "list_recent_by_novel" in body, "告警窗口没用「最近 N 章」的方法"
    assert "list_by_novel(" not in body, "告警窗口仍在用「最早 N 章」的方法"


# ── N-05：「没评过」在存储层必须可分辨 ───────────────────────────────────


def test_the_schema_allows_a_null_similarity():
    """⚠ 列必须**可空**（NULL = 没评过）。

    ⚠ 写成 ``NOT NULL DEFAULT 0.0`` ⇒ 「没评过」被压成 0.0 ⇒ 与真 0.0 同形。
    """
    text = SCHEMA.read_text(encoding="utf-8-sig")
    assert "similarity_score REAL NOT NULL" not in text, (
        "similarity_score 还是 NOT NULL ⇒ 存不下「没评过」"
    )
    assert "similarity_score REAL DEFAULT 0.0" in text, "列定义不见了"


def test_the_column_is_nullable_in_a_real_database(db):
    """⚠ 真库（跑完初始化补丁）里这一列必须可空。"""
    row = db.get_connection().execute(
        "PRAGMA table_info(chapter_style_scores)"
    ).fetchall()
    info = {r[1]: r for r in row}
    assert "similarity_score" in info, "表都没有"
    assert not info["similarity_score"][3], "列还是 NOT NULL ⇒ 补丁没生效"


def test_a_chapter_without_a_baseline_is_stored_as_none(score_repo, db):
    """⚠ 没有基准 ⇒ 存 **None**（不是 0.0）。

    ⚠ 存 0.0 ⇒ 下游那条 ``IS NOT NULL`` 过滤恒真 ⇒ 假告警。
    """
    service = _service(score_repo)
    result = service.score_chapter("n1", 1, "这是一段中文正文，用来统计词频与句长。")

    assert result["similarity_score"] is None, "没有基准却算出了相似度"
    row = db.get_connection().execute(
        "SELECT similarity_score FROM chapter_style_scores WHERE novel_id = ? AND chapter_number = ?",
        ("n1", 1),
    ).fetchone()
    assert row is not None, "没落库"
    assert row[0] is None, (
        "库里存的是 %r 而不是 NULL ⇒「没评过」被压成了假值" % (row[0],)
    )


def test_no_baseline_means_no_alert(score_repo):
    """⚠ 全是「没评过」⇒ **不许**告警（没有结论就没有告警）。"""
    _seed(score_repo, "n1", {i: None for i in range(1, 21)})

    assert _service(score_repo)._check_drift_alert("n1") is False, (
        "一章都没评过却报漂移告警 ⇒ 「没评过」被当成了「相似度 0」"
    )


def test_a_null_row_is_not_counted_as_a_low_score(score_repo):
    """⚠ NULL 行**不进**告警统计（这是修复前那条恒真过滤的意义）。"""
    # 最近 5 章里有 2 章是 NULL、3 章真低分 ⇒ 有效章数不足 ⇒ 不告警
    scores = {i: 0.9 for i in range(1, 16)}
    scores.update({16: 0.2, 17: None, 18: 0.2, 19: None, 20: 0.2})
    _seed(score_repo, "n1", scores)

    assert _service(score_repo)._check_drift_alert("n1") is False, (
        "未评估(NULL)的章节被算进了「连续低分」"
    )
