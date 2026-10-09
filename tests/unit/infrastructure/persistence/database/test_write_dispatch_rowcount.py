"""单写者派发器下 DELETE/UPDATE 的 rowcount 语义修复。

历史缺陷：非 writer 线程的写 SQL 入队后返回的游标 rowcount 硬编码 1，
十余处 ``cur.rowcount > 0`` 的删除/更新判定对「目标不存在」也返回成功。
"""
import sqlite3
from pathlib import Path

import pytest

from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.write_dispatch import (
    estimate_mutating_rowcount,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parents[5] / "infrastructure" / "persistence" / "database" / "schema.sql"
)


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    for i in range(3):
        conn.execute(
            "INSERT INTO story_nodes (id, novel_id, node_type, number, title,"
            " planning_status, planning_source, order_index, created_at, updated_at)"
            " VALUES (?, 'n1', 'chapter', ?, 't', 'confirmed', 'manual', 0,"
            " '2026-01-01', '2026-01-01')",
            (f"node-{i}", i + 1),
        )
    conn.commit()
    conn.close()
    return DatabaseConnection(str(db_path))


def test_estimate_delete_with_where_params(db):
    sql = "DELETE FROM story_nodes WHERE id = ?"
    assert estimate_mutating_rowcount(db, sql, ("node-1",)) == 1
    assert estimate_mutating_rowcount(db, sql, ("missing",)) == 0


def test_estimate_update_skips_set_placeholders(db):
    sql = "UPDATE story_nodes SET title = ?, order_index = ? WHERE id = ?"
    # SET 有 2 个占位符，WHERE 1 个；params 应偏移 2
    assert estimate_mutating_rowcount(db, sql, ("新标题", 9, "node-2")) == 1
    assert estimate_mutating_rowcount(db, sql, ("新标题", 9, "missing")) == 0


def test_estimate_ignores_question_marks_inside_string_literals(db):
    # SQL 内联字符串字面量里的 '?'（引号内）不计占位符，WHERE 参数对位正确
    sql = "UPDATE story_nodes SET title = '为什么??' WHERE id = ?"
    assert estimate_mutating_rowcount(db, sql, ("node-2",)) == 1


def test_estimate_unsupported_pattern_returns_none(db):
    # CTE 形式不受支持：返回 None 由调用方回退旧行为
    with_cte = "WITH x AS (SELECT 1) DELETE FROM story_nodes WHERE id = ?"
    assert estimate_mutating_rowcount(db, with_cte, ("node-1",)) is None


def test_estimate_delete_without_where_counts_all(db):
    sql = "DELETE FROM story_nodes"
    assert estimate_mutating_rowcount(db, sql, ()) == 3


def test_direct_delete_missing_row_reports_zero_rowcount(db, monkeypatch):
    """直连模式下删不存在的行 rowcount 为 0（回归锚点：派发模式估算必须与此语义一致）。"""
    monkeypatch.setenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "1")
    cur = db.execute("DELETE FROM story_nodes WHERE id = ?", ("no-such-id",))
    db.get_connection().commit()
    assert cur.rowcount == 0
    cur = db.execute("DELETE FROM story_nodes WHERE id = ?", ("node-1",))
    db.get_connection().commit()
    assert cur.rowcount == 1
