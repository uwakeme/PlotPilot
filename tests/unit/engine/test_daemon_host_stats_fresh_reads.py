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
    """临时 chapters 库（WAL 模式，与生产一致）：3 行 completed（已提交）。"""
    db_path = tmp_path / "stats.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
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


def test_stats_reads_see_latest_commit_despite_pinned_read_snapshot(
    fresh_db, monkeypatch
):
    """回归：某连接的读事务把 WAL 快照钉在旧时代时，统计读取仍看到最新提交。

    真实形态：守护进程线程本地连接残留隐式事务（如未 commit 的 UPDATE），
    其后所有读都停在旧快照——驾驶舱完稿数冻结、book_done 永不触发。
    """
    # 连接 A：显式读事务，建立并钉住快照（WAL 下不阻塞其他连接写提交）
    stray = sqlite3.connect(fresh_db, isolation_level=None)
    stray.execute("BEGIN")
    stray.execute("SELECT COUNT(*) FROM chapters").fetchone()

    # 连接 B：提交第 4 个 completed 章节（模拟持久化队列消费者正常落库）
    other = sqlite3.connect(fresh_db)
    other.execute("INSERT INTO chapters VALUES ('n-1', 'completed', 'committed-later')")
    other.commit()
    other.close()

    # 钉死的连接 A 仍看到旧快照（证明钉死成立）
    pinned = stray.execute(
        "SELECT COUNT(*) FROM chapters WHERE novel_id='n-1' AND status='completed'"
    ).fetchone()[0]
    assert pinned == 3

    host = _host_without_init()
    # 被测代码必须用一次性短连接，看到 WAL 最新提交
    assert host._count_completed_chapters(SimpleNamespace(value="n-1")) == 4
    completed, manuscript, _ = host._read_chapter_stats_ephemeral("n-1")
    assert completed == 4
    assert manuscript == 4

    stray.execute("ROLLBACK")
    stray.close()
