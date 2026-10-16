"""引擎写作路径必须让 story_nodes 章节节点反映真实写作进度。

2026-10-16 实测的故障形态（本文件复刻）：

- 全库 587 个章节节点，``story_nodes.word_count > 0`` 的数量为 **0**；
- 引擎写作路径只写 ``chapters`` 表，从不回写 ``story_nodes``；
- ``act_planning_delegate`` 的闸门 0 / pending、以及
  ``ContinuousPlanningService._count_written_chapters_in_act`` 全部用
  ``story_nodes.word_count`` 判断「这一章动笔没有」，于是恒判「全部未写」。

后果链：上一幕永远有「未动笔章节」→ 新幕开不出来 → 写作找不到下一章 →
``_current_act_fully_written`` 把「找不到这一幕」当成「这一幕写完了」→
``novel.current_act`` 每 tick +1 棘轮式暴涨（线上实测 174 → 182）。

本文件刻意**不手工给替身塞 word_count**：上一版测试
（``test_chapter_budget_ceiling.py``）用 ``_node(..., word_count=2000)``
预设了「已写章节 word_count>0」这一生产环境永不成立的前提，
于是 1929 条全绿而线上仍在死循环。这里一律走真实 SQLite 落库。
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.novel.value_objects.novel_id import NovelId
from engine.runtime.act_planning_delegate import run_act_planning
from engine.runtime.daemon_host import DaemonHostMixin
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)
from infrastructure.persistence.database.story_node_repository import (
    StoryNodeRepository,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "infrastructure"
    / "persistence"
    / "database"
    / "schema.sql"
)

NOVEL_ID = "n1"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """真实 schema 的临时库。

    结构刻意做成生产形态：2 幕各 1 章，章节行 status='completed' 且有正文，
    但 story_nodes 章节节点的 word_count **保持默认 0**——这正是线上 587/587
    的真实状态。手改这个字段会让测试失真。
    """
    monkeypatch.setenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "1")
    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    # word_count 由迁移追加，不在基表 schema.sql 里；生产库有，这里补齐
    existing = {r[1] for r in conn.execute("PRAGMA table_info(chapters)")}
    if "word_count" not in existing:
        conn.execute("ALTER TABLE chapters ADD COLUMN word_count INTEGER DEFAULT 0")
    # 015 迁移就是「把存量脏数据回填掉」的那一支。它会在 DatabaseConnection
    # 初始化时被迁移器自动应用，一旦应用，本 fixture 造出的 word_count=0 会被
    # 就地改写成 2000 —— 对照用例的前提（镜像陈旧）被抹平，正向用例也会退化成
    # 空洞断言（删掉同步代码照样绿）。
    # 因此预置 015 为已应用，让迁移器跳过它，fixture 保持「迁移前」的真实形态。
    # migrations_applied 同样不在基表 schema.sql 里，由迁移器创建，这里先建好。
    conn.execute(
        "CREATE TABLE IF NOT EXISTS migrations_applied ("
        " migration_file TEXT PRIMARY KEY,"
        " applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO migrations_applied (migration_file, applied_at)"
        " VALUES ('015_backfill_chapter_node_progress.sql', '2026-01-01 00:00:00')"
    )
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, 'T', 'slug1', 10)",
        (NOVEL_ID,),
    )
    conn.execute(
        "INSERT INTO story_nodes (id, novel_id, parent_id, node_type, number, title,"
        " planning_status, planning_source, order_index, created_at, updated_at)"
        " VALUES ('vol-1', ?, NULL, 'volume', 1, '卷一', 'confirmed', 'ai_macro', 0,"
        " '2026-01-01', '2026-01-01')",
        (NOVEL_ID,),
    )
    for act_no, chapter_no in ((1, 1), (2, 2)):
        conn.execute(
            "INSERT INTO story_nodes (id, novel_id, parent_id, node_type, number, title,"
            " planning_status, planning_source, order_index, created_at, updated_at)"
            " VALUES (?, ?, 'vol-1', 'act', ?, ?, 'confirmed', 'ai_act', 0,"
            " '2026-01-01', '2026-01-01')",
            (f"act-{act_no}", NOVEL_ID, act_no, f"第{act_no}幕"),
        )
        conn.execute(
            "INSERT INTO story_nodes (id, novel_id, parent_id, node_type, number, title,"
            " planning_status, planning_source, order_index, created_at, updated_at)"
            " VALUES (?, ?, ?, 'chapter', ?, ?, 'confirmed', 'ai_act', 0,"
            " '2026-01-01', '2026-01-01')",
            (f"chapter-{chapter_no}", NOVEL_ID, f"act-{act_no}", chapter_no, f"第{chapter_no}章"),
        )
        conn.execute(
            "INSERT INTO chapters (id, novel_id, number, title, content, outline, status,"
            " word_count, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, '', 'completed', 2000, '2026-01-01', '2026-01-01')",
            (f"chapter-{chapter_no}", NOVEL_ID, chapter_no, f"第{chapter_no}章", "正文" * 1000),
        )
    conn.commit()
    conn.close()
    return DatabaseConnection(str(db_path))


def _host(db):
    host = DaemonHostMixin()
    host.story_node_repo = StoryNodeRepository(db)
    host.chapter_repository = SqliteChapterRepository(db)
    # 只桩掉与本用例无关的副作用，闸门逻辑走真实实现
    host._is_still_running = lambda novel: True
    host._flush_novel = lambda novel: None
    host._update_shared_state = lambda *a, **k: None
    host._clear_llm_error_summary = lambda *a, **k: None
    host._count_completed_chapters = lambda nid: 2
    return host


# ── 缺陷 A：找不到这一幕 ≠ 这一幕写完了 ──────────────────────────────


@pytest.mark.asyncio
async def test_missing_act_node_is_not_reported_as_act_fully_written(db):
    """``current_act`` 越过真实幕数时，不得宣告「本幕已写完」。

    线上形态：结构树只有 2 幕而 ``current_act`` 已被棘轮推到 182。
    旧实现找不到 ``current_act + 1`` 的幕节点就 ``return True``，
    ``writing_delegate`` 随即 ``current_act += 1``，每 10 秒涨一次。
    """
    host = _host(db)
    novel = SimpleNamespace(novel_id=NovelId(NOVEL_ID), current_act=182)

    assert await host._current_act_fully_written(novel) is False


@pytest.mark.asyncio
async def test_act_with_completed_chapters_is_still_reported_as_written(db):
    """对照组：真实存在且已写完的幕，判定不能被上面那条改动带歪。"""
    host = _host(db)
    novel = SimpleNamespace(novel_id=NovelId(NOVEL_ID), current_act=1)  # 第 2 幕

    assert await host._current_act_fully_written(novel) is True


# ── 缺陷 B：章节节点必须反映真实写作进度 ────────────────────────────


@pytest.mark.asyncio
async def test_sync_writes_chapter_progress_onto_story_node(db):
    """章节写完后，story_nodes 章节节点必须带上真实字数与状态。

    这是闸门 0 / pending / ``_count_written_chapters_in_act`` 共同依赖的
    字段。引擎写作路径不回写它，这三个判定点就永久失明。
    """
    from application.blueprint.services.story_node_progress_sync import (
        sync_chapter_node_progress,
    )

    await sync_chapter_node_progress(
        host_repo := StoryNodeRepository(db),
        NOVEL_ID,
        2,
        word_count=2000,
        status="completed",
    )

    nodes = {n.number: n for n in await host_repo.get_by_novel(NOVEL_ID)}
    assert nodes[2].word_count == 2000
    assert nodes[2].status == "completed"


@pytest.mark.asyncio
async def test_sync_is_a_noop_when_chapter_node_absent(db):
    """没有对应章节节点时不得抛异常——正文可能先于结构落库。"""
    from application.blueprint.services.story_node_progress_sync import (
        sync_chapter_node_progress,
    )

    synced = await sync_chapter_node_progress(
        StoryNodeRepository(db), NOVEL_ID, 99, word_count=100, status="completed"
    )
    assert synced is False


# ── 缺陷 C：端到端——同步后闸门 0 必须放行下一幕 ──────────────────────


@pytest.mark.asyncio
async def test_act_planning_blocks_next_act_while_node_progress_stale(db, monkeypatch):
    """对照组：结构节点进度未同步时，闸门 0 必须拦住开新幕。

    这条锁住缺陷本身——它同时证明下面那条断言不是恒真的。
    """
    from engine.runtime import act_planning_delegate

    host = _host(db)  # 未做任何同步，节点 word_count 仍为 0
    host.planning_service = SimpleNamespace(create_next_act_auto=_async_return({}))
    monkeypatch.setattr(act_planning_delegate, "_read_shared_state", lambda _nid: {})

    novel = SimpleNamespace(
        novel_id=NovelId(NOVEL_ID),
        target_chapters=10,
        current_act=2,  # 要开第 3 幕
        current_chapter_in_act=0,
        current_stage=None,
        autopilot_status=None,
    )

    await run_act_planning(host, novel)

    assert host.planning_service.create_next_act_auto.await_count == 0
    assert novel.current_stage is not None and novel.current_stage.name == "WRITING"


@pytest.mark.asyncio
async def test_act_planning_opens_next_act_after_progress_synced(db, monkeypatch):
    """上一幕章节已写完且进度已同步时，幕级规划必须能开下一幕。

    这是线上那本书卡死的直接复刻：2 幕写满，current_act=2（要开第 3 幕），
    但因为节点 word_count 停在 0，闸门 0 永远判定「上一幕尚有 1 章未动笔」
    而把状态打回 WRITING，新幕永远开不出来。
    """
    from application.blueprint.services.story_node_progress_sync import (
        sync_chapter_node_progress,
    )
    from engine.runtime import act_planning_delegate

    host = _host(db)
    # 引擎写作路径的同步动作：把真实进度写回章节节点
    await sync_chapter_node_progress(
        host.story_node_repo, NOVEL_ID, 2, word_count=2000, status="completed"
    )

    host.planning_service = SimpleNamespace(
        create_next_act_auto=_async_return(
            {"success": True, "next_act": {"parent_id": "vol-1", "number": 3}}
        )
    )
    monkeypatch.setattr(act_planning_delegate, "_read_shared_state", lambda _nid: {})

    novel = SimpleNamespace(
        novel_id=NovelId(NOVEL_ID),
        target_chapters=10,
        current_act=2,  # 要开第 3 幕
        current_chapter_in_act=0,
        current_stage=None,
        autopilot_status=None,
    )

    await run_act_planning(host, novel)

    assert host.planning_service.create_next_act_auto.await_count == 1, (
        "闸门 0 把已经写完的第 2 幕当成还有章没写，新幕开不出来"
    )


def _async_return(value):
    from unittest.mock import AsyncMock

    return AsyncMock(return_value=value)


# ── 接线：引擎写作路径必须真的调用同步 ──────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_save_chapter_syncs_story_node_progress(db):
    """``_step_save_chapter`` 落库后必须自动回写结构节点。

    没有这条，``sync_chapter_node_progress`` 只是一个没人调用的工具函数，
    闸门 0 在真实运行中依旧失明——那正是修复前的样子。
    """
    from engine.pipeline.base import BaseStoryPipeline
    from engine.pipeline.context import PipelineContext

    story_repo = StoryNodeRepository(db)
    chapter_repo = SqliteChapterRepository(db)

    pipeline = BaseStoryPipeline()
    # 走 repository 直写分支，避免测试依赖跨进程 CQRS 队列
    pipeline._push_persistence_command = lambda ctx: False

    ctx = PipelineContext(novel_id=NOVEL_ID, chapter_number=2)
    ctx.inject(
        chapter_repository=chapter_repo,
        story_node_repo=story_repo,
        novel_repository=None,
        llm_service=None,
    )
    ctx.chapter_content = "正文" * 1000
    ctx.word_count = 2000
    ctx.outline = ""

    result = await pipeline._step_save_chapter(ctx)
    assert result.passed, f"章节保存失败：{result.message}"

    nodes = {n.number: n for n in await story_repo.get_by_novel(NOVEL_ID)}
    assert nodes[2].word_count == 2000, "管线保存章节后没有回写结构节点字数"
    assert nodes[2].status == "completed", "管线保存章节后没有回写结构节点状态"


@pytest.mark.asyncio
async def test_daemon_ephemeral_save_also_syncs_story_node(db):
    """daemon/legacy 写入路径（改写、强制置 completed）同样必须镜像进度。

    该方法经 CQRS 队列推裸 SQL，因此同步也走同一队列、保持单写者语义。
    """
    from engine.runtime.daemon_host import DaemonHostMixin

    host = DaemonHostMixin()
    pushed: list = []
    host._queue_sql = lambda sql, params=(): pushed.append((sql, list(params))) or True

    host._save_chapter_ephemeral(
        NOVEL_ID, 2, content="正文" * 500, status="completed", word_count=2000
    )

    node_updates = [s for s, _ in pushed if "story_nodes" in s]
    assert node_updates, "_save_chapter_ephemeral 没有回写 story_nodes"
    assert "word_count = ?" in node_updates[0] and "status = ?" in node_updates[0]
