"""故事线 provenance 必须走完「写 → 存 → 读 → 改 → 暴露」五段（登记表未处理项 3/4/5/6）。

## 问题

`storylines.estimated_chapter_end_source` / `_note` 这一对 G-03 provenance
只在**写入**那一段是好的，后四段都漏：

| 段 | 位置 | 症状 |
|---|---|---|
| 写 | `chapter_narrative_sync` / `state_updater` | ✅ 带 source/note |
| 存 | `SqliteStorylineRepository.save()` 的自愈 ALTER | ❌ **漏两列** —— 老库上 INSERT 直接失败 |
| 存 | 持久化队列载荷 | ❌ 生产者不序列化、重建不读 ⇒ 落库永远是「未标注」 |
| 读 | 队列重建 `Storyline(...)` | ❌ 不传 ⇒ 实体默认空串 |
| 改 | `PUT /storylines/{id}` | ❌ 改了 end 不改来源 ⇒ **落库后的假 provenance** |
| 暴露 | `StorylineResponse` | ❌ 不含这两个字段 ⇒ 读者无从分辨 |

「落库后的假来源」比「没有来源」更坏：note 里写着「模型给的 8」而库里存的是
用户改的 42，下游会把它当可信 provenance 消费。

## 口径

**值本身就是唯一事实源**，来源词表在 domain 层
（`domain/novel/value_objects/storyline_end_source.py`）：
`ai` / `ai_adjusted` / `fallback` / `manual` / `''`（未标注，**不等于** `ai`）。
"""
import ast
import pathlib
import sqlite3
import sys

import pytest

from domain.novel.entities.storyline import Storyline
from domain.novel.value_objects.novel_id import NovelId
from domain.novel.value_objects.storyline_end_source import (
    ALL_SOURCES,
    SOURCE_AI,
    SOURCE_AI_ADJUSTED,
    SOURCE_FALLBACK,
    SOURCE_MANUAL,
    SOURCE_UNMARKED,
)
from domain.novel.value_objects.storyline_role import StorylineRole
from domain.novel.value_objects.storyline_status import StorylineStatus
from domain.novel.value_objects.storyline_type import StorylineType
from infrastructure.persistence.database.sqlite_storyline_repository import (
    SqliteStorylineRepository,
)


def _repo_root() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists() or (parent / "pytest.ini").exists():
            return parent
    raise RuntimeError("找不到仓库根")


ROOT = _repo_root()


def _storyline_ddl() -> str:
    """从**真实** schema.sql 截 DDL —— 复刻替身必然漂移。"""
    sql = (ROOT / "infrastructure/persistence/database/schema.sql").read_text(
        encoding="utf-8-sig"
    )
    chunks = []
    for stmt in sql.split(";"):
        code = "\n".join(
            ln for ln in stmt.splitlines() if not ln.lstrip().startswith("--")
        ).strip()
        head = code.upper()
        if not head.startswith("CREATE TABLE IF NOT EXISTS"):
            continue
        if " STORYLINES (" in head or " STORYLINE_MILESTONES (" in head:
            chunks.append(code)
    assert len(chunks) >= 2, f"没截到 storylines DDL（找到 {len(chunks)} 段）"
    return ";\n".join(chunks) + ";"


def _make_db(drop_provenance: bool = False):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_storyline_ddl())
    if drop_provenance:
        # 造一个「迁移没跑过」的老库：真实 DDL 减去 G-03 那两列
        conn.execute("ALTER TABLE storylines DROP COLUMN estimated_chapter_end_source")
        conn.execute("ALTER TABLE storylines DROP COLUMN estimated_chapter_end_note")
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

    return _DB(), conn


def _sl(sid="sl-1", end=42, source=SOURCE_AI, note="模型给的 42"):
    return Storyline(
        id=sid,
        novel_id=NovelId("n-1"),
        storyline_type=StorylineType.MAIN_PLOT,
        status=StorylineStatus.ACTIVE,
        estimated_chapter_start=1,
        estimated_chapter_end=end,
        estimated_chapter_end_source=source,
        estimated_chapter_end_note=note,
        name="主线",
    )


# ======================================================================
#  1. 存储：自愈 ALTER 必须补齐两列（第 6 条）
# ======================================================================


def test_save_works_on_a_legacy_db_without_the_provenance_columns():
    """老库（迁移没跑过）上 save() 不能失败，且 provenance 要真落进去。

    此前自愈 ALTER 补了 7 列，独缺这两列 —— 而 INSERT 语句里带着它们，
    于是老库上整个故事线保存路径直接抛「no such column」。
    """
    db, conn = _make_db(drop_provenance=True)
    cols_before = {r[1] for r in conn.execute("PRAGMA table_info(storylines)")}
    assert "estimated_chapter_end_source" not in cols_before
    assert "estimated_chapter_end_note" not in cols_before

    repo = SqliteStorylineRepository(db)
    repo.save(_sl(source=SOURCE_AI, note="模型给的 42"))

    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(storylines)")}
    assert "estimated_chapter_end_source" in cols_after, "自愈没有补 source 列"
    assert "estimated_chapter_end_note" in cols_after, "自愈没有补 note 列"

    back = repo.get_by_id("sl-1")
    assert back is not None, "故事线没存进去"
    assert back.estimated_chapter_end_source == SOURCE_AI
    assert back.estimated_chapter_end_note == "模型给的 42"


def test_self_heal_does_not_default_the_source_to_ai():
    """自愈补的列缺省必须是空串（未标注），不是 'ai'。

    默认填 'ai' 会把未标注的旧数据读成「这是模型的判断」。

    ⚠ 断言落在**schema 的列默认值**上，不是行的值 ——
    `save()` 的 INSERT 永远显式写出这两列，所以列默认值在仓储路径上
    根本不会被触发，只改 DEFAULT 是**不可观测的**变异。
    要让它可观测，就得查 `PRAGMA table_info` 的 `dflt_value`。
    """
    db, conn = _make_db(drop_provenance=True)
    repo = SqliteStorylineRepository(db)
    repo.save(_sl(sid="sl-unmarked", source=SOURCE_UNMARKED, note=SOURCE_UNMARKED))

    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(storylines)")}
    src_default = cols["estimated_chapter_end_source"][4]
    note_default = cols["estimated_chapter_end_note"][4]
    assert src_default in ("''", '""'), (
        f"source 列的默认值是 {src_default!r}，应是空串"
    )
    assert note_default in ("''", '""'), (
        f"note 列的默认值是 {note_default!r}，应是空串"
    )

    row = conn.execute(
        "SELECT estimated_chapter_end_source FROM storylines WHERE id = ?",
        ("sl-unmarked",),
    ).fetchone()
    assert row[0] == "", f"未标注被落成了 {row[0]!r}"


# ======================================================================
#  2. 队列：载荷与重建都得带（第 3 条）
# ======================================================================


def test_queue_payload_carries_the_provenance():
    """生产端不序列化 ⇒ 队列里根本没有这两个字段，重建读到的是空串。"""
    src = (ROOT / "application/world/services/chapter_narrative_sync.py").read_bytes().decode("utf-8-sig")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if "estimated_chapter_end" in keys and "progress_summary" in keys:
                if "estimated_chapter_end_source" in keys and "estimated_chapter_end_note" in keys:
                    found = True
    assert found, "推进队列的故事线载荷里没有 source/note —— provenance 在这一段就丢了"


def test_queue_reconstruction_reads_the_provenance():
    """重建端不读 ⇒ 实体默认空串，落库后永远「未标注」。"""
    src = (ROOT / "application/engine/services/persistence_queue.py").read_bytes().decode("utf-8-sig")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name == "Storyline":
                kws = {k.arg for k in node.keywords}
                if "estimated_chapter_end" in kws:
                    if "estimated_chapter_end_source" in kws and "estimated_chapter_end_note" in kws:
                        found = True
    assert found, "队列重建 Storyline 时没有传 source/note"


# ======================================================================
#  3. 人工改 end：来源必须跟着改（第 4 条）
# ======================================================================


def test_manual_end_change_marks_the_source_manual():
    """改了取值就必须改来源 —— 否则是**落库后的假 provenance**。"""
    from interfaces.api.v1.engine.generation import _mark_storyline_end_manual

    sl = _sl(end=8, source=SOURCE_AI, note="模型给的 8")
    _mark_storyline_end_manual(sl, 42)

    assert sl.estimated_chapter_end == 42
    assert sl.estimated_chapter_end_source == SOURCE_MANUAL
    assert "42" in sl.estimated_chapter_end_note, "note 必须自述最终采用值"
    assert "8" in sl.estimated_chapter_end_note, "note 应记下改之前的值"
    assert SOURCE_AI in sl.estimated_chapter_end_note, "note 应记下改之前的来源"


def test_manual_note_cites_the_stored_chapter():
    """note 里提到的章号必须就是真正落库的那个（否则又是假 provenance）。"""
    from interfaces.api.v1.engine.generation import _mark_storyline_end_manual

    sl = _sl(end=8, source=SOURCE_AI, note="模型给的 8")
    _mark_storyline_end_manual(sl, 42)

    cited = set(__import__("re").findall(r"第 (\d+) 章", sl.estimated_chapter_end_note))
    assert cited == {"42", "8"}
    assert str(sl.estimated_chapter_end) in cited


def test_user_created_storyline_is_marked_manual():
    """用户自己建的故事线，收尾章是人给的，不是模型判的。"""
    from domain.novel.services.storyline_manager import StorylineManager

    class _Repo:
        def __init__(self): self.saved = None
        def save(self, sl): self.saved = sl

    repo = _Repo()
    mgr = StorylineManager(repo)
    sl = mgr.create_storyline(
        novel_id=NovelId("n-1"),
        storyline_type=StorylineType.MAIN_PLOT,
        estimated_chapter_start=1,
        estimated_chapter_end=30,
    )

    assert sl.estimated_chapter_end_source == SOURCE_MANUAL
    assert sl.estimated_chapter_end_note


# ======================================================================
#  4. API 暴露（第 5 条）
# ======================================================================


def test_storyline_response_exposes_the_provenance():
    """读者只看得到「第 42 章收尾」时，分不出那是模型判的还是人工改的。"""
    from interfaces.api.v1.engine.generation import StorylineResponse, _storyline_to_response

    fields = StorylineResponse.model_fields
    assert "estimated_chapter_end_source" in fields
    assert "estimated_chapter_end_note" in fields

    resp = _storyline_to_response(_sl(source=SOURCE_AI_ADJUSTED, note="模型给的 8，抬到 42"))
    assert resp.estimated_chapter_end_source == SOURCE_AI_ADJUSTED
    assert "42" in resp.estimated_chapter_end_note


def test_unmarked_storyline_reports_empty_source_not_ai():
    """未标注必须是空串 —— 默认填 'ai' 会把旧数据读成「这是模型的判断」。"""
    from interfaces.api.v1.engine.generation import _storyline_to_response

    resp = _storyline_to_response(_sl(source=SOURCE_UNMARKED, note=SOURCE_UNMARKED))
    assert resp.estimated_chapter_end_source == SOURCE_UNMARKED
    assert resp.estimated_chapter_end_source != SOURCE_AI


# ======================================================================
#  5. 来源词表在 domain 层（分层纪律）
# ======================================================================


def test_source_vocabulary_lives_in_the_domain_layer():
    """domain 不得反向依赖 application —— 词表必须放 domain。"""
    from domain.novel.value_objects import storyline_end_source

    assert storyline_end_source.SOURCE_AI == "ai"
    assert SOURCE_AI in ALL_SOURCES
    assert SOURCE_AI_ADJUSTED in ALL_SOURCES
    assert SOURCE_FALLBACK in ALL_SOURCES
    assert SOURCE_MANUAL in ALL_SOURCES
    assert SOURCE_UNMARKED not in ALL_SOURCES, "「未标注」不是一种来源"


def test_domain_layer_does_not_import_application():
    """`storyline_manager`（domain）要用手工来源常量，不能从 application 取。"""
    for rel in (
        "domain/novel/services/storyline_manager.py",
        "domain/novel/entities/storyline.py",
    ):
        tree = ast.parse((ROOT / rel).read_bytes().decode("utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("application"), (
                    f"{rel} 反向依赖了 application.{node.module}"
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("application"), (
                        f"{rel} 反向依赖了 {alias.name}"
                    )


def test_foreshadow_pacing_reexports_the_domain_constants():
    """既有 import 路径要继续可用。"""
    from application.world.services import foreshadow_pacing

    assert foreshadow_pacing.SOURCE_AI == SOURCE_AI
    assert foreshadow_pacing.SOURCE_AI_ADJUSTED == SOURCE_AI_ADJUSTED
    assert foreshadow_pacing.SOURCE_FALLBACK == SOURCE_FALLBACK
