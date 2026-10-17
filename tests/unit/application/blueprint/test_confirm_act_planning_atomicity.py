"""幕级规划确认的原子性与同号替换语义（真实 SQLite 落库验证）。

覆盖两个历史缺陷：
1. 删旧与插新非原子：中途失败后旧章已被永久删除（丢章窗口）；
2. 删除复用带 renumber 级联的章节删除：重确认中间幕会把后续幕正文行
   整体错号，随后被树↔正文对齐逻辑误删。
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
)
from infrastructure.persistence.database.chapter_element_repository import (
    ChapterElementRepository,
)
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)
from infrastructure.persistence.database.story_node_repository import (
    StoryNodeRepository,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parents[4] / "infrastructure" / "persistence" / "database" / "schema.sql"
)

NOVEL_ID = "n1"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "1")
    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, 'T', 'slug1', 0)",
        (NOVEL_ID,),
    )
    # 三幕结构：幕A(1-2) 幕B(3-4) 幕C(5-6)
    for node_id, parent, ntype, number in [
        ("act-a", NOVEL_ID, "act", 1),
        ("act-b", NOVEL_ID, "act", 2),
        ("act-c", NOVEL_ID, "act", 3),
    ]:
        conn.execute(
            "INSERT INTO story_nodes (id, novel_id, parent_id, node_type, number, title,"
            " planning_status, planning_source, order_index, created_at, updated_at)"
            " VALUES (?, ?, NULL, ?, ?, ?, 'confirmed', 'manual', 0, '2026-01-01', '2026-01-01')",
            (node_id, NOVEL_ID, ntype, number, node_id),
        )
    for act_id, start in (("act-a", 1), ("act-b", 3), ("act-c", 5)):
        for offset in range(2):
            number = start + offset
            chapter_id = f"chapter-{NOVEL_ID}-chapter-{number}"
            conn.execute(
                "INSERT INTO story_nodes (id, novel_id, parent_id, node_type, number, title,"
                " planning_status, planning_source, order_index, created_at, updated_at)"
                " VALUES (?, ?, ?, 'chapter', ?, ?, 'confirmed', 'ai_act', 0,"
                " '2026-01-01', '2026-01-01')",
                (chapter_id, NOVEL_ID, act_id, number, f"第{number}章"),
            )
            conn.execute(
                "INSERT INTO chapters (id, novel_id, number, title, content, outline, status,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, '', 'draft', '2026-01-01', '2026-01-01')",
                (chapter_id, NOVEL_ID, number, f"第{number}章", f"正文{number}"),
            )
    conn.commit()
    conn.close()
    return DatabaseConnection(str(db_path))


def _make_svc(db):
    svc = ContinuousPlanningService(
        story_node_repo=StoryNodeRepository(db),
        chapter_element_repo=ChapterElementRepository(str(db.db_path)),
        llm_service=SimpleNamespace(),
        chapter_repository=SqliteChapterRepository(db),
    )
    svc._write_chapter_plan_variables = lambda **kwargs: None

    @contextmanager
    def _txn():
        with db.transaction() as conn:
            yield conn

    svc._write_txn = _txn
    return svc


def _plan_rows(*titles):
    return [
        {
            "number": i + 1,
            "title": t,
            "main_event": f"{t}事件",
            "handoff_from_previous": "承接",
            "handoff_to_next": "引出",
            # 伏笔动作是 planning-act/user.md 的必填字段，校验链会查。
            # plant_and_resolve 在任意位置都合法（它既算种也算收），
            # 且是生产库里最常见的取值（261/586），这里用它保持 fixture 贴近真实。
            "foreshadow_action": "plant_and_resolve",
        }
        for i, t in enumerate(titles)
    ]


def _book_rows(db):
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT id, number, content FROM chapters WHERE novel_id = ? ORDER BY number",
        (NOVEL_ID,),
    ).fetchall()
    db.get_connection().commit()
    return [(r["id"], r["number"], r["content"]) for r in rows]


@pytest.mark.asyncio
async def test_mid_transaction_failure_rolls_back_deletions(db):
    """事务中途失败：本幕旧章与正文行必须原样保留，不出现丢章窗口。"""
    svc = _make_svc(db)
    original = svc.story_node_repo.save_batch_on_conn

    def _boom(conn, nodes):
        raise sqlite3.IntegrityError("FOREIGN KEY constraint failed (simulated)")

    svc.story_node_repo.save_batch_on_conn = _boom
    svc._normalize_act_chapter_row = lambda raw, act_local_index: {"title": raw["title"]}
    svc._merged_elements_dict = lambda row: {}
    svc._create_elements_from_data = lambda cid, d: []

    before = _book_rows(db)
    assert len(before) == 6

    with pytest.raises(sqlite3.IntegrityError):
        await svc.confirm_act_planning(
            act_id="act-b",
            chapters=_plan_rows("新章A", "新章B"),
        )

    assert _book_rows(db) == before  # 3、4 章正文完好
    conn = db.get_connection()
    tree_count = conn.execute(
        "SELECT COUNT(*) FROM story_nodes WHERE novel_id = ? AND node_type = 'chapter'",
        (NOVEL_ID,),
    ).fetchone()[0]
    assert tree_count == 6  # 树上章节一个没少
    svc.story_node_repo.save_batch_on_conn = original


@pytest.mark.asyncio
async def test_reconfirm_middle_act_keeps_later_chapters_intact(db):
    """重确认中间幕（幕B 3-4）：后续幕（幕C 5-6）正文行号不变、内容不丢，新章同号替换。"""
    svc = _make_svc(db)
    svc._normalize_act_chapter_row = lambda raw, act_local_index: {"title": raw["title"]}
    svc._merged_elements_dict = lambda row: {}
    svc._create_elements_from_data = lambda cid, d: []

    result = await svc.confirm_act_planning(
        act_id="act-b",
        chapters=_plan_rows("新B1", "新B2"),
    )
    assert result["success"] is True

    rows = _book_rows(db)
    assert [(n, c) for _, n, c in rows] == [
        (1, "正文1"), (2, "正文2"),
        (3, ""), (4, ""),          # 幕B 新章（同号替换，内容重置为规划占位）
        (5, "正文5"), (6, "正文6"),  # 幕C 原样保留
    ]
    assert rows[2][0] == f"chapter-{NOVEL_ID}-chapter-3"

    # 幕聚合字段回填正确
    conn = db.get_connection()
    act = conn.execute(
        "SELECT chapter_start, chapter_end, chapter_count FROM story_nodes WHERE id = 'act-b'"
    ).fetchone()
    assert (act["chapter_start"], act["chapter_end"], act["chapter_count"]) == (3, 4, 2)
