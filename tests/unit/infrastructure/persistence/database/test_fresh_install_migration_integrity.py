"""全新安装必须把每个迁移文件都标记为已应用。

背景
----
`apply_migration_files` 逐条 `conn.execute(statement)`，任一语句抛错就把**整个文件**
判为失败并**跳过写入 `migrations_applied`**（`migration_runner.py:158-160`）。
这意味着一个迁移文件只要含有哪怕一条永远执行不了的语句，它就会被无限重试 ——
每次启动重跑一遍，并打一条 ERROR 级汇总日志，且**不会有任何用户可见的症状**
（失败的那条语句可能只是条无副作用的诊断语句，库结构其实是对的）。

两个真实踩过的实例：

1. `004_performance_indexes.sql` 里三条 `EXPLAIN QUERY PLAN` 带 `?` 占位符却
   不预绑定参数 ⇒ `sqlite3.ProgrammingError`（注意：**不是** `OperationalError`，
   所以走不到 `_BENIGN_ERROR_MARKERS` 分支）⇒ 文件永不标记已应用。
   顺带让末尾的 `ANALYZE` 在每次启动时全库扫描一遍。
   既有安装因该文件早已被标记而侥幸无事，只有全新安装暴露 —— 所以它活了很久。

2. `add_macro_diagnosis_context_patch.sql` 在字典序上早于建表的
   `add_macro_diagnosis_results.sql` ⇒ ALTER 先于 CREATE ⇒ `no such table`
   ⇒ 同样永不标记（已由 `test_macro_diagnosis_migration_order.py` 单独钉住）。

两个实例的共同形态是「测试看不见、日志只有一行、库结构却是对的」。
本文件是**通用守卫**：不针对具体文件，只断言不变式 ——
全新安装跑完之后，迁移目录里的每个文件都必须进入 `migrations_applied`。

反向价值：将来任何人往任何迁移里塞一条执行不了的语句，这条测试立刻变红，
而不是像 `004` 那样无声无息地每次启动重试。
"""
import logging
from pathlib import Path

import pytest

from infrastructure.persistence.database.connection import (
    DatabaseConnection,
    _database_asset_dir,
)


def _migrations_dir() -> Path:
    return _database_asset_dir() / "migrations"


def _start_error_capture():
    """把 migration_runner 的 ERROR 级日志接进一个真正会拿到东西的 list。

    返回 (handler, logger, records, 原 level)。注意 records 必须由这里创建
    并返回给调用方 —— 如果调用方另起一个空 list 再「顺带」返回，捕获就断链了，
    断言会永远空洞通过。
    """
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                records.append(record.getMessage())

    handler = _Capture()
    log = logging.getLogger("infrastructure.persistence.database.migration_runner")
    log.addHandler(handler)
    previous_level = log.level
    log.setLevel(logging.WARNING)
    return handler, log, records, previous_level


@pytest.fixture
def fresh_install(tmp_path):
    """走 DatabaseConnection 的真实初始化路径建一个全新库。

    yield (连接, 捕获到的 ERROR 日志列表)。
    """
    handler, log, records, previous_level = _start_error_capture()
    db = None
    try:
        db = DatabaseConnection(str(tmp_path / "fresh.db"))
        conn = db.get_connection()
        yield conn, records
    finally:
        if db is not None:
            db.close_all()
        log.removeHandler(handler)
        log.setLevel(previous_level)


def test_every_migration_file_is_marked_applied_on_a_fresh_install(fresh_install):
    """不变式：全新库跑完迁移链后，目录里每个 .sql 都必须在 migrations_applied 里。

    任何一个文件缺席 = 它的某条语句执行失败 = 每次启动都会重跑并刷 ERROR 日志。
    """
    conn, _records = fresh_install

    on_disk = {p.name for p in _migrations_dir().glob("*.sql")}
    assert on_disk, "迁移目录为空 —— 测试前提不成立"

    marked = {
        row[0] for row in conn.execute("SELECT migration_file FROM migrations_applied")
    }
    never_marked = sorted(on_disk - marked)
    assert not never_marked, (
        f"全新安装后这些迁移文件从未被标记为已应用（说明其中有语句执行失败，"
        f"启动时会无限重试并刷 ERROR 日志）：{never_marked}"
    )


def test_fresh_install_emits_no_migration_error_logs(fresh_install):
    """失败会以 ERROR 汇总暴露出来 —— 全新安装不该有这一行。"""
    _conn, records = fresh_install
    assert not records, f"全新安装产生了迁移失败日志：{records[:3]}"


def test_fresh_install_actually_creates_the_004_performance_indexes(tmp_path):
    """守卫「别删过头」：移走 EXPLAIN 诊断语句时不能连带干掉真正的索引。

    004 里那些 EXPLAIN 本身无 schema 副作用，被移除是正确的；但同文件的
    `CREATE INDEX IF NOT EXISTS` 是要留给所有安装的，必须仍然落到库里 ——
    这里验证的是**真库状态**，不是「文本里还写着」。
    """
    handler, log, _records, previous_level = _start_error_capture()
    db = None
    try:
        db = DatabaseConnection(str(tmp_path / "fresh_idx.db"))
        conn = db.get_connection()
        present = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
            )
        }
    finally:
        if db is not None:
            db.close_all()
        log.removeHandler(handler)
        log.setLevel(previous_level)

    required = {
        "idx_novels_autopilot_running",
        "idx_novels_active_cover",
        "idx_chapters_novel_cover",
        "idx_chapters_novel_status",
        "idx_triples_novel_chapter_cover",
        "idx_triples_subject_cover",
        "idx_triples_entity_type_cover",
        "idx_story_nodes_novel_tree",
        "idx_story_nodes_planning",
        "idx_chapter_summaries_knowledge_cover",
    }
    missing = sorted(required - present)
    assert not missing, f"全新安装后这些 004 迁移创建的索引不存在：{missing}"
