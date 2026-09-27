"""章节版本历史(ChapterDraftRepository + 快照钩子)单元测试"""
import sqlite3
from contextlib import contextmanager

import pytest

from infrastructure.persistence.database.chapter_draft_repository import (
    ChapterDraftRepository,
    snapshot_chapter_before_content_change,
)


class _Db:
    """内存 sqlite,带 chapters + chapter_drafts 两表"""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE chapters (
                id TEXT PRIMARY KEY,
                novel_id TEXT NOT NULL,
                number INTEGER NOT NULL,
                title TEXT,
                content TEXT,
                status TEXT DEFAULT 'draft',
                word_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(novel_id, number)
            );
            CREATE TABLE chapter_drafts (
                id TEXT PRIMARY KEY,
                novel_id TEXT NOT NULL,
                chapter_id TEXT NOT NULL,
                chapter_number INTEGER NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                outline TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'manual',
                word_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    @contextmanager
    def transaction(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def execute(self, sql, params=()):
        self.conn.execute(sql, params)
        self.conn.commit()

    def fetch_one(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetch_all(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_connection(self):
        return self.conn

    def insert_chapter(self, novel_id, number, content, status="completed", cid=None):
        self.conn.execute(
            "INSERT INTO chapters (id, novel_id, number, title, content, status, word_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cid or f"ch-{number}", novel_id, number, f"第{number}章", content, status, len(content)),
        )
        self.conn.commit()


def test_snapshot_creates_draft_and_skips_duplicates():
    db = _Db()
    db.insert_chapter("n-1", 5, "第一版正文")

    first = snapshot_chapter_before_content_change(db, "n-1", 5, "manual_save")
    assert first is not None and first.source == "manual_save"

    # 内容未变 → 跳过
    assert snapshot_chapter_before_content_change(db, "n-1", 5, "manual_save") is None
    # 最近快照即当前内容 → 跳过
    assert snapshot_chapter_before_content_change(db, "n-1", 5, "pre_revise") is None

    drafts = ChapterDraftRepository(db).list_drafts("n-1", 5)
    assert len(drafts) == 1


def test_snapshot_prefix_extension_scoped_to_autopilot():
    db = _Db()
    db.insert_chapter("n-1", 1, "开头")
    snapshot_chapter_before_content_change(db, "n-1", 1, "manual_save")
    # 纯延伸:自动驾驶流式路径(skip_extension=True)→ 不留痕
    db.execute("UPDATE chapters SET content = '开头' || '更多内容' WHERE novel_id='n-1' AND number=1")
    assert snapshot_chapter_before_content_change(db, "n-1", 1, "pre_autopilot", skip_extension=True) is None
    # 手动保存/AI 修改:同样的追加是完整编辑动作 → 必须留痕
    assert snapshot_chapter_before_content_change(db, "n-1", 1, "manual_save") is not None
    # 全新内容(非前缀关系)→ 任何来源都留痕
    db.execute("UPDATE chapters SET content = '完全不同的新版本' WHERE novel_id='n-1' AND number=1")
    assert snapshot_chapter_before_content_change(db, "n-1", 1, "manual_save") is not None

    # 空内容章节 / 不存在的章节 → 跳过
    db.insert_chapter("n-1", 2, "")
    assert snapshot_chapter_before_content_change(db, "n-1", 2, "manual_save") is None
    assert snapshot_chapter_before_content_change(db, "n-1", 99, "manual_save") is None


def test_trim_keeps_thirty_versions():
    db = _Db()
    db.insert_chapter("n-1", 1, "v0")
    repo = ChapterDraftRepository(db)
    for i in range(35):
        db.execute("UPDATE chapters SET content = ? WHERE novel_id='n-1' AND number=1", (f"v{i}",))
        snapshot_chapter_before_content_change(db, "n-1", 1, "manual_save")

    drafts = repo.list_drafts("n-1", 1)
    assert len(drafts) == 30
    assert drafts[0].content == "v34"  # 最新在前


def test_restore_updates_content_and_saves_previous(monkeypatch):
    # 避免前缀跳过逻辑干扰:v0/v1/v2 互不为前缀
    db = _Db()
    db.insert_chapter("n-1", 3, "旧版甲", status="completed")
    repo = ChapterDraftRepository(db)
    v1 = repo.save_draft("n-1", "ch-3", 3, "历史版本一", source="manual_save")
    db.execute("UPDATE chapters SET content='当前版' WHERE novel_id='n-1' AND number=3")
    v2 = repo.save_draft("n-1", "ch-3", 3, "历史版本二", source="pre_revise")
    db.execute("UPDATE chapters SET content='当前版乙' WHERE novel_id='n-1' AND number=3")

    result = repo.restore_draft("n-1", 3, v1.id)

    assert result["restored"] is True
    row = db.fetch_one("SELECT content, status, word_count FROM chapters WHERE novel_id='n-1' AND number=3")
    assert row["content"] == "历史版本一"
    assert row["status"] == "completed"  # status 保持
    assert row["word_count"] == len("历史版本一")

    # 恢复前当前内容已存为 pre_restore
    drafts = repo.list_drafts("n-1", 3)
    assert drafts[0].source == "pre_restore" and drafts[0].content == "当前版乙"
    # 可再恢复到 v2(回到恢复前)
    repo.restore_draft("n-1", 3, v2.id)
    row = db.fetch_one("SELECT content FROM chapters WHERE novel_id='n-1' AND number=3")
    assert row["content"] == "历史版本二"


def test_restore_validates_draft_ownership():
    db = _Db()
    db.insert_chapter("n-1", 1, "x")
    repo = ChapterDraftRepository(db)
    repo.save_draft("n-1", "ch-1", 1, "v1")

    with pytest.raises(ValueError):
        repo.restore_draft("n-1", 1, "不存在的id")
    with pytest.raises(ValueError):
        repo.restore_draft("n-1", 9, "不存在的id")


def test_projection_snapshot_hooks_fire(monkeypatch):
    """prose/revise 投影在覆写前应产生对应 source 的快照"""
    db = _Db()
    db.insert_chapter("n-1", 2, "投影前正文", status="completed")

    from application.ai_invocation.contracts.chapter_prose_generation import (
        project_chapter_prose_to_chapters,
    )
    from application.ai_invocation.contracts.chapter_revise_interactive import (
        project_chapter_revise_to_chapters,
    )

    project_chapter_prose_to_chapters(db, {
        "novel_id": "n-1", "chapter_number": 2, "content": "AI生成的新正文", "word_count": 8,
    })
    project_chapter_revise_to_chapters(db, {
        "novel_id": "n-1", "chapter_number": 2, "content": "AI优化后的正文", "word_count": 8,
    })

    repo = ChapterDraftRepository(db)
    drafts = repo.list_drafts("n-1", 2)
    sources = [d.source for d in drafts]
    assert "pre_prose" in sources and "pre_revise" in sources

    row = db.fetch_one("SELECT content, status FROM chapters WHERE novel_id='n-1' AND number=2")
    assert row["content"] == "AI优化后的正文"
    # prose 投影按既有语义把 status 打回 draft;revise 只保内容不改 status
    assert row["status"] == "draft"
