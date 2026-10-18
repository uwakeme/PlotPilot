"""迁移的执行顺序必须让「加列」排在「建表」之后。

背景：`migration_runner.apply_migration_files` 按 `sorted(glob("*.sql"))`
即**文件名字典序**执行。原先加列的迁移叫
`add_macro_diagnosis_context_patch.sql`，建表的叫 `add_macro_diagnosis_results.sql`，
`c` < `r` ⇒ **加列排在建表之前** ⇒ ALTER 执行时表还不存在 ⇒
`no such table: macro_diagnosis_results` ⇒ 迁移失败。

后果（对**新装用户 / 新数据库**）：
1. `macro_diagnosis_results` 表最终被建出来，但没有 `context_patch` /
   `total_words_at_run` 两列 —— 基表 `schema.sql` 里也没有这张表。
2. `application/audit/services/macro_diagnosis_service.py:461-475`
   正在往这两列 INSERT，会抛 `has no column named context_patch`。
3. 迁移永远不会被标记为已应用 ⇒ **每次启动打一条 ERROR 并无限重试**。

老库不受影响（列是后加的文件跑对了顺序时才补上的），所以这个缺陷只在新装环境暴露。

本测试用一个**全新库**走 `DatabaseConnection` 的真实初始化路径
（schema.sql + 全部迁移），断言两列到位。
"""
import pytest

from infrastructure.persistence.database.connection import DatabaseConnection


MIGRATIONS_DIR = (
    "infrastructure/persistence/database/migrations"
)


def _table_exists(conn, table):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return bool(row)


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def test_macro_diagnosis_table_gets_its_two_columns_on_a_fresh_install(tmp_path):
    """全新安装后，静默注入用的两列必须存在。"""
    db = DatabaseConnection(str(tmp_path / "fresh.db"))
    try:
        conn = db.get_connection()
        assert _table_exists(conn, "macro_diagnosis_results"), (
            "建表迁移没有执行 —— 测试前提不成立"
        )
        cols = _columns(conn, "macro_diagnosis_results")
        assert "context_patch" in cols, (
            "全新安装后 macro_diagnosis_results 缺 context_patch 列；"
            "加列迁移很可能又排在了建表迁移之前（字典序陷阱）"
        )
        assert "total_words_at_run" in cols, (
            "全新安装后 macro_diagnosis_results 缺 total_words_at_run 列"
        )
    finally:
        db.close_all()


def _migrations_dir():
    """向上找到含 migrations/ 的仓库根。

    原实现写死 `parents[4]` —— 那是 `tests/`，不是仓库根（应为 `parents[5]`），
    只因后面跟着一个 cwd 兜底才没暴露。cwd 兜底会把「测试从任意目录运行」
    变成隐性依赖；这里改成向上搜索，与运行目录彻底解耦。
    """
    from pathlib import Path

    rel = ("infrastructure", "persistence", "database", "migrations")
    for parent in Path(__file__).resolve().parents:
        candidate = parent.joinpath(*rel)
        if candidate.is_dir():
            return candidate
    raise AssertionError(f"从 {Path(__file__)} 向上找不到 {MIGRATIONS_DIR}")


def test_column_adding_migration_sorts_after_the_table_creating_one():
    """守卫：文件名排序本身就是缺陷的成因，光断言结果不够 —— 排序也要钉住。

    改文件名时这条会红，提醒新名字必须排在 `add_macro_diagnosis_results.sql` 之后。
    """
    mdir = _migrations_dir()

    create_sql = mdir / "add_macro_diagnosis_results.sql"
    alter_sql = mdir / "add_macro_diagnosis_results_context_patch.sql"
    assert create_sql.is_file(), f"建表迁移不见了：{create_sql}"
    assert alter_sql.is_file(), (
        "加列迁移改名后文件缺失；旧名 add_macro_diagnosis_context_patch.sql "
        "在字典序上早于 add_macro_diagnosis_results.sql，必须改名"
    )
    assert create_sql.name < alter_sql.name, (
        f"加列迁移 {alter_sql.name} 必须排在建表迁移 {create_sql.name} 之后，"
        f"否则 ALTER 会先于 CREATE 执行"
    )


def _build_legacy_install_with_patch_columns(target):
    """造一个「已经把 context_patch 两列补齐」的既有安装。

    **不得**从 `data/plotpilot.db` 拷贝：那是 1.5GB 的本机生产库，被 .gitignore
    排除，干净 checkout / CI 上根本不存在。原实现把它当必需输入，
    且守卫写成了 `if not prod:`（`prod` 是非空字面量，恒假）⇒
    `pytest.skip` 永远不可达 ⇒ CI 上必然 FileNotFoundError。
    这里只构造本用例真正需要的最小结构：一张带两列的基表 +
    一条记录旧文件名的 migrations_applied。
    """
    import sqlite3

    conn = sqlite3.connect(target)
    try:
        # 直接执行建表迁移来还原「老库」：表、索引与真实既有安装一致。
        # 手写简化 DDL 会被建表迁移里的 CREATE INDEX ... (novel_id, resolved)
        # 打断 —— 少一列就报 no such column 并计入 ERROR 日志。
        create_sql = (_migrations_dir() / "add_macro_diagnosis_results.sql").read_text(
            encoding="utf-8"
        )
        conn.executescript(create_sql)
        # 既有安装已经补过这两列 ⇒ 改名后的迁移重跑会撞 duplicate column
        conn.execute("ALTER TABLE macro_diagnosis_results ADD COLUMN context_patch TEXT")
        conn.execute(
            "ALTER TABLE macro_diagnosis_results ADD COLUMN total_words_at_run INTEGER DEFAULT 0"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS migrations_applied (
                migration_file TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # 模拟既有安装的迁移记录：旧文件名已在 applied 列表里
        conn.execute(
            "INSERT OR IGNORE INTO migrations_applied (migration_file) VALUES (?)",
            ("add_macro_diagnosis_context_patch.sql",),
        )
        conn.commit()
    finally:
        conn.close()


def test_reapplying_to_an_install_that_already_has_the_columns_is_silent(tmp_path):
    """老库升级：改名后的迁移会重跑一次，撞 duplicate column 必须被静默跳过。

    `_BENIGN_ERROR_MARKERS` 含 "duplicate column"，所以既有安装升级时
    不应产生任何 ERROR 日志，也不应因为失败而永远不标记已应用。
    """
    import logging

    target = tmp_path / "existing_install.db"
    _build_legacy_install_with_patch_columns(target)

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                records.append(record.getMessage())

    log = logging.getLogger("infrastructure.persistence.database.migration_runner")
    handler = _Capture()
    log.addHandler(handler)
    log.setLevel(logging.WARNING)
    try:
        db = DatabaseConnection(str(target))
        try:
            conn = db.get_connection()
            cols = _columns(conn, "macro_diagnosis_results")
            assert "context_patch" in cols
            marked = conn.execute(
                "SELECT COUNT(*) FROM migrations_applied WHERE migration_file=?",
                ("add_macro_diagnosis_results_context_patch.sql",),
            ).fetchone()[0]
            assert marked == 1, "改名后的迁移没有标记为已应用 ⇒ 每次启动都会重试"
        finally:
            db.close_all()
    finally:
        log.removeHandler(handler)

    assert not records, f"老库升级产生了 ERROR 日志：{records[:3]}"
