# -*- coding: utf-8 -*-
"""H-05：宏观结构合并的**风险分级**（fatal / warning）+ node_type 口径修正。

第 59 批核实到的真实病因链（比登记表描述深两层）：

1. **冲突检测在生产里从未触发**（口径 bug，本批最重要的发现）：
   `MacroMergeEngine` 里写死 ``node['node_type'] == 'CHAPTER'``（大写），
   而生产侧传的是 ``node.node_type.value``
   （``domain/structure/story_node.py`` 的 NodeType 值是**小写** ``'chapter'``）
   ⇒ ``_mark_carriers_bottom_up`` 一个 carrier 都标不出来
   ⇒ ``execute_diff`` 的「跳过章节节点」分支也从不触发
   ⇒ **章节结构节点被当成空壳删掉**，且 RED 阻断永不亮。
   而引擎自己的单测用的是大写 fixture ⇒ 「全绿、线上完全失效」。
   证据：``review-briefs/_t59_probe_node_type.txt``（生产形状复刻）。
2. **风险只有两档**（本批要做的那一半）：
   有冲突 ⇒ 一律 fatal ⇒ 整个重构卡死（哪怕那个节点下面只有未动笔的章节）；
   没冲突 ⇒ 照常落库。

⇒ 本批：① 口径归一（**只在比较时**归一，落库的 node_type 保持小写口径）；
        ② 冲突分两档：fatal（有正文 ⇒ 阻断，不变量原样保留）/
        warning（只有未动笔的章节 ⇒ 不阻断、旧节点**保留**、显式提示）。

⚠ 判据不明（缺 ``has_prose`` 位）时一律按 fatal —— 宁可阻断，不可丢稿。
"""
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.audit.services.macro_merge_engine import (
    CONFLICT_SEVERITY_FATAL,
    CONFLICT_SEVERITY_WARNING,
    MacroMergeEngine,
    MergePlan,
)
from application.audit.services.macro_merge_engine import MergeConflictException
from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
)
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.story_node_repository import (
    StoryNodeRepository,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
SCHEMA_PATH = (
    _REPO_ROOT / "infrastructure" / "persistence" / "database" / "schema.sql"
)

NOVEL = "n-h05"


# ───────────────────────────── 形状构造 ─────────────────────────────


def _node(
    node_id,
    node_type,
    *,
    parent_id=None,
    number=0,
    title="",
    order_index=0,
    has_prose=None,
):
    """旧结构节点（生产形状：node_type 小写）。"""
    d = {
        "id": node_id,
        "novel_id": NOVEL,
        "parent_id": parent_id,
        "node_type": node_type,
        "number": number,
        "title": title,
        "description": "",
        "order_index": order_index,
    }
    if has_prose is not None:
        d["has_prose"] = has_prose
    return d


def _old_spine(*, chapter_has_prose, drop_act=False):
    """旧结构：部 → 卷 → 两幕；章节挂在**第二幕**下面。

    新结构只保留一幕 ⇒ 第二幕在 new_map 里找不到 ⇒ 它是 carrier ⇒ 冲突。
    """
    nodes = [
        _node("part-n-h05-1", "part", number=1, title="第一部", order_index=0),
        _node(
            "volume-n-h05-1",
            "volume",
            parent_id="part-n-h05-1",
            number=1,
            title="第一卷",
            order_index=1,
        ),
        _node(
            "act-n-h05-1",
            "act",
            parent_id="volume-n-h05-1",
            number=1,
            title="第一幕",
            order_index=2,
        ),
        _node(
            "act-n-h05-2",
            "act",
            parent_id="volume-n-h05-1",
            number=2,
            title="第二幕",
            order_index=3,
        ),
        _node(
            "chapter-n-h05-1",
            "chapter",
            parent_id="act-n-h05-2",
            number=1,
            title="第1章",
            order_index=4,
            has_prose=chapter_has_prose,
        ),
    ]
    return nodes


def _new_nodes(*, keep_both_acts=False):
    """新结构（生产形状：与旧结构同 id 的宏观节点会被「换皮不换骨」更新）。"""
    acts = [{"title": "第一幕"}, {"title": "第二幕"}] if keep_both_acts else [{"title": "第一幕"}]
    return [
        _node("part-n-h05-1", "part", number=1, title="第一部·新", order_index=0),
        _node(
            "volume-n-h05-1",
            "volume",
            parent_id="part-n-h05-1",
            number=1,
            title="第一卷·新",
            order_index=1,
        ),
        _node(
            "act-n-h05-1",
            "act",
            parent_id="volume-n-h05-1",
            number=1,
            title="第一幕·新",
            order_index=2,
        ),
    ] + (
        [
            _node(
                "act-n-h05-2",
                "act",
                parent_id="volume-n-h05-1",
                number=2,
                title="第二幕·新",
                order_index=3,
            )
        ]
        if keep_both_acts
        else []
    )


# ─────────────────── 1. 口径：生产形状（小写 node_type）必须真的生效 ───────────────────


def test_production_shape_detects_carrier_and_blocks():
    """生产形状（node_type 小写）必须检出 carrier 并阻断。

    这条守卫守的是「第 59 批发现的静默失效」：
    引擎原先只认大写 'CHAPTER' ⇒ carrier 标不出来 ⇒
    章节节点被当空壳删掉、RED 永不亮，而单测全绿。
    """
    plan = MacroMergeEngine(
        _old_spine(chapter_has_prose=True), _new_nodes()
    ).execute_diff()

    assert plan.has_fatal_conflict is True
    assert plan.summary["status"] == "RED"
    assert [c["node_id"] for c in plan.fatal_conflicts] == ["act-n-h05-2"]
    # 章节节点绝不能被当成空壳删掉（那会让正文失去结构归属）
    assert "chapter-n-h05-1" not in plan.deletes
    # 承载章节的节点也不删（删了章节就成了孤儿）
    assert "act-n-h05-2" not in plan.deletes


def test_node_type_case_does_not_change_the_verdict():
    """大小写口径不得改变结论 —— 判据只认「是不是章节」，不认字面量大小写。"""
    lower = MacroMergeEngine(
        _old_spine(chapter_has_prose=True), _new_nodes()
    ).execute_diff()
    upper = MacroMergeEngine(
        [dict(n, node_type=n["node_type"].upper()) for n in _old_spine(chapter_has_prose=True)],
        [dict(n, node_type=n["node_type"].upper()) for n in _new_nodes()],
    ).execute_diff()

    assert lower.summary["status"] == upper.summary["status"]
    assert sorted(lower.deletes) == sorted(upper.deletes)
    assert sorted(c["node_id"] for c in lower.conflicts) == sorted(
        c["node_id"] for c in upper.conflicts
    )


def test_chapter_nodes_are_never_deleted():
    """章节节点不进 deletes —— 合并只管宏观框架，不碰章节本身。"""
    plan = MacroMergeEngine(
        _old_spine(chapter_has_prose=True), _new_nodes()
    ).execute_diff()

    assert "chapter-n-h05-1" not in plan.deletes


# ───────────────────────────── 2. 风险分级 ─────────────────────────────


def test_written_chapter_conflict_is_fatal():
    """有正文 ⇒ fatal ⇒ 阻断重构（登记表不变量：删除含正文节点必须阻断）。"""
    plan = MacroMergeEngine(
        _old_spine(chapter_has_prose=True), _new_nodes()
    ).execute_diff()

    assert plan.has_fatal_conflict is True
    assert plan.warnings == []
    conflict = plan.fatal_conflicts[0]
    assert conflict["severity"] == CONFLICT_SEVERITY_FATAL
    assert conflict["written_chapters"] == 1
    assert conflict["carried_chapters"] == 1
    assert "已有正文" in conflict["reason"]


def test_unwritten_chapter_conflict_is_warning_only():
    """只有未动笔的章节 ⇒ warning ⇒ 不阻断，但旧节点保留 + 显式提示。"""
    plan = MacroMergeEngine(
        _old_spine(chapter_has_prose=False), _new_nodes()
    ).execute_diff()

    assert plan.has_fatal_conflict is False
    assert plan.summary["status"] == "YELLOW"
    assert len(plan.warnings) == 1
    warning = plan.warnings[0]
    assert warning["node_id"] == "act-n-h05-2"
    assert warning["severity"] == CONFLICT_SEVERITY_WARNING
    assert warning["written_chapters"] == 0
    assert warning["carried_chapters"] == 1
    # 旧节点保留（不删 ⇒ 章节不会被孤立），也不替作者搬家
    assert "act-n-h05-2" not in plan.deletes
    assert "chapter-n-h05-1" not in plan.deletes


def test_unknown_prose_evidence_is_treated_as_fatal():
    """判据不明（缺 has_prose）⇒ 按 fatal —— 「没测出来」不等于「可以放行」。"""
    old_nodes = _old_spine(chapter_has_prose=True)
    # 抹掉判据位：模拟调用方没给出「这一章动笔没有」
    for node in old_nodes:
        node.pop("has_prose", None)

    plan = MacroMergeEngine(old_nodes, _new_nodes()).execute_diff()

    assert plan.has_fatal_conflict is True
    assert plan.warnings == []


def test_conflict_without_severity_key_is_treated_as_fatal():
    """缺 severity 的冲突按 fatal —— 分级依据不明 ≠ 可以放行。"""
    plan = MergePlan(
        creates=[],
        updates=[],
        deletes=[],
        conflicts=[{"node_id": "act-1", "reason": "r"}],
        summary={},
    )

    assert plan.has_fatal_conflict is True


def test_has_fatal_conflict_is_derived_not_a_second_truth():
    """has_fatal_conflict 只能由 conflicts 派生 —— 不许存第二份真相。"""
    assert "has_fatal_conflict" not in MergePlan.__dataclass_fields__


# ─────────────────────── 3. 服务层：真库、生产形状 ───────────────────────


@pytest.fixture
def story_db(tmp_path, monkeypatch):
    """真实 schema 的临时库（生产形状：小写 node_type）。"""
    monkeypatch.setenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "1")
    db_path = tmp_path / "h05.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    existing = {r[1] for r in conn.execute("PRAGMA table_info(chapters)")}
    if "word_count" not in existing:
        conn.execute("ALTER TABLE chapters ADD COLUMN word_count INTEGER DEFAULT 0")
    # 015 迁移会把 story_nodes 的进度回填成 chapters 的值；这里预置为已应用，
    # 让「章节节点有没有正文」完全由本 fixture 的插入值决定（不被迁移改写）。
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
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, 'T', 'slug-h05', 10)",
        (NOVEL,),
    )
    conn.commit()
    conn.close()
    return DatabaseConnection(str(db_path))


def _insert_structure(db, *, chapter_word_count, chapter_status):
    """插入旧结构：部 → 卷 → 两幕，第二幕下挂一章。"""
    conn = db.get_connection()
    for nid, parent, ntype, num, title, oi in (
        ("part-n-h05-1", None, "part", 1, "第一部", 0),
        ("volume-n-h05-1", "part-n-h05-1", "volume", 1, "第一卷", 1),
        ("act-n-h05-1", "volume-n-h05-1", "act", 1, "第一幕", 2),
        ("act-n-h05-2", "volume-n-h05-1", "act", 2, "第二幕", 3),
    ):
        conn.execute(
            "INSERT INTO story_nodes (id, novel_id, parent_id, node_type, number, title,"
            " planning_status, planning_source, order_index, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'confirmed', 'ai_macro', ?, '2026-01-01', '2026-01-01')",
            (nid, NOVEL, parent, ntype, num, title, oi),
        )
    conn.execute(
        "INSERT INTO story_nodes (id, novel_id, parent_id, node_type, number, title,"
        " content, word_count, status, planning_status, planning_source, order_index,"
        " created_at, updated_at)"
        " VALUES (?, ?, ?, 'chapter', 1, '第1章', '', ?, ?, 'confirmed', 'ai_act', 4,"
        " '2026-01-01', '2026-01-01')",
        ("chapter-n-h05-1", NOVEL, "act-n-h05-2", chapter_word_count, chapter_status),
    )
    conn.commit()


def _service(db):
    svc = ContinuousPlanningService.__new__(ContinuousPlanningService)
    svc.story_node_repo = StoryNodeRepository(db)
    return svc


def _new_structure():
    """新结构：只保留一幕 ⇒ 第二幕成 carrier。"""
    return [
        {
            "title": "第一部",
            "volumes": [
                {"title": "第一卷", "acts": [{"title": "第一幕"}]},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_service_blocks_merge_when_chapter_has_prose(story_db):
    """章节已有正文 ⇒ 抛 MergeConflictException，且**一个节点都不许落库**。"""
    _insert_structure(story_db, chapter_word_count=2000, chapter_status="completed")
    svc = _service(story_db)
    before = {n.id for n in await svc.story_node_repo.get_by_novel(NOVEL)}

    with pytest.raises(MergeConflictException) as exc_info:
        await svc.confirm_macro_plan_safe(NOVEL, _new_structure())

    conflicts = exc_info.value.conflicts
    assert [c["node_id"] for c in conflicts] == ["act-n-h05-2"]
    assert all(c["severity"] == CONFLICT_SEVERITY_FATAL for c in conflicts)
    # 阻断 ⇒ 结构一个节点都没动
    after = {n.id for n in await svc.story_node_repo.get_by_novel(NOVEL)}
    assert after == before


@pytest.mark.asyncio
async def test_service_reports_warning_and_keeps_old_nodes(story_db):
    """章节还没动笔 ⇒ 不阻断：落库照常，旧节点与章节**都保留**。"""
    _insert_structure(story_db, chapter_word_count=0, chapter_status="draft")
    svc = _service(story_db)

    result = await svc.confirm_macro_plan_safe(NOVEL, _new_structure())

    assert result["success"] is True
    assert len(result["warnings"]) == 1
    warning = result["warnings"][0]
    assert warning["node_id"] == "act-n-h05-2"
    assert warning["severity"] == CONFLICT_SEVERITY_WARNING
    assert warning["written_chapters"] == 0
    assert warning["carried_chapters"] == 1

    ids = {n.id for n in await svc.story_node_repo.get_by_novel(NOVEL)}
    # 旧节点保留 ⇒ 章节不会被孤立
    assert "act-n-h05-2" in ids
    assert "chapter-n-h05-1" in ids


@pytest.mark.asyncio
async def test_service_fatal_payload_excludes_warnings(story_db):
    """阻断载荷里只有 fatal —— warning 不许混进「重构被阻断」的证据里。"""
    conn = story_db.get_connection()
    _insert_structure(story_db, chapter_word_count=2000, chapter_status="completed")
    # 再挂一个只有大纲、没动笔的章节（挂在第一部下）
    conn.execute(
        "INSERT INTO story_nodes (id, novel_id, parent_id, node_type, number, title,"
        " content, word_count, status, planning_status, planning_source, order_index,"
        " created_at, updated_at)"
        " VALUES (?, ?, ?, 'chapter', 2, '第2章', '', 0, 'draft', 'confirmed', 'ai_act', 5,"
        " '2026-01-01', '2026-01-01')",
        ("chapter-n-h05-2", NOVEL, "act-n-h05-1"),
    )
    conn.commit()
    svc = _service(story_db)

    with pytest.raises(MergeConflictException) as exc_info:
        # 新结构把两幕都砍掉 ⇒ 一条 fatal + 一条 warning
        await svc.confirm_macro_plan_safe(
            NOVEL,
            [{"title": "第一部", "volumes": [{"title": "第一卷", "acts": []}]}],
        )

    conflicts = exc_info.value.conflicts
    # 有正文的是第二幕（fatal）；第一幕下面那章没动笔（warning）——
    # warning **不许**混进「重构被阻断」的载荷里。
    assert [c["node_id"] for c in conflicts] == ["act-n-h05-2"]
    assert all(c["severity"] == CONFLICT_SEVERITY_FATAL for c in conflicts)


@pytest.mark.asyncio
async def test_service_warns_through_fallback_and_apply_entrypoints(story_db):
    """warning 档必须一路透传到落库入口的返回值（守护进程 / API 都要看得见）。"""
    _insert_structure(story_db, chapter_word_count=0, chapter_status="draft")
    svc = _service(story_db)

    persist_result = await svc.persist_macro_structure_with_fallback(
        NOVEL, _new_structure()
    )
    assert len(persist_result["warnings"]) == 1
    assert persist_result["warnings"][0]["severity"] == CONFLICT_SEVERITY_WARNING

    apply_result = await svc.apply_macro_plan_from_llm_result(
        {"success": True, "structure": _new_structure()},
        NOVEL,
        10,
    )
    assert len(apply_result["warnings"]) == 1
    assert apply_result["warnings"][0]["severity"] == CONFLICT_SEVERITY_WARNING


# ─────────────────────── 4. 守护进程：warning 必须有人看见 ───────────────────────


def _macro_invocation_outcome() -> SimpleNamespace:
    return SimpleNamespace(
        status="completed",
        session_id="session-1",
        operation="autopilot.macro.plan",
        node_key="planning-quick-macro",
        autopilot_pause_reason="",
        payload={
            "commit": SimpleNamespace(
                result={"continuation": {"macro_plan": {"success": True, "structure": []}}}
            )
        },
    )


@pytest.mark.asyncio
async def test_daemon_surfaces_merge_warnings(monkeypatch):
    """守护进程落库后必须把 warning 打进日志（不阻断，但不许静默）。"""
    from engine.runtime import macro_planning_delegate

    fake_logger = MagicMock()
    monkeypatch.setattr(macro_planning_delegate, "logger", fake_logger)

    host = MagicMock()
    host._is_still_running.return_value = True
    host.planning_service.apply_macro_plan_from_llm_result = AsyncMock(
        return_value={
            "success": True,
            "created_nodes": 3,
            "warnings": [
                {
                    "node_id": "act-n-h05-2",
                    "title": "第二幕",
                    "severity": CONFLICT_SEVERITY_WARNING,
                }
            ],
        }
    )
    novel = MagicMock()
    novel.novel_id.value = "n-1"
    novel.target_chapters = 12
    novel.auto_approve_mode = False

    monkeypatch.setattr(
        macro_planning_delegate, "_read_shared_state", lambda _novel_id: {}
    )
    monkeypatch.setattr(
        macro_planning_delegate,
        "_request_macro_invocation",
        AsyncMock(return_value=_macro_invocation_outcome()),
    )

    await macro_planning_delegate.run_macro_planning(host, novel)

    warning_calls = [
        call for call in fake_logger.warning.call_args_list if "第二幕" in str(call)
    ]
    assert warning_calls, "warning 必须在日志里出现（否则作者看不到旧节点被保留）"


# ─────────────────────── 5. API 出口：warning 必须回前端 ───────────────────────


@pytest.mark.asyncio
async def test_confirm_macro_plan_route_returns_warnings():
    """确认接口的成功响应必须带 warnings（前端据此提示作者）。"""
    from interfaces.api.v1.blueprint.continuous_planning_routes import (
        MacroPlanConfirmRequest,
        confirm_macro_plan,
    )

    service = MagicMock()
    service.confirm_macro_plan_safe = AsyncMock(
        return_value={
            "success": True,
            "summary": {"status": "YELLOW"},
            "warnings": [
                {
                    "node_id": "act-n-h05-2",
                    "title": "第二幕",
                    "severity": CONFLICT_SEVERITY_WARNING,
                }
            ],
        }
    )

    result = await confirm_macro_plan(
        "n-1",
        MacroPlanConfirmRequest(structure=[{"title": "第一部"}]),
        service,
    )

    assert len(result["warnings"]) == 1
    assert result["warnings"][0]["severity"] == CONFLICT_SEVERITY_WARNING


# ─────────────────────── 6. 前端：warning 必须被显示出来 ───────────────────────


def test_frontend_surfaces_merge_warnings():
    """前端必须在写入成功后把 warning 显示出来（不许静默吞掉）。"""
    modal = (
        _REPO_ROOT / "frontend" / "src" / "components" / "workbench" / "MacroPlanModal.vue"
    ).read_text(encoding="utf-8")
    planning = (_REPO_ROOT / "frontend" / "src" / "api" / "planning.ts").read_text(
        encoding="utf-8"
    )

    assert "export interface MacroMergeWarning" in planning
    assert "MacroMergeWarning" in modal
    # 必须真的**从响应里读** warnings（不是自己造一个空数组）
    assert "result?.warnings" in modal
    # 必须走 warning 提示分支（锚到确切语句：文件里另有一处 message.warning，
    # 只查 "message.warning" 出现过 ⇒ 换成 message.success 也照样绿）
    assert (
        "      message.warning(\n"
        "        `结构已写入，但有 ${warnings.length} 个旧节点仍承载未动笔的章节，已保留待你手动处理`,"
    ) in modal
