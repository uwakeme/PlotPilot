"""迁移失败必须以 error 级汇总暴露，不允许静默继续。"""
import logging
import sqlite3

from infrastructure.persistence.database.migration_runner import apply_migration_files


def test_failed_migrations_are_summarized_at_error_level(tmp_path, caplog):
    conn = sqlite3.connect(":memory:")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_ok.sql").write_text(
        "CREATE TABLE IF NOT EXISTS mig_ok (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    (migrations / "002_bad.sql").write_text(
        "CREATE TABLE bad_syntax (id INTEGER PRIMARY KEY,;)", encoding="utf-8"
    )

    with caplog.at_level(logging.ERROR, logger="infrastructure.persistence.database.migration_runner"):
        apply_migration_files(conn, migrations)

    assert any("应用失败" in r.message for r in caplog.records)
    assert any("002_bad.sql" in r.message for r in caplog.records)
    conn.close()
