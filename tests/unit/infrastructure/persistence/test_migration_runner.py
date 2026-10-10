"""迁移不能因中途一条语句失败而把整个文件标记为已完成。

背景：migration_runner 用 executescript 整文件执行，中途 duplicate column
会被吞掉并 _mark_applied 整个文件，其后的 ALTER TABLE 永久丢失。
"""
import sqlite3
from pathlib import Path

import pytest

from infrastructure.persistence.database import migration_runner

# 真实迁移语料（触发器 BEGIN...END 拆分边界就落在这里）
MIGRATIONS_DIR = (
    Path(__file__).parents[4]
    / "infrastructure"
    / "persistence"
    / "database"
    / "migrations"
)


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


class TestSplitSqlStatements:
    def test_splits_on_semicolon(self):
        stmts = migration_runner._split_sql_statements(
            "CREATE TABLE a(id INTEGER);\nCREATE TABLE b(id INTEGER);\n"
        )
        assert len(stmts) == 2

    def test_keeps_semicolon_inside_string_literal(self):
        stmts = migration_runner._split_sql_statements(
            "INSERT INTO t(v) VALUES ('a;b');\nSELECT 1;\n"
        )
        assert len(stmts) == 2
        assert "a;b" in stmts[0]

    def test_drops_comment_only_trailing(self):
        stmts = migration_runner._split_sql_statements("SELECT 1;\n-- done\n")
        assert len(stmts) == 1

    def test_empty_input(self):
        assert migration_runner._split_sql_statements("") == []

    def test_comment_only(self):
        assert migration_runner._split_sql_statements("-- only a comment\n") == []

    def test_multiple_statements_on_one_line(self):
        """一行多条语句必须拆开：conn.execute 只能执行单条，否则抛 ProgrammingError。"""
        stmts = migration_runner._split_sql_statements(
            "CREATE TABLE a(i); CREATE TABLE b(j);"
        )
        assert len(stmts) == 2
        assert stmts[0] == "CREATE TABLE a(i);"
        assert stmts[1] == "CREATE TABLE b(j);"


class TestPartialFailureHandling:
    def test_later_statement_still_applied_after_benign_failure(self, conn, tmp_path):
        """第 1 条 duplicate column（良性）不得阻止第 2 条 ALTER 执行。"""
        conn.execute("CREATE TABLE prompt_nodes(id INTEGER PRIMARY KEY, node_type TEXT)")
        conn.commit()

        d = tmp_path / "migrations"
        d.mkdir()
        (d / "999_test_partial.sql").write_text(
            "ALTER TABLE prompt_nodes ADD COLUMN node_type TEXT;\n"
            "ALTER TABLE prompt_nodes ADD COLUMN asset_layer TEXT;\n",
            encoding="utf-8",
        )

        migration_runner.apply_migration_files(conn, d)

        cols = {r[1] for r in conn.execute("PRAGMA table_info(prompt_nodes)")}
        assert "asset_layer" in cols, "良性失败之后的语句必须继续执行"

    def test_hard_failure_not_marked_applied(self, conn, tmp_path):
        """真实失败不得写入 migrations_applied。"""
        d = tmp_path / "migrations"
        d.mkdir()
        (d / "998_test_broken.sql").write_text(
            "CREATE TABLE ok_t(id INTEGER);\nTHIS IS NOT SQL;\n",
            encoding="utf-8",
        )

        migration_runner.apply_migration_files(conn, d)

        rows = conn.execute("SELECT migration_file FROM migrations_applied").fetchall()
        assert all(r[0] != "998_test_broken.sql" for r in rows), (
            "含失败语句的文件不得标记为已完成"
        )

    def test_clean_file_is_marked_applied(self, conn, tmp_path):
        d = tmp_path / "migrations"
        d.mkdir()
        (d / "997_test_ok.sql").write_text(
            "CREATE TABLE ok_t(id INTEGER);\n", encoding="utf-8"
        )

        migration_runner.apply_migration_files(conn, d)

        rows = conn.execute("SELECT migration_file FROM migrations_applied").fetchall()
        assert any(r[0] == "997_test_ok.sql" for r in rows)

    def test_multiple_statements_on_one_line_applied(self, conn, tmp_path):
        """一行多条语句的文件必须全部执行且正常标记（回归：ProgrammingError 会让文件永不标记）。"""
        d = tmp_path / "migrations"
        d.mkdir()
        (d / "996_test_oneline.sql").write_text(
            "CREATE TABLE one_a(id INTEGER); CREATE TABLE one_b(id INTEGER);\n",
            encoding="utf-8",
        )

        migration_runner.apply_migration_files(conn, d)

        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"one_a", "one_b"} <= names, "一行多条语句必须全部执行"

        rows = conn.execute("SELECT migration_file FROM migrations_applied").fetchall()
        assert any(r[0] == "996_test_oneline.sql" for r in rows), (
            "一行多条语句的文件不得被判为失败"
        )

    def test_unsupported_function_not_marked_applied(self, conn, tmp_path):
        """no such function 不再被静默标记完成（本次改动的语义变更，须有测试守住）。"""
        d = tmp_path / "migrations"
        d.mkdir()
        (d / "995_test_bad_fn.sql").write_text(
            "CREATE TABLE fn_t(id INTEGER, v TEXT CHECK (my_fn(v) IS NULL));\n",
            encoding="utf-8",
        )

        migration_runner.apply_migration_files(conn, d)

        rows = conn.execute("SELECT migration_file FROM migrations_applied").fetchall()
        assert all(r[0] != "995_test_bad_fn.sql" for r in rows), (
            "使用了不支持的 SQLite 函数的迁移不得标记为已完成"
        )


class TestRealMigrationCorpusBoundaries:
    """拆分逻辑最容易先坏的边界，钉在真实迁移语料上。"""

    def test_trigger_begin_end_block_kept_as_single_statement(self):
        """005 的 CREATE TRIGGER ... BEGIN ... END; 必须整块保留，不能被内部 ; 拆开。"""
        script = (
            MIGRATIONS_DIR / "005_persistence_queue_enhanced.sql"
        ).read_text(encoding="utf-8")

        stmts = migration_runner._split_sql_statements(script)

        assert len(stmts) == 3
        trigger = stmts[2]
        assert trigger.upper().startswith("CREATE TRIGGER")
        assert "BEGIN" in trigger and "END;" in trigger
        # 触发器内部那条 UPDATE 不能被单独拆成独立语句
        assert sum(1 for s in stmts if "UPDATE persistence_queue" in s) == 1

    def test_trigger_block_actually_executes(self, conn, tmp_path):
        """整块触发器语句必须能被真正执行（只验文本形状不够）。"""
        script = (
            MIGRATIONS_DIR / "005_persistence_queue_enhanced.sql"
        ).read_text(encoding="utf-8")
        conn.execute(
            "CREATE TABLE persistence_queue (id INTEGER PRIMARY KEY, status TEXT)"
        )
        conn.commit()

        d = tmp_path / "migrations"
        d.mkdir()
        (d / "005_trigger.sql").write_text(script, encoding="utf-8")

        migration_runner.apply_migration_files(conn, d)

        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name='update_queue_timestamp'"
        ).fetchone()[0]
        assert n == 1, "触发器必须被完整创建"