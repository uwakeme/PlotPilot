"""阅读进度仓储单元测试。

外键在测试里必须显式开启：SQLite 默认关闭，级联删除依赖它
（生产由 sqlite_pragmas.apply_standard_pragmas 开启）。
"""
import sqlite3

import pytest

from infrastructure.persistence.database.reading_progress_repository import (
    ReadingProgressRepository,
)


class _DbBase:
    """内存 sqlite，带 novels + reading_progress。"""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.flush_calls = 0
        self.flush_result = True
        self.conn.executescript(
            """
            CREATE TABLE novels (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL
            );
            CREATE TABLE reading_progress (
                novel_id TEXT PRIMARY KEY,
                last_chapter_number INTEGER NOT NULL DEFAULT 1,
                last_read_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
            );
            """
        )

    def execute(self, sql, params=()):
        self.conn.execute(sql, params)
        self.conn.commit()

    def flush_pending_writes(self, timeout: float = 2.0):
        self.flush_calls += 1
        return self.flush_result

    def fetch_one(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetch_all(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]


@pytest.fixture
def repo():
    db = _DbBase()
    db.execute("INSERT INTO novels (id, slug, title) VALUES ('n1', 'n1', '测试书')", ())
    yield ReadingProgressRepository(db)
    db.conn.close()


def test_get_returns_none_when_never_read(repo):
    assert repo.get("n1") is None


def test_record_inserts_first_progress_row(repo):
    row = repo.record("n1", 3)

    assert row["last_chapter_number"] == 3
    assert row["last_read_at"] is not None


def test_record_twice_updates_same_row_instead_of_inserting(repo):
    repo.record("n1", 3)
    row = repo.record("n1", 7)

    assert row["last_chapter_number"] == 7
    rows = repo.db.fetch_all("SELECT * FROM reading_progress WHERE novel_id = 'n1'")
    assert len(rows) == 1


def test_progress_is_isolated_per_novel(repo):
    repo.db.execute("INSERT INTO novels (id, slug, title) VALUES ('n2', 'n2', '另一本')", ())
    repo.record("n1", 2)

    assert repo.get("n2") is None


def test_deleting_novel_cascades_progress_row(repo):
    repo.record("n1", 5)

    repo.db.execute("DELETE FROM novels WHERE id = 'n1'", ())

    assert repo.get("n1") is None


def test_record_flushes_pending_writes_before_reading_back(repo):
    """入队写是最终一致语义；不回读前必须等队列排空，否则拿到旧值。"""
    repo.record("n1", 4)

    assert repo.db.flush_calls == 1


def test_record_reads_back_only_after_flush():
    """顺序才是关键：flush 必须早于回读，不能只在别处调过就算。"""
    order = []

    class _Db(_DbBase):
        def flush_pending_writes(self, timeout: float = 2.0):
            order.append("flush")
            return True

        def fetch_one(self, sql, params=()):
            order.append("read")
            return super().fetch_one(sql, params)

    db = _Db()
    db.execute("INSERT INTO novels (id, slug, title) VALUES ('n1', 'n1', '测试书')", ())
    ReadingProgressRepository(db).record("n1", 2)

    assert order.index("flush") < order.index("read")
    db.conn.close()


def test_record_returns_none_when_flush_times_out(repo):
    """写队列超时 = 可能读到旧行，必须让调用方能识别，不能静默返回过期数据。"""
    repo.db.flush_result = False

    assert repo.record("n1", 5) is None


def test_record_returns_none_does_not_corrupt_stored_progress(repo):
    """超时后回读为 None，但已入队的写最终仍会落盘——下次上报读到的是 5 而不是旧值。"""
    repo.db.flush_result = False
    assert repo.record("n1", 5) is None

    repo.db.flush_result = True
    row = repo.record("n1", 5)

    assert row["last_chapter_number"] == 5