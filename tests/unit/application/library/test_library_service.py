"""书库读模型单元测试。"""
import json
import sqlite3

import pytest

from application.library.services.library_service import (
    LibraryService,
    ProgressWriteNotFlushedError,
)


class _Db:
    """内存 sqlite，建 novels / chapters / reading_progress 三表。"""

    def __init__(self):
        self.flush_result = True
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(
            """
            CREATE TABLE novels (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                author TEXT DEFAULT '未知作者',
                target_chapters INTEGER NOT NULL DEFAULT 0,
                premise TEXT DEFAULT '',
                current_stage TEXT DEFAULT 'planning',
                generation_prefs_json TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE chapters (
                id TEXT PRIMARY KEY,
                novel_id TEXT NOT NULL,
                number INTEGER NOT NULL,
                title TEXT,
                content TEXT,
                status TEXT DEFAULT 'draft',
                UNIQUE(novel_id, number),
                FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
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
        return self.flush_result

    def fetch_one(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetch_all(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]


@pytest.fixture
def service():
    db = _Db()
    yield LibraryService(db)
    db.conn.close()


def _add_novel(db, novel_id, title, stage="writing", prefs=None, premise="", updated="2026-01-01 00:00:00"):
    db.execute(
        "INSERT INTO novels (id, slug, title, author, premise, current_stage, "
        "generation_prefs_json, target_chapters, updated_at) "
        "VALUES (?, ?, ?, '未知作者', ?, ?, ?, 100, ?)",
        (
            novel_id,
            novel_id,
            title,
            premise,
            stage,
            json.dumps(prefs or {}, ensure_ascii=False),
            updated,
        ),
    )


def _add_chapter(db, novel_id, number, content="正文", title=None, status="draft"):
    db.execute(
        "INSERT INTO chapters (id, novel_id, number, title, content, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (f"{novel_id}-ch{number}", novel_id, number, title, content, status),
    )


# ── 列表聚合 ────────────────────────────────────────────────

def test_chapter_count_excludes_chapters_without_content(service):
    db = service.db
    _add_novel(db, "n1", "甲书")
    _add_chapter(db, "n1", 1, content="一二三")
    _add_chapter(db, "n1", 2, content="")   # 空壳章节，不该计入
    _add_chapter(db, "n1", 3, content="四")

    book = next(b for b in service.list_books() if b["slug"] == "n1")

    assert book["chapter_count"] == 2


def test_total_word_count_uses_length_of_content(service):
    db = service.db
    _add_novel(db, "n1", "甲书")
    _add_chapter(db, "n1", 1, content="一二三")     # 3
    _add_chapter(db, "n1", 2, content="四五")       # 2

    book = next(b for b in service.list_books() if b["slug"] == "n1")

    assert book["total_word_count"] == 5


def test_book_with_no_chapters_yields_zero_counters(service):
    db = service.db
    _add_novel(db, "n1", "甲书")

    book = next(b for b in service.list_books() if b["slug"] == "n1")

    assert book["chapter_count"] == 0
    assert book["total_word_count"] == 0
    assert book["last_read"] is None


def test_stage_is_mapped_through_public_stage_name(service):
    db = service.db
    _add_novel(db, "n1", "甲书", stage="auditing")

    book = next(b for b in service.list_books() if b["slug"] == "n1")

    assert book["stage"] == "reviewing"


def test_genre_prefers_locked_value_from_generation_prefs(service):
    db = service.db
    _add_novel(db, "n1", "甲书", prefs={"locked_genre": "仙侠", "locked_world_preset": "东方修真"})

    book = next(b for b in service.list_books() if b["slug"] == "n1")

    assert book["genre"] == "仙侠"
    assert book["world_preset"] == "东方修真"


def test_last_read_carries_chapter_number_and_title(service):
    db = service.db
    _add_novel(db, "n1", "甲书")
    _add_chapter(db, "n1", 4, title="风起")
    service.record_progress("n1", 4)

    book = next(b for b in service.list_books() if b["slug"] == "n1")

    assert book["last_read"]["chapter_number"] == 4
    assert book["last_read"]["chapter_title"] == "风起"


def test_last_read_title_falls_back_to_chapter_number(service):
    db = service.db
    _add_novel(db, "n1", "甲书")
    _add_chapter(db, "n1", 6, title=None)
    service.record_progress("n1", 6)

    book = next(b for b in service.list_books() if b["slug"] == "n1")

    assert book["last_read"]["chapter_title"] == "第 6 章"


# ── 排序 ───────────────────────────────────────────────────

def test_read_books_sort_before_unread(service):
    db = service.db
    _add_novel(db, "n1", "甲书", updated="2026-01-01 00:00:00")
    _add_novel(db, "n2", "乙书", updated="2099-01-01 00:00:00")
    service.record_progress("n1", 1)

    order = [b["slug"] for b in service.list_books()]

    assert order.index("n1") < order.index("n2")


def test_sort_by_updated_is_accepted(service):
    db = service.db
    _add_novel(db, "n1", "甲书", updated="2026-01-01 00:00:00")
    _add_novel(db, "n2", "乙书", updated="2099-01-01 00:00:00")

    order = [b["slug"] for b in service.list_books(sort="updated")]

    assert order == ["n2", "n1"]


def test_unknown_sort_value_falls_back_to_recent(service):
    db = service.db
    _add_novel(db, "n1", "甲书")

    assert service.list_books(sort="nonsense") == service.list_books(sort="recent")


# ── 目录 ───────────────────────────────────────────────────

def test_toc_lists_chapters_ordered_and_without_content(service):
    db = service.db
    _add_novel(db, "n1", "甲书")
    _add_chapter(db, "n1", 2, content="二十二", title="第二章", status="draft")
    _add_chapter(db, "n1", 1, content="一", title="第一章", status="completed")

    toc = service.get_toc("n1")

    assert [c["number"] for c in toc["chapters"]] == [1, 2]
    assert toc["chapters"][0]["title"] == "第一章"
    assert toc["chapters"][0]["word_count"] == 1
    assert toc["chapters"][0]["status"] == "completed"


def test_toc_includes_chapter_even_when_content_empty(service):
    db = service.db
    _add_novel(db, "n1", "甲书")
    _add_chapter(db, "n1", 1, content="")

    toc = service.get_toc("n1")

    assert [c["number"] for c in toc["chapters"]] == [1]


def test_toc_returns_none_for_unknown_novel(service):
    assert service.get_toc("nope") is None


# ── 单章 ───────────────────────────────────────────────────

def test_get_chapter_returns_content_and_neighbours(service):
    db = service.db
    _add_novel(db, "n1", "甲书")
    _add_chapter(db, "n1", 1, content="一")
    _add_chapter(db, "n1", 2, content="二")
    _add_chapter(db, "n1", 3, content="三")

    chapter = service.get_chapter("n1", 2)

    assert chapter["content"] == "二"
    assert chapter["prev_number"] == 1
    assert chapter["next_number"] == 3


def test_first_chapter_has_no_prev_and_last_has_no_next(service):
    db = service.db
    _add_novel(db, "n1", "甲书")
    _add_chapter(db, "n1", 1, content="一")
    _add_chapter(db, "n1", 2, content="二")

    first = service.get_chapter("n1", 1)
    last = service.get_chapter("n1", 2)

    assert first["prev_number"] is None
    assert first["next_number"] == 2
    assert last["prev_number"] == 1
    assert last["next_number"] is None


def test_neighbours_are_number_based_not_row_based(service):
    """第 2 章被删后，第 3 章的 prev 应是 1 而不是 2。"""
    db = service.db
    _add_novel(db, "n1", "甲书")
    _add_chapter(db, "n1", 1, content="一")
    _add_chapter(db, "n1", 3, content="三")

    chapter = service.get_chapter("n1", 3)

    assert chapter["prev_number"] == 1


def test_get_chapter_returns_none_for_unknown_novel_or_number(service):
    db = service.db
    _add_novel(db, "n1", "甲书")

    assert service.get_chapter("nope", 1) is None
    assert service.get_chapter("n1", 99) is None


def test_get_chapter_preserves_markdown_and_crlf(service):
    db = service.db
    _add_novel(db, "n1", "甲书")
    _add_chapter(db, "n1", 1, content="# 标题\r\n\r\n正文")

    chapter = service.get_chapter("n1", 1)

    assert chapter["content"] == "# 标题\r\n\r\n正文"


# ── 进度 ───────────────────────────────────────────────────

def test_record_progress_rejects_unknown_novel(service):
    assert service.record_progress("nope", 1) is None


def test_record_progress_is_idempotent_for_same_chapter(service):
    db = service.db
    _add_novel(db, "n1", "甲书")

    service.record_progress("n1", 3)
    service.record_progress("n1", 3)

    rows = db.fetch_all("SELECT * FROM reading_progress")
    assert len(rows) == 1


def test_record_progress_raises_when_write_not_flushed(service):
    """写队列超时：宁可报错，也不能把上一章的旧章号当新章号返回。"""
    db = service.db
    _add_novel(db, "n1", "甲书")
    db.flush_result = False

    with pytest.raises(ProgressWriteNotFlushedError):
        service.record_progress("n1", 7)