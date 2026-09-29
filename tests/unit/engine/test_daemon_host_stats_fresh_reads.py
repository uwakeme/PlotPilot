"""守护进程统计读取必须用一次性短连接（防线程本地连接快照钉死）。

回归背景：act_planning_delegate 的僵死会话清理曾在管线线程执行 UPDATE 未 commit，
Python sqlite3 隐式事务把该线程本地连接的读快照钉死在旧时代——驾驶舱完稿数冻结、
book_done 永不触发。统计读取改为每次新开短连接后，必须能看到最新已提交数据。
"""
import sqlite3
from types import SimpleNamespace

import pytest

from engine.runtime.daemon_host import DaemonHostMixin as Host


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """临时 chapters 库：3 行 completed（已提交）。"""
    db_path = tmp_path / "stats.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE chapters (novel_id TEXT, status TEXT, content TEXT)")
    conn.executemany(
        "INSERT INTO chapters VALUES (?, ?, ?)",
        [
            ("n-1", "completed", "alpha"),
            ("n-1", "completed", "beta"),
            ("n-1", "completed", "gamma"),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("application.paths.get_db_path", lambda: str(db_path))
    return str(db_path)


def _host_without_init():
    return Host.__new__(Host)


def test_count_completed_chapters_reads_committed_state(fresh_db):
    host = _host_without_init()
    assert host._count_completed_chapters(SimpleNamespace(value="n-1")) == 3


def test_read_chapter_stats_ephemeral_returns_counts_and_words(fresh_db):
    host = _host_without_init()
    completed, manuscript, words = host._read_chapter_stats_ephemeral("n-1")
    assert (completed, manuscript) == (3, 3)
    assert words == len("alpha") + len("beta") + len("gamma")


def test_stats_reads_see_latest_commit_despite_stray_txn_on_thread_local(
    fresh_db, monkeypatch
):
    """回归：线程本地连接残留未提交 DML（钉住快照）时，统计读取仍看到最新提交。"""
    monkeypatch.setenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "1")

    # 在线程本地连接上制造一条未提交 DML —— 钉住该连接的读快照
    from infrastructure.persistence.database.connection import get_database

    db = get_database(fresh_db)
    db.execute("INSERT INTO chapters VALUES ('n-1', 'draft', 'stray-uncommitted')")

    # 另一连接提交第 4 个 completed 章节（模拟持久化队列消费者正常落库）
    other = sqlite3.connect(fresh_db)
    other.execute("INSERT INTO chapters VALUES ('n-1', 'completed', 'committed-later')")
    other.commit()
    other.close()

    host = _host_without_init()
    # 旧实现（线程本地连接读）在此处只会看到 3；短连接必须看到 4
    assert host._count_completed_chapters(SimpleNamespace(value="n-1")) == 4
    completed, manuscript, _ = host._read_chapter_stats_ephemeral("n-1")
    assert completed == 4
    assert manuscript == 4  # 3 completed + 1 未提交 draft 对短连接不可见

    db.close()  # 清理线程本地连接（丢弃未提交 DML）
