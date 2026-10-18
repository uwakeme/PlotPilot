import sqlite3
from pathlib import Path

from infrastructure.persistence.database.sqlite_storyline_repository import SqliteStorylineRepository
from domain.novel.entities.storyline import Storyline
from domain.novel.value_objects.novel_id import NovelId
from domain.novel.value_objects.storyline_role import StorylineRole
from domain.novel.value_objects.storyline_status import StorylineStatus
from domain.novel.value_objects.storyline_type import StorylineType


def _storyline_ddl() -> str:
    """从**真实** `schema.sql` 里截出 storylines / storyline_milestones 的 DDL。

    ⚠ 此前这里手抄了一份 DDL，于是 schema 一改就跟真实库分叉 ——
    加 `estimated_chapter_end_source` 时它就是第一个红的，而且红得莫名其妙
    （`table storylines has no column named ...`）。复刻替身必然漂移，
    派生才跟得上。
    """
    root = Path(__file__).resolve().parents[4]
    sql = (root / "infrastructure" / "persistence" / "database" / "schema.sql").read_text(
        encoding="utf-8-sig"
    )
    wanted = ("STORYLINES", "STORYLINE_MILESTONES")
    chunks = []
    for stmt in sql.split(";"):
        # 先剥掉 `--` 注释行：schema.sql 里每张表前面都有一段注释，
        # 不剥的话 head 开头是 `--` 而不是 CREATE，匹配不到。
        code_lines = [
            ln for ln in stmt.splitlines() if not ln.lstrip().startswith("--")
        ]
        code = "\n".join(code_lines).strip()
        head = code.upper()
        if not head.startswith("CREATE TABLE IF NOT EXISTS"):
            continue
        if any(f" {name} (" in head for name in wanted):
            chunks.append(code)
    assert len(chunks) >= 2, (
        f"没从 schema.sql 里截到 storylines 相关 DDL（找到 {len(chunks)} 段）—— "
        f"schema.sql 结构可能变了"
    )
    return ";\n".join(chunks) + ";"


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # FK 指向 novels，这里只建故事线两张表；sqlite3 默认不开 FK 强制
    conn.executescript(_storyline_ddl())
    conn.commit()

    class _DB:
        def get_connection(self): return conn
        def fetch_one(self, sql, params=()):
            cur = conn.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None
        def fetch_all(self, sql, params=()):
            cur = conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    return _DB()


def test_save_and_load_with_new_fields():
    db = _make_db()
    repo = SqliteStorylineRepository(db)

    sl = Storyline(
        id="sl-test-1",
        novel_id=NovelId("novel-test"),
        storyline_type=StorylineType.MAIN_PLOT,
        status=StorylineStatus.ACTIVE,
        estimated_chapter_start=1,
        estimated_chapter_end=80,
        role=StorylineRole.SUB,
        parent_id="sl-main-1",
        chapter_weight=0.6,
        # G-03：来源必须跟着取值一起落库
        estimated_chapter_end_source="ai",
        estimated_chapter_end_note="采用模型给出的预估收尾章",
    )
    repo.save(sl)

    loaded = repo.get_by_id("sl-test-1")
    assert loaded is not None
    assert loaded.role == StorylineRole.SUB
    assert loaded.parent_id == "sl-main-1"
    assert abs(loaded.chapter_weight - 0.6) < 0.001
    assert loaded.estimated_chapter_end == 80
    assert loaded.estimated_chapter_end_source == "ai", (
        "收尾章来源没落库 —— 落库后「模型判断」与「代码兜底」无法分辨"
    )
    assert "模型" in loaded.estimated_chapter_end_note


def test_source_defaults_to_empty_not_ai():
    """未标注来源必须是空串 —— 默认填 'ai' 会把旧数据读成「模型的判断」。"""
    db = _make_db()
    repo = SqliteStorylineRepository(db)

    sl = Storyline(
        id="sl-nosrc",
        novel_id=NovelId("novel-test"),
        storyline_type=StorylineType.MAIN_PLOT,
        status=StorylineStatus.ACTIVE,
        estimated_chapter_start=1,
        estimated_chapter_end=50,
    )
    repo.save(sl)
    loaded = repo.get_by_id("sl-nosrc")
    assert loaded.estimated_chapter_end == 50
    assert loaded.estimated_chapter_end_source == ""
    assert loaded.estimated_chapter_end_source != "ai"


def test_old_data_without_role_column_defaults_to_main():
    """Simulate loading from a DB that doesn't have role/parent_id/chapter_weight columns yet."""
    db = _make_db()
    repo = SqliteStorylineRepository(db)
    # Insert directly without new columns (simulating old data)
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO storylines (id, novel_id, storyline_type, status, "
        "estimated_chapter_start, estimated_chapter_end, current_milestone_index, "
        "extensions, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("sl-old", "novel-1", "main_plot", "active", 1, 10, 0, "{}", "2024-01-01", "2024-01-01")
    )
    conn.commit()
    # Now add missing columns (repo.save would do this, but we test _row_to_storyline directly)
    # First trigger the pragma check by calling get_by_id which goes through _row_to_storyline
    loaded = repo.get_by_id("sl-old")
    assert loaded is not None
    assert loaded.role == StorylineRole.MAIN  # default
