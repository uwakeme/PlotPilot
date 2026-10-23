"""D-07：恢复时改写大纲 —— **原大纲保留 + 用户看得见**。

背景
----
登记表 D-07 行：「中断恢复静默改写大纲」——
写作中断恢复时会把章节 outline **重置为幕级轻量大纲**、清空正文、word_count=0。

第 1 批已修的部分（本次不动）：
- 没有可信替代大纲时**保留原大纲**（初值从 ``""`` 改成 ``None``）；
- 丢弃只发生在「真被打断」或「启停」时（常规 tick 不丢）；
- 清理失败改记 warning，不再 ``logger.debug`` 吞掉。

本次补的部分（登记表「该谁定」列：**恢复时保留已确认 outline，或 UI 显式提示改写**）：

⚠ 「保留已确认 outline」此前**只覆盖了「没有替代大纲」那一种情形**。
**有**幕级 ``act_chapter_plan`` 时（也就是真的会改写的那条路），
用户的七段剧本被**直接覆盖**、原文凭空消失，作者既看不到、也恢复不了。

⇒ 本次两条都做：
1. **保留**：覆盖前把原大纲原文存进 ``story_nodes.metadata['outline_rewritten']``；
2. **提示**：前端读到该记号就显式告诉作者「这一章的大纲被换过了，原文在此」。

守卫要点
--------
- 后端行为级：改写时原大纲**必须**被留下；不留下就红；
- 「没有替代大纲」那条路**不许**留记号（不许制造噪音）；
- 替换前后一样 ⇒ 不留记号；
- 前端**文件内容级**断言（前端零测试设施 ⇒ 只能这样，局限写在文件头）。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from application.engine.services.autopilot_recovery_policy import (
    OUTLINE_REWRITTEN_KEY,
    OUTLINE_REWRITTEN_SOURCE,
    AutopilotRecoveryPolicy,
)

ROOT = Path(__file__).resolve().parents[4]
FRONTEND_PANEL = ROOT / "frontend/src/components/workbench/ChapterContentPanel.vue"


#: 幕级 ``act_chapter_plan`` 的最小可用形状（``render_lightweight_act_chapter_outline``
#: 读的是 main_event / handoff_* 这些字段，不是 beats）。
_ACT_PLAN = {
    "number": 3,
    "main_event": "主角追查线索",
    "handoff_from_previous": "承接上一章",
    "handoff_to_next": "交给下一章",
}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE story_nodes (
            id TEXT PRIMARY KEY,
            novel_id TEXT,
            node_type TEXT,
            number INTEGER,
            outline TEXT,
            metadata TEXT,
            updated_at TEXT
        );
        CREATE TABLE chapters (
            novel_id TEXT,
            number INTEGER,
            outline TEXT,
            content TEXT,
            word_count INTEGER,
            status TEXT,
            updated_at TEXT
        );
        CREATE TABLE variable_values (
            scope_key TEXT,
            is_current INTEGER,
            source_node_key TEXT,
            variable_key TEXT
        );
        """
    )
    return conn


def _seed(
    conn: sqlite3.Connection,
    *,
    chapter_outline: str = "一、情节点1\n二、情节点2\n三、情节点3",
    node_outline: str = "一、情节点1\n二、情节点2\n三、情节点3",
    act_plan: dict | None = None,
    preplan: dict | None = None,
) -> None:
    metadata: dict = {}
    if act_plan is not None:
        metadata["act_chapter_plan"] = act_plan
    if preplan is not None:
        metadata["chapter_preplan"] = preplan
    conn.execute(
        "INSERT INTO story_nodes (id, novel_id, node_type, number, outline, metadata)"
        " VALUES ('n1', 'b1', 'chapter', 3, ?, ?)",
        (node_outline, json.dumps(metadata, ensure_ascii=False)),
    )
    conn.execute(
        "INSERT INTO chapters (novel_id, number, outline, content, word_count, status)"
        " VALUES ('b1', 3, ?, '旧正文', 123, 'writing')",
        (chapter_outline,),
    )
    conn.commit()


def _policy(conn: sqlite3.Connection) -> AutopilotRecoveryPolicy:
    class _Db:
        def fetch_one(self, sql, params=()):
            row = conn.execute(sql, params).fetchone()
            # ⚠ 生产用的是 ``DatabaseConnection``，它返回的是能 ``.get()`` 的行对象；
            # 这里用 ``sqlite3.Row`` ⇒ 必须包一层，让 ``.get()`` 可用。
            return dict(row) if row is not None else None

        def execute(self, sql, params=()):
            return conn.execute(sql, params)

        def commit(self):
            conn.commit()

    return AutopilotRecoveryPolicy(db=_Db())


def _node_metadata(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT metadata FROM story_nodes WHERE id = 'n1'"
    ).fetchone()
    return json.loads(row["metadata"] or "{}")


# ── 1) 改写时原大纲必须被留下 ────────────────────────────────────────

def test_rewritten_outline_keeps_the_original_outline():
    """核心守卫：改写大纲时，**原大纲原文**必须留下来。

    没有这一条 ⇒ 用户的已确认大纲凭空消失，且无法恢复。
    """
    conn = _db()
    _seed(
        conn,
        act_plan=_ACT_PLAN,
        preplan={"plan": {"sections": []}},
    )
    policy = _policy(conn)

    policy._discard_transient_chapter_preplan("b1", 3)

    metadata = _node_metadata(conn)
    marker = metadata.get(OUTLINE_REWRITTEN_KEY)
    assert marker, "改写了大纲却没留下记号 ⇒ 原大纲丢了"
    assert marker["original_outline"].startswith("一、情节点1")
    assert marker["replaced_by"] == OUTLINE_REWRITTEN_SOURCE
    assert marker["rewritten_at"], "没记时间戳"


def test_original_outline_is_taken_from_the_chapter_row_first():
    """原大纲优先取 ``chapters.outline``（用户看到的那一份）。"""
    conn = _db()
    _seed(
        conn,
        chapter_outline="用户确认过的七段剧本",
        node_outline="结构树上的另一份",
        act_plan=_ACT_PLAN,
        preplan={"plan": {"sections": []}},
    )
    policy = _policy(conn)

    policy._discard_transient_chapter_preplan("b1", 3)

    marker = _node_metadata(conn)[OUTLINE_REWRITTEN_KEY]
    assert marker["original_outline"] == "用户确认过的七段剧本"


def test_original_outline_falls_back_to_the_node_outline():
    """``chapters.outline`` 为空时退回 ``story_nodes.outline``。"""
    conn = _db()
    _seed(
        conn,
        chapter_outline="",
        node_outline="结构树上的大纲",
        act_plan=_ACT_PLAN,
        preplan={"plan": {"sections": []}},
    )
    policy = _policy(conn)

    policy._discard_transient_chapter_preplan("b1", 3)

    marker = _node_metadata(conn)[OUTLINE_REWRITTEN_KEY]
    assert marker["original_outline"] == "结构树上的大纲"


# ── 2) 不该留记号的情形（不许制造噪音） ──────────────────────────────

def test_no_marker_when_there_is_no_alternative_outline():
    """没有可信替代大纲 ⇒ **保留原大纲**，不该留「被改写」的记号。"""
    conn = _db()
    _seed(conn, act_plan=None, preplan={"plan": {"sections": []}})
    policy = _policy(conn)

    policy._discard_transient_chapter_preplan("b1", 3)

    metadata = _node_metadata(conn)
    assert OUTLINE_REWRITTEN_KEY not in metadata
    # 原大纲还在
    row = conn.execute("SELECT outline FROM chapters WHERE novel_id='b1'").fetchone()
    assert row["outline"].startswith("一、情节点1")


def test_no_marker_when_replacement_is_identical():
    """替换前后一样 ⇒ 没有「改写」这回事 ⇒ 不留记号。

    ⚠ 构造要点：原大纲必须**就是**轻量大纲的渲染结果，否则两者不同 ⇒ 会留记号
    （那是正确行为，不是 bug）。
    """
    from application.blueprint.services.chapter_plan_renderer import (
        render_lightweight_act_chapter_outline,
    )

    same = render_lightweight_act_chapter_outline(_ACT_PLAN)
    assert same.strip(), "轻量大纲渲染不出内容 ⇒ 夹具无效"

    conn = _db()
    _seed(
        conn,
        chapter_outline=same,
        node_outline=same,
        act_plan=_ACT_PLAN,
        preplan={"plan": {"sections": []}},
    )
    policy = _policy(conn)
    policy._discard_transient_chapter_preplan("b1", 3)

    metadata = _node_metadata(conn)
    assert OUTLINE_REWRITTEN_KEY not in metadata, (
        "替换前后一样却留了『被改写』的记号 ⇒ 制造噪音"
    )


def test_marker_is_not_written_when_nothing_was_replaced():
    """完全没有替代大纲、也没有 preplan ⇒ metadata 不该被动。"""
    conn = _db()
    _seed(conn, act_plan=None, preplan=None)
    policy = _policy(conn)

    policy._discard_transient_chapter_preplan("b1", 3)

    metadata = _node_metadata(conn)
    assert OUTLINE_REWRITTEN_KEY not in metadata
    assert "chapter_preplan" not in metadata


# ── 3) 记号要能被前端读到 ────────────────────────────────────────────

def test_marker_lives_in_story_nodes_metadata():
    """记号必须放在 ``story_nodes.metadata``（``to_dict()`` 已原样透出 ⇒ 前端能读）。"""
    conn = _db()
    _seed(
        conn,
        act_plan=_ACT_PLAN,
        preplan={"plan": {"sections": []}},
    )
    policy = _policy(conn)
    policy._discard_transient_chapter_preplan("b1", 3)

    metadata = _node_metadata(conn)
    assert OUTLINE_REWRITTEN_KEY in metadata
    # 必须可 JSON 序列化（它整个 metadata 都要被 json.dumps 写回）
    json.dumps(metadata, ensure_ascii=False)


def test_story_node_to_dict_exposes_metadata():
    """结构树接口会把 metadata 透出去 ⇒ 前端拿得到（守卫这条链路没被改坏）。"""
    from domain.structure.story_node import PlanningSource, StoryNode

    node = StoryNode(
        id="n1",
        novel_id="b1",
        node_type="chapter",
        number=3,
        title="第三章",
        description=None,
        order_index=0,
        planning_source=PlanningSource.AI_ACT,
        metadata={OUTLINE_REWRITTEN_KEY: {"original_outline": "原文"}},
    )
    payload = node.to_dict()
    assert OUTLINE_REWRITTEN_KEY in payload["metadata"]


# ── 4) 前端：必须显式提示（文件内容级） ──────────────────────────────

def test_frontend_shows_an_explicit_notice_for_rewritten_outline():
    """前端必须有一条**显式提示**（不是静默换掉）。

    ⚠ 前端零测试设施（只有 ``npm run build`` 门禁）⇒ 这是**文件内容级**断言：
    它挡得住「提示被删掉」，挡不住「提示改个写法仍然存在」。

    ⚠ 判据要同时覆盖**逻辑**与**模板**：
    只查 ``outlineRewrittenNotice`` 的话，把模板里的 ``v-if`` 改成 ``false``
    就绕过去了（假绿）⇒ 必须查模板里那一行真的**绑着**这个 computed。
    """
    source = FRONTEND_PANEL.read_text(encoding="utf-8")

    assert "outlineRewrittenNotice" in source, "前端没有『大纲被改写』的提示逻辑"
    assert "outline_rewritten" in source, "前端没读后端留下的记号"
    # 提示文案要说清「被替换」这件事
    assert "被替换" in source, "提示文案没说清大纲被替换过"
    # 原大纲要能被看到（不是只说「换过了」）
    assert "originalOutline" in source, "原大纲没被展示出来"
    # ⚠ 模板里必须**真的**绑着这个条件（不能是 v-if="false"）
    assert 'v-if="outlineRewrittenNotice"' in source, (
        "模板里的提示没有绑在 outlineRewrittenNotice 上 ⇒ 提示不会显示"
    )
