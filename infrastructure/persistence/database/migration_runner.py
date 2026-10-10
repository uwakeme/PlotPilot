"""SQLite SQL migration runner."""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from infrastructure.persistence.database.sqlite_retry import (
    get_sqlite_retry_settings,
    is_sqlite_lock_error,
    migration_retry_delay,
)

logger = logging.getLogger(__name__)


# executescript() 会先隐式 COMMIT 已执行的前半段，随后一条语句抛错就导致
# 整个文件被 _mark_applied —— 其后的 ALTER TABLE 永久丢失且每次启动跳过。
# 改为逐语句执行：良性错误跳过该条继续，真错误记入 failed 且不标记已应用。
_BENIGN_ERROR_MARKERS = ("already exists", "duplicate column")


def _split_sql_statements(script: str) -> list[str]:
    """把 SQL 脚本拆成单条语句。

    用 sqlite3.complete_statement 判断语句边界，它能正确识别字符串字面量里的
    分号，避免 "INSERT ... VALUES ('a;b')" 被误拆。
    """
    statements: list[str] = []
    buffer = ""
    for line in (script or "").splitlines(keepends=True):
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("--")):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            stmt = buffer.strip()
            if stmt and stmt != ";":
                statements.append(stmt)
            buffer = ""
    tail = buffer.strip()
    if tail:
        statements.append(tail)
    return statements


def apply_migration_files(conn: sqlite3.Connection, migrations_dir: Path) -> None:
    """Apply SQL migrations idempotently using the existing tracking table."""
    retry_settings = get_sqlite_retry_settings()
    max_retries = retry_settings.migration_max_retries
    for attempt in range(max_retries):
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS migrations_applied (
                    migration_file TEXT PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
            break
        except sqlite3.OperationalError as exc:
            if is_sqlite_lock_error(exc) and attempt < max_retries - 1:
                logger.warning(
                    "Database locked, retrying... (attempt %s/%s)",
                    attempt + 1,
                    max_retries,
                )
                time.sleep(migration_retry_delay(attempt, retry_settings))
                continue
            if "already exists" in str(exc):
                break
            logger.warning(
                "Cannot create migrations_applied table: %s, using legacy mode",
                exc,
            )
            apply_migration_files_legacy(conn, migrations_dir)
            return

    applied = set()
    try:
        cursor = conn.execute("SELECT migration_file FROM migrations_applied")
        applied = {row[0] for row in cursor.fetchall()}
    except Exception:
        pass

    if not migrations_dir.is_dir():
        logger.warning(
            "未找到迁移目录（将仅依赖 schema.sql 与代码内补丁）: %s",
            migrations_dir,
        )
        return

    new_migrations = 0
    failed: list[tuple[str, str]] = []
    for migration_path in sorted(migrations_dir.glob("*.sql")):
        migration_file = migration_path.name
        if migration_file in applied:
            continue

        try:
            migration_sql = migration_path.read_text(encoding="utf-8")
        except Exception as exc:
            failed.append((migration_file, f"读取失败: {exc}"))
            continue

        file_failed = False
        for statement in _split_sql_statements(migration_sql):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as exc:
                err = str(exc)
                if any(m in err for m in _BENIGN_ERROR_MARKERS):
                    logger.debug(
                        "Migration %s 语句已存在，跳过: %s", migration_file, err
                    )
                    continue
                file_failed = True
                logger.warning(
                    "Migration %s 语句失败: %s | SQL: %s",
                    migration_file, err,
                    statement[:120].replace("\n", " "),
                )
            except Exception as exc:
                file_failed = True
                logger.warning(
                    "Migration %s 语句异常: %s | SQL: %s",
                    migration_file, exc,
                    statement[:120].replace("\n", " "),
                )

        conn.commit()

        if file_failed:
            failed.append((migration_file, "存在失败语句，未标记为已应用"))
            continue

        conn.execute(
            "INSERT OR IGNORE INTO migrations_applied (migration_file) VALUES (?)",
            (migration_file,),
        )
        conn.commit()
        logger.info("Applied migration: %s", migration_file)
        new_migrations += 1

    if new_migrations == 0 and applied:
        logger.debug("All %d migrations already applied, skipped", len(applied))

    if failed:
        # 迁移失败不阻断启动（历史库可能已有等价结构），但必须以 error 级
        # 汇总暴露——静默继续会让数据库停留在不一致状态而无任何显性信号
        logger.error(
            "%d 个迁移文件应用失败（数据库可能缺列/缺表，相关功能将异常）: %s",
            len(failed),
            "; ".join(f"{name}: {err}" for name, err in failed),
        )


def apply_migration_files_legacy(
    conn: sqlite3.Connection, migrations_dir: Path
) -> None:
    """Legacy migration mode without a tracking table."""
    if not migrations_dir.is_dir():
        logger.warning(
            "未找到迁移目录（将仅依赖 schema.sql 与代码内补丁）: %s",
            migrations_dir,
        )
        return

    for migration_path in sorted(migrations_dir.glob("*.sql")):
        migration_file = migration_path.name
        try:
            migration_sql = migration_path.read_text(encoding="utf-8")
            conn.executescript(migration_sql)
            conn.commit()
            logger.info("Applied migration: %s", migration_file)
        except sqlite3.OperationalError as exc:
            if "already exists" in str(exc) or "duplicate column" in str(exc):
                logger.debug("Migration %s already applied: %s", migration_file, exc)
            else:
                logger.warning("Migration %s failed: %s", migration_file, exc)
        except OSError as exc:
            logger.warning("Failed to read migration %s: %s", migration_file, exc)
        except Exception as exc:
            logger.warning("Failed to apply migration %s: %s", migration_file, exc)


def _mark_applied(conn: sqlite3.Connection, migration_file: str) -> None:
    try:
        conn.execute(
            "INSERT OR IGNORE INTO migrations_applied (migration_file) VALUES (?)",
            (migration_file,),
        )
        conn.commit()
    except Exception:
        pass
