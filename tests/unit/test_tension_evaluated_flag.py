# -*- coding: utf-8 -*-
"""第 82 批（N-01）：张力「有没有真的评过」的三态化守卫。

病根（第 80 批独立验收 N-01）
----------------------------
``chapters`` 四列张力都是 ``REAL DEFAULT 50.0``
⇒ 「从没评过」与「恰好得 50 分」在**存储层完全同形**
⇒ 下游那条「分辨默认 50」的启发式判据（``monitor.py``）永远不成立
   （它要求 ``plot_tension IS NULL``，而该列有 DEFAULT 50.0 ⇒ **恒假**）
⇒ 未评估章节在张力曲线上是货真价实的「平缓 5/10」**假点**，
   并且参与均值/方差统计。

修法（用户拍板）
----------------
加一列 ``tension_evaluated``（**不动现有四列的列契约** ⇒ 可回滚、风险最小）：
``1`` = 真评过；``0`` = 未评估（此时 50.0 只是占位）。

本文件钉住
----------
1. **列存在**（schema + 迁移）；
2. **回填判据**（用真迁移文件在内存库上跑一遍 ⇒ 行为级，不抄判据）；
3. **每个写入点都点亮标记**（漏一个 ⇒ 那条路径写出来的分永远是「未评估」）；
4. **下游读标记**、**不再用旧启发式**；
5. **实体语义**（新建 = 未评估；写真分 = 已评估；未评估哨兵**不**点亮）；
6. **仓储往返**（标记不被悄悄改掉）；
7. **曲线端到端**（未评估章节不进统计）。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]   # ← 仓库根（tests/unit/xxx.py ⇒ parents[2]）
SCHEMA = ROOT / "infrastructure" / "persistence" / "database" / "schema.sql"
MIGRATION = (
    ROOT / "infrastructure" / "persistence" / "database" / "migrations"
    / "add_tension_evaluated_flag.sql"
)

sys.path.insert(0, str(ROOT))


# ── 1. 列存在 ────────────────────────────────────────────────────────────


def test_the_schema_declares_the_flag():
    """⚠ 新建库必须**自带**这列（否则要走迁移才有）。"""
    text = SCHEMA.read_text(encoding="utf-8-sig")
    assert "tension_evaluated" in text, "schema.sql 没声明 tension_evaluated"
    assert "tension_evaluated INTEGER NOT NULL DEFAULT 0" in text, (
        "列定义必须是「NOT NULL + 默认 0（未评估）」——默认值是**安全的一侧**"
    )


def test_the_migration_file_exists_and_adds_the_column():
    """⚠ 老库靠迁移升级 ⇒ 迁移文件必须在，且必须 ADD COLUMN。"""
    text = MIGRATION.read_text(encoding="utf-8-sig")
    assert "ALTER TABLE chapters ADD COLUMN tension_evaluated" in text, (
        "迁移没加列"
    )


# ── 2. 回填判据（行为级：跑真迁移文件）──────────────────────────────────


def _fresh_db_without_the_column() -> sqlite3.Connection:
    """建一个**没有** tension_evaluated 列的 chapters 表（模拟老库）。"""
    con = sqlite3.connect(":memory:")
    con.execute(
        """
        CREATE TABLE chapters (
            id TEXT PRIMARY KEY,
            novel_id TEXT NOT NULL,
            number INTEGER NOT NULL,
            title TEXT,
            content TEXT,
            outline TEXT,
            status TEXT DEFAULT 'draft',
            tension_score REAL DEFAULT 50.0,
            plot_tension REAL DEFAULT 50.0,
            emotional_tension REAL DEFAULT 50.0,
            pacing_tension REAL DEFAULT 50.0
        )
        """
    )
    return con


def _seed(con: sqlite3.Connection) -> None:
    rows = [
        # (id, status, tension, plot, emo, pacing, content) —— 真·未评估
        ("ch-uneval-1", "draft", 50.0, 50.0, 50.0, 50.0, ""),
        ("ch-uneval-2", "draft", 50.0, 50.0, 50.0, 50.0, None),
        # 真·评过、恰好 50 分（其它维度不同 ⇒ 与占位可分辨）
        ("ch-real-50", "completed", 50.0, 52.0, 48.0, 50.0, "正文"),
        # 普通已评估
        ("ch-normal", "completed", 71.0, 72.0, 75.0, 68.0, "正文"),
        # ⚠ 四列全 50 但**已完成且有正文** ⇒ 真·评过（恰好 50/50/50/50）
        #   ⇒ 保守起见必须算「已评估」（宁可多算，也别把真实评分抹掉）
        ("ch-real-all-50", "completed", 50.0, 50.0, 50.0, 50.0, "正文"),
        # ⚠ 四列全 50、draft、但**有正文** ⇒ 也算「评过」（同上，保守）
        ("ch-draft-with-content", "draft", 50.0, 50.0, 50.0, 50.0, "有正文"),
    ]
    for cid, status, t, p, e, pa, content in rows:
        con.execute(
            """
            INSERT INTO chapters (id, novel_id, number, title, content, status,
                                  tension_score, plot_tension, emotional_tension, pacing_tension)
            VALUES (?, 'n1', 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cid, cid, content, status, t, p, e, pa),
        )
    con.commit()


def _run_migration(con: sqlite3.Connection) -> None:
    """跑**项目自己的**迁移器（不是手抄 SQL）。"""
    from infrastructure.persistence.database.migration_runner import (
        apply_migration_files,
    )

    apply_migration_files(con, MIGRATION.parent)
    con.commit()


def test_the_backfill_marks_only_the_never_scored_rows_as_unevaluated():
    """⚠ 回填判据：**四列全 50 + draft + 空正文** ⇒ 未评估；其余 ⇒ 已评估。

    ⚠ 方向是「宁可多算一个已评估」：把真实评分当成没评过 ⇒ 真实数据被抹掉，
      那比多算一个更贵。
    """
    con = _fresh_db_without_the_column()
    _seed(con)
    _run_migration(con)

    flag = {
        row[0]: row[1]
        for row in con.execute("SELECT id, tension_evaluated FROM chapters")
    }
    assert flag["ch-uneval-1"] == 0, "真·未评估（draft + 四列全 50 + 空正文）必须标 0"
    assert flag["ch-uneval-2"] == 0, "真·未评估（content IS NULL）必须标 0"
    assert flag["ch-real-50"] == 1, "真·评过的 50 分必须标 1"
    assert flag["ch-normal"] == 1, "普通已评估必须标 1"
    assert flag["ch-real-all-50"] == 1, "「恰好 50/50/50/50 且已完稿」必须标 1（保守）"
    assert flag["ch-draft-with-content"] == 1, "draft 但有正文 ⇒ 保守标 1"
    con.close()


def test_the_migration_is_idempotent():
    """⚠ 迁移跑两遍不能炸（``duplicate column`` 是良性错误）。"""
    con = _fresh_db_without_the_column()
    _seed(con)
    _run_migration(con)
    _run_migration(con)
    total = con.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]
    assert total == 6, "迁移重跑后行数不该变"
    con.close()


# ── 3. 每个写入点都点亮标记 ──────────────────────────────────────────────

#: 写张力分数的生产写入点 → 必须**同一句 SQL**里点亮标记
TENSION_WRITERS = (
    "application/world/services/chapter_narrative_sync.py",
    "application/engine/services/persistence_queue.py",
    "infrastructure/persistence/database/sqlite_chapter_repository.py",
    "engine/runtime/daemon_host.py",
)


def test_every_tension_writer_lights_the_flag():
    """⚠ 写进库的张力**必须**同时点亮标记。

    ⚠ 漏一个 ⇒ 那条路径写出来的分永远是「未评估」
      ⇒ 章节从曲线上消失（比假点更糟：那是**丢数据**）。

    ⚠ 判据用 **AST**（函数级）而不是正则扫文本：
      有的写入点把 SQL 拼成片段（``daemon_host`` 的 ``set_parts``），
      正则按「整条语句」根本扫不到 ⇒ 守卫会**假绿**。

    ⚠⚠ 第 82 批变异验证（M01/M04 曾经打不红）：判据必须是**逐条 SQL**，
      不能只看「同一个函数里出现过 tension_evaluated」——
      那会让「一个函数里有两条写入、只坏了一条」**逃过去**。
      ⇒ 现在：**整条 SQL**（含 UPDATE/INSERT）自己必须带标记；
        只有「片段式」写入点（拼 set_parts）才按**函数级**判。
    """
    import ast
    import re

    #: 「写张力值」的标记 —— ⚠ **不能**只认 ``tension_score = ?``：
    #:   UPSERT 形状写的是 ``tension_score = excluded.tension_score``（没有 ``?``）
    #:   ⇒ 只认问号 ⇒ **整条 SQL 根本进不了射程**（第 82 批变异验证 M04 的根因）。
    _WRITE = re.compile(
        r"\b(tension_score|plot_tension|emotional_tension|pacing_tension)\s*=(?!=)"
    )

    missing = []
    for rel in TENSION_WRITERS:
        path = ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))

        # ① **所有**「整条 SQL」常量 —— 不管它挂在函数里、类属性上还是模块级
        #    ⚠ 第 82 批变异验证（M04 打不红）：``_SAVE_SQL`` 是**类属性**
        #      ⇒ 只扫函数的话，整条 SQL 根本没进射程 ⇒ 冲突子句丢了标记也没人拦。
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            stmt = node.value
            if not _WRITE.search(stmt):
                continue
            if "UPDATE" not in stmt and "INSERT" not in stmt:
                continue   # 片段（见 ②）
            if "tension_evaluated" not in stmt:
                missing.append(
                    "%s :: 整条 SQL 没带标记 :: %s" % (rel, " ".join(stmt.split())[:90])
                )
            # ⚠ UPSERT 的**冲突子句**也必须带标记
            #   （只写在 INSERT 列表里 ⇒ 已有行被更新时标记不会跟着走 ⇒ 静默丢标记）
            if "DO UPDATE SET" in stmt:
                update_clause = stmt.split("DO UPDATE SET", 1)[1]
                if "tension_evaluated" not in update_clause:
                    missing.append("%s :: ON CONFLICT 子句没更新标记" % rel)

        # ② **片段式**写入点（函数里拼 set_parts 之类）
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            constants = [
                n.value
                for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            joined = "\n".join(constants)
            if not _WRITE.search(joined):
                continue
            if any(
                _WRITE.search(c) and ("UPDATE" in c or "INSERT" in c)
                for c in constants
            ):
                continue   # 整条 SQL 已在 ① 查过
            if "tension_evaluated" not in joined:
                missing.append("%s :: %s()（片段式写入，没点亮标记）" % (rel, node.name))
    assert not missing, "这些写入点没点亮 tension_evaluated：\n  " + "\n  ".join(missing)


# ── 4. 下游读标记，不再用旧启发式 ───────────────────────────────────────


def test_the_monitor_reads_the_flag_and_not_the_old_heuristic():
    """⚠ 下游必须读**标记**；旧启发式（``plot_tension IS NULL``）恒假 ⇒ 禁用。"""
    text = (ROOT / "interfaces" / "api" / "v1" / "workbench" / "monitor.py").read_text(
        encoding="utf-8-sig"
    )
    assert "tension_evaluated" in text, "monitor 没读 tension_evaluated"
    assert "is_default_draft" not in text, (
        "旧的启发式判据还在（它要求 plot_tension IS NULL ⇒ 恒假 ⇒ 未评估章节被当假点）"
    )


# ── 5. 实体语义 ──────────────────────────────────────────────────────────


def _chapter(**kwargs):
    from domain.novel.entities.chapter import Chapter
    from domain.novel.value_objects.novel_id import NovelId

    params = dict(
        id="ch-1",
        novel_id=NovelId("n1"),
        number=1,
        title="第一章",
        content="正文",
    )
    params.update(kwargs)
    return Chapter(**params)


def test_a_new_chapter_is_not_evaluated():
    """⚠ 新建章节 = **没评过**（安全的一侧）。"""
    assert _chapter().tension_evaluated is False


def test_writing_a_real_score_marks_the_chapter_evaluated():
    """⚠ 写真分 ⇒ 本章从此「评过」。"""
    chapter = _chapter()
    chapter.update_tension_score(72.0)
    assert chapter.tension_evaluated is True


def test_the_unevaluated_sentinel_does_not_light_the_flag():
    """⚠ 未评估哨兵（-1）**不是**评分 ⇒ 不许把标记点亮。"""
    from domain.novel.value_objects.tension_dimensions import TensionDimensions

    chapter = _chapter()
    chapter.update_tension_dimensions(TensionDimensions.unevaluated())
    assert chapter.tension_evaluated is False, "哨兵值把「没评过」说成了「评过」"


def test_a_real_dimension_set_lights_the_flag():
    from domain.novel.value_objects.tension_dimensions import TensionDimensions

    chapter = _chapter()
    chapter.update_tension_dimensions(TensionDimensions.from_raw_scores(70, 60, 65))
    assert chapter.tension_evaluated is True


# ── 6. 仓储往返 ──────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path):
    """⚠ 用**临时文件**库（不是 ``:memory:``）。

    ⚠ 为什么：``DatabaseConnection.__init__`` 会跑完整初始化（schema.sql +
    代码级补丁 + SQL 迁移），但 ``:memory:`` 的那个初始化连接在
    ``_ensure_database_exists`` 末尾被 **close** ⇒ 内存库随之消失
    ⇒ 之后 ``get_connection()`` 拿到的是**另一个空库**
    ⇒ 代码级补丁列（``word_count`` / ``generation_hint``）根本不在。
    ⇒ 文件库才是生产同构的做法。
    """
    from infrastructure.persistence.database.connection import DatabaseConnection

    database = DatabaseConnection(str(tmp_path / "tension-flag.db"))
    yield database
    database.close()


def test_the_flag_survives_the_repository_round_trip(db):
    """⚠ 存进去的标记必须原样读回来（不许被默认值吃掉）。"""
    from infrastructure.persistence.database.sqlite_chapter_repository import (
        SqliteChapterRepository,
    )
    from domain.novel.value_objects.chapter_id import ChapterId

    repo = SqliteChapterRepository(db)
    # ⚠ chapters.novel_id 有外键 ⇒ 先有书
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES ('n1', 'N', 'n1', 10)"
    )
    db.get_connection().commit()

    repo.save(_chapter(id="ch-a", number=1, tension_evaluated=False))
    repo.save(_chapter(id="ch-b", number=2, tension_evaluated=True))

    by_id = repo.get_by_id(ChapterId("ch-a"))
    by_id_b = repo.get_by_id(ChapterId("ch-b"))
    assert by_id is not None and by_id.tension_evaluated is False, "未评估的标记丢了"
    assert by_id_b is not None and by_id_b.tension_evaluated is True, "已评估的标记丢了"


# ── 7. 曲线端到端 ────────────────────────────────────────────────────────


def test_the_curve_treats_an_unevaluated_chapter_as_unevaluated(monkeypatch):
    """⚠ 未评估章节 ⇒ 不进统计、点标记为「未评估」（**假点消除**）。

    ⚠ 这一条是本批的**终点**：判定层 → 存储层 → 展示层，一路到接口。
    """
    import interfaces.api.v1.workbench.monitor as monitor

    class _Chapter:
        def __init__(self, number, tension, evaluated):
            self.number = number
            self.tension_score = tension
            self.tension_evaluated = evaluated
            self.title = "第%d章" % number
            self.status = "completed"
            self.plot_tension = tension

    class _Repo:
        def list_by_novel(self, _novel_id):
            return [
                _Chapter(1, 70.0, True),
                _Chapter(2, 50.0, False),   # ← 未评估（占位 50.0）
                _Chapter(3, 60.0, True),
            ]

    monkeypatch.setattr(monitor, "get_chapter_repository", lambda: _Repo())

    curve = monitor._get_tension_curve_sync("n1")
    evaluated = [p for p in curve.points if p.evaluated]
    unevaluated = [p for p in curve.points if not p.evaluated]

    assert [p.chapter for p in unevaluated] == [2], "第 2 章（未评估）没被标成未评估"
    assert [p.chapter for p in evaluated] == [1, 3], "已评估章节被误判成未评估"
    assert curve.stats is not None
    assert curve.stats.evaluated_count == 2, "未评估章节混进了已评估计数"
    assert curve.stats.unevaluated_count == 1
    # 均值只算已评估 ⇒ 70/60 ⇒ 6.5；混进占位 50 会变成 6.0
    assert curve.stats.avg_tension == pytest.approx(6.5), (
        "均值把未评估的占位分算进去了（假点还在污染统计）"
    )
