"""迁移不能因中途一条语句失败而把整个文件标记为已完成。

背景：migration_runner 用 executescript 整文件执行，中途 duplicate column
会被吞掉并 _mark_applied 整个文件，其后的 ALTER TABLE 永久丢失。
"""
import sqlite3

import pytest

from infrastructure.persistence.database import migration_runner


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