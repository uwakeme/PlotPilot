"""第 67 批守卫：B-5 —— 反 AI 评分的**口径版本**必须可辨、且历史数据必须换算。

背景（独立审查报告 ``review-briefs/2026-10-30-review-report-85c0f726-closeout.md`` B-5）
------------------------------------------------------------------------------
第 16 批把归一化从「每万字」改成「每千字」（阈值 5.0/3.0/1.0 → 4.0/2.0/0.8），
但 ``anti_ai_audits`` 的历史行**没重算、也没记口径版本**
⇒ 生产库 591 行跨刻度混排（同一本书里既有旧刻度又有新刻度）
⇒ 趋势序列读起来像「AI 味暴跌 93%」，纯属刻度切换的假象。

本文件钉住
----------
1. 迁移 SQL 真的能把**旧刻度**的行换算到当前刻度（**行为级**：建库 → 塞样本 → 跑迁移 → 断言）；
2. **当前刻度**的行换算后**零漂移**（不许被顺手改掉）；
3. 迁移 SQL 里的权重/阈值与 Python **单一事实源**一致（不许两份漂）；
4. 写侧每次都打上当前版本；读侧把版本**带出来**（消费方才有办法判断可比性）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
MIGRATIONS = ROOT / "infrastructure" / "persistence" / "database" / "migrations"

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS anti_ai_audits (
    audit_id        TEXT PRIMARY KEY,
    novel_id        TEXT NOT NULL,
    chapter_number  INTEGER NOT NULL,
    total_hits      INTEGER NOT NULL DEFAULT 0,
    critical_hits   INTEGER NOT NULL DEFAULT 0,
    warning_hits    INTEGER NOT NULL DEFAULT 0,
    info_hits       INTEGER NOT NULL DEFAULT 0,
    severity_score  REAL NOT NULL DEFAULT 0.0,
    overall_assessment TEXT NOT NULL DEFAULT '未知',
    hit_density     REAL NOT NULL DEFAULT 0.0,
    critical_density REAL NOT NULL DEFAULT 0.0,
    category_distribution TEXT,
    top_patterns    TEXT,
    recommendations TEXT,
    improvement_suggestions TEXT,
    hits_detail     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(novel_id, chapter_number)
);
"""


def _apply_migration(conn: sqlite3.Connection, name: str) -> None:
    """按**生产迁移器**的方式应用迁移（逐语句 + 良性错误跳过）。

    ⚠ 为什么要复用迁移器的拆句与良性错误名单：
      生产里 ``ALTER TABLE ... ADD COLUMN`` 重复执行会报 "duplicate column"
      ⇒ 迁移器把它当**良性**错误跳过、继续跑后面的语句
      ⇒ 用 ``executescript`` 会在第一条就整体中止 ⇒ 测不出真实行为。
    """
    from infrastructure.persistence.database.migration_runner import (
        _BENIGN_ERROR_MARKERS,
        _split_sql_statements,
    )

    sql = (MIGRATIONS / name).read_text(encoding="utf-8")
    for statement in _split_sql_statements(sql):
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if any(marker in str(exc) for marker in _BENIGN_ERROR_MARKERS):
                continue
            raise
    conn.commit()


def _seed_row(
    conn: sqlite3.Connection,
    audit_id: str,
    *,
    total_hits: int,
    critical: int,
    warning: int,
    info: int,
    severity_score: float,
    assessment: str,
    hit_density: float,
) -> None:
    conn.execute(
        """
        INSERT INTO anti_ai_audits
        (audit_id, novel_id, chapter_number, total_hits, critical_hits, warning_hits,
         info_hits, severity_score, overall_assessment, hit_density, critical_density)
        VALUES (?, 'n-1', 1, ?, ?, ?, ?, ?, ?, ?, 0.0)
        """,
        (audit_id, total_hits, critical, warning, info, severity_score, assessment, hit_density),
    )
    conn.commit()


def _row(conn: sqlite3.Connection, audit_id: str) -> dict:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT severity_score, overall_assessment, score_scale_version "
        "FROM anti_ai_audits WHERE audit_id = ?",
        (audit_id,),
    )
    return dict(cur.fetchone())


# ─────────────────────── 1) 迁移真的能换算（行为级） ───────────────────────


def test_the_migration_converts_the_old_scale_rows():
    """⚠ 旧刻度的行必须被换算成当前刻度（**精确**的 10 倍关系 ⇒ 直接除以 10）。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(BASE_SCHEMA)
    # 生产库真实样本（prodrealtest-001 第 1 章）：raw=9.0，字数≈2031.6 ⇒ 旧刻度 44.3
    _seed_row(
        conn,
        "old-scale",
        total_hits=9,
        critical=0,
        warning=9,
        info=0,
        severity_score=44.3,
        assessment="严重",
        hit_density=4.43,
    )

    _apply_migration(conn, "add_anti_ai_score_scale_version.sql")

    row = _row(conn, "old-scale")
    assert row["severity_score"] == 4.4, (
        f"旧刻度行没有换算（44.3 ⇒ 应为 4.4），实际 {row['severity_score']}"
    )
    assert row["overall_assessment"] == "严重", row
    assert row["score_scale_version"] == 2, row


def test_the_migration_keeps_the_current_scale_rows_unchanged():
    """⚠ **当前刻度**的行换算后必须**零漂移**（不许被顺手改掉）。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(BASE_SCHEMA)
    # 生产库真实样本（prodrealtest-001 第 3 章）：raw=5.2，当前刻度 3.1
    _seed_row(
        conn,
        "new-scale",
        total_hits=8,
        critical=0,
        warning=4,
        info=4,
        severity_score=3.1,
        assessment="中等",
        hit_density=4.83,
    )

    _apply_migration(conn, "add_anti_ai_score_scale_version.sql")

    row = _row(conn, "new-scale")
    assert row["severity_score"] == 3.1, f"当前刻度的行被改动了：{row}"
    assert row["overall_assessment"] == "中等", row
    assert row["score_scale_version"] == 2, row


def test_the_migration_converts_the_capped_rows():
    """⚠ 封顶行（100）判不出当初那套刻度 ⇒ 用 raw + hit_density **重算**当前刻度真值。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(BASE_SCHEMA)
    # raw=26.5，字数=2000 ⇒ 旧刻度 132.5（封顶 100），当前刻度 13.25
    _seed_row(
        conn,
        "capped",
        total_hits=20,
        critical=5,
        warning=10,
        info=5,
        severity_score=100.0,
        assessment="严重",
        hit_density=10.0,
    )

    _apply_migration(conn, "add_anti_ai_score_scale_version.sql")

    row = _row(conn, "capped")
    assert row["severity_score"] == 13.3, (
        f"封顶行没有按当前刻度重算（应为 13.3），实际 {row['severity_score']}"
    )
    assert row["score_scale_version"] == 2, row


def test_the_migration_marks_the_zero_rows_as_the_current_scale():
    """⚠ 零分行与刻度无关（raw=0）⇒ 直接标成当前刻度。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(BASE_SCHEMA)
    _seed_row(
        conn,
        "zero",
        total_hits=0,
        critical=0,
        warning=0,
        info=0,
        severity_score=0.0,
        assessment="纯净",
        hit_density=0.0,
    )

    _apply_migration(conn, "add_anti_ai_score_scale_version.sql")

    row = _row(conn, "zero")
    assert row["score_scale_version"] == 2, row
    assert row["severity_score"] == 0.0, row


def test_the_migration_is_idempotent():
    """⚠ 迁移跑两遍不能把数据改坏（第二次必须无变化）。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(BASE_SCHEMA)
    _seed_row(
        conn,
        "old-scale",
        total_hits=9,
        critical=0,
        warning=9,
        info=0,
        severity_score=44.3,
        assessment="严重",
        hit_density=4.43,
    )

    _apply_migration(conn, "add_anti_ai_score_scale_version.sql")
    first = _row(conn, "old-scale")
    _apply_migration(conn, "add_anti_ai_score_scale_version.sql")
    second = _row(conn, "old-scale")

    assert first == second, f"迁移不幂等：{first} → {second}"


# ─────────────────────── 2) 口径常量不许两份漂 ───────────────────────


def test_the_migration_thresholds_match_the_single_source():
    """⚠ 迁移 SQL 里的权重/阈值必须与 Python **单一事实源**一致。

    ⚠ 第 67 批变异验证发现的洞：第一版只查「阈值在 SQL 里**出现过**」
      ⇒ 把其中一个分支的 4.0 改成 5.0 时，另一个分支里的 4.0 还在
      ⇒ 守卫照样绿 ⇒ 现在改成**集合比对**（用到的阈值集合必须完全一致）。
    """
    import re

    from application.audit.services.anti_ai_thresholds import (
        ASSESSMENT_THRESHOLDS,
        DEFAULT_ASSESSMENT,
        SEVERITY_WEIGHTS,
    )

    sql = (MIGRATIONS / "add_anti_ai_score_scale_version.sql").read_text(encoding="utf-8")

    # 权重：SQL 里 `3.0 * critical_hits` 的那个 3.0 必须等于单一事实源。
    sql_weights = {
        column: float(number)
        for number, column in re.findall(
            r"([0-9]+(?:\.[0-9]+)?)\s*\*\s*(critical_hits|warning_hits|info_hits)", sql
        )
    }
    assert sql_weights == {
        "critical_hits": SEVERITY_WEIGHTS["critical"],
        "warning_hits": SEVERITY_WEIGHTS["warning"],
        "info_hits": SEVERITY_WEIGHTS["info"],
    }, f"迁移 SQL 的严重性权重与单一事实源不一致：{sql_weights}"

    # 阈值：SQL 里用到的**值↔标签配对集合**必须与单一事实源一致。
    # ⚠ 第 70 批（R-8）补的：原先只比**值的集合** ⇒ 把 SQL 里 4.0/2.0 的
    #   「严重/中等」**标签对调**（值集合不变）⇒ 互锁照样绿 ⇒ 现在比配对。
    sql_thresholds = {
        (float(value), label)
        for value, label in re.findall(
            r">=\s*([0-9]+(?:\.[0-9]+)?)\s*THEN\s*'(严重|中等|轻微)'", sql
        )
    }
    expected_thresholds = {
        (float(threshold), label)
        for threshold, label in ASSESSMENT_THRESHOLDS
        if label != DEFAULT_ASSESSMENT
    }
    assert sql_thresholds == expected_thresholds, (
        f"迁移 SQL 用到的评级阈值是 {sorted(sql_thresholds)}，"
        f"单一事实源是 {sorted(expected_thresholds)} ⇒ 换算口径漂了"
    )


def test_the_scale_version_is_declared_once():
    """⚠ 口径版本号有**唯一**定义（防两份漂）。"""
    from application.audit.services.anti_ai_thresholds import SCORE_SCALE_VERSION

    assert SCORE_SCALE_VERSION == 2, (
        f"当前口径版本是 {SCORE_SCALE_VERSION}；改口径时必须同时更新迁移与守卫"
    )


# ─────────────────────── 3) 写侧打版本 / 读侧带版本 ───────────────────────


def test_the_repository_writes_the_current_scale_version(tmp_path):
    """⚠ 每次写入都必须打上**当前**口径版本。"""
    from infrastructure.persistence.database.connection import get_database
    from infrastructure.persistence.database.sqlite_anti_ai_audit_repository import (
        SqliteAntiAiAuditRepository,
    )

    db = get_database(str(tmp_path / "anti.db"))
    db.execute(BASE_SCHEMA)
    db.commit()

    repo = SqliteAntiAiAuditRepository(db)
    repo.upsert(
        novel_id="n-1",
        chapter_number=1,
        total_hits=3,
        critical_hits=0,
        warning_hits=3,
        info_hits=0,
        severity_score=1.5,
        overall_assessment="轻微",
        hit_density=2.0,
    )

    row = db.fetch_one(
        "SELECT score_scale_version FROM anti_ai_audits WHERE novel_id = ? AND chapter_number = ?",
        ("n-1", 1),
    )
    assert row is not None, "upsert 没写进去"
    assert row["score_scale_version"] == 2, (
        f"写入的行没有口径版本（{row['score_scale_version']!r}）⇒ 读侧无法判断可比性"
    )


def test_the_read_side_exposes_the_scale_version(tmp_path):
    """⚠ 读侧必须把版本**带出来**（消费方才有办法判断「能不能比」）。"""
    from infrastructure.persistence.database.connection import get_database
    from infrastructure.persistence.database.sqlite_anti_ai_audit_repository import (
        SqliteAntiAiAuditRepository,
    )

    db = get_database(str(tmp_path / "anti.db"))
    db.execute(BASE_SCHEMA)
    db.commit()

    repo = SqliteAntiAiAuditRepository(db)
    repo.upsert(
        novel_id="n-1",
        chapter_number=1,
        total_hits=3,
        critical_hits=0,
        warning_hits=3,
        info_hits=0,
        severity_score=1.5,
        overall_assessment="轻微",
        hit_density=2.0,
    )

    trend = repo.get_trend_data("n-1")
    listed = repo.list_by_novel("n-1")
    single = repo.get_by_chapter("n-1", 1)

    assert trend and trend[0].get("score_scale_version") == 2, trend
    assert listed and listed[0].get("score_scale_version") == 2, listed
    assert single and single.get("score_scale_version") == 2, single
