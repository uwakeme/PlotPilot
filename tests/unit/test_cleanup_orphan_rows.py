# -*- coding: utf-8 -*-
"""``scripts/utils/cleanup_orphan_rows.py`` 的守卫（合成库 ⇒ **不碰生产数据**）。

守的是什么
----------
上一轮清理（2026-10-31）留下的三个洞：

1. 判据是「名字像不像测试」⇒ 真书 ``prodrealtest-001`` 会被误伤；
2. 判据只覆盖「有 ``novel_id`` 列的表」⇒ 没有那列的表**从来没被检查过**
   （「没被扫到的表 = 没被检查的表」）；
3. 父表先删 ⇒ 子表再也抓不到（顺序错 ⇒ 孤儿留在库里）。

⇒ 这里用**合成库**把三件事都钉住：孤儿被删、真书一行不动、残留复扫为 0。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_module():
    """按路径加载维护脚本（它不在包内 ⇒ 不能直接 import）。"""
    path = ROOT / "scripts" / "utils" / "cleanup_orphan_rows.py"
    spec = importlib.util.spec_from_file_location("cleanup_orphan_rows", path)
    module = importlib.util.module_from_spec(spec)
    # ⚠ 必须先注册进 ``sys.modules`` ⇒ ``@dataclass`` 解析 ``cls.__module__`` 才不会炸
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cleanup = _load_module()

LIVE = "book-live"
DEAD = "book-dead"
#: 一对**前缀包含**的书 id（短的是长的前缀 ⇒ 用来抓「前缀短的那个先命中」的错）
LIVE_SHORT = "book"
LIVE_LONG = "book-1"


SCHEMA = """
CREATE TABLE novels (id TEXT PRIMARY KEY, title TEXT);
CREATE TABLE ai_trace_spans (id INTEGER PRIMARY KEY, novel_id TEXT, trace_id TEXT);
CREATE TABLE ai_trace_artifacts (artifact_id INTEGER PRIMARY KEY, trace_id TEXT, novel_id TEXT);
CREATE TABLE cast_snapshots (novel_id TEXT, payload TEXT);
CREATE TABLE governance_events (id TEXT PRIMARY KEY, novel_id TEXT);
CREATE TABLE narrative_contracts (id TEXT PRIMARY KEY, novel_id TEXT);
CREATE TABLE chapters (id TEXT PRIMARY KEY, novel_id TEXT);
CREATE TABLE chapter_elements (id INTEGER PRIMARY KEY, chapter_id TEXT);
CREATE TABLE chapter_summaries (id TEXT PRIMARY KEY, summary TEXT);
CREATE TABLE triples (id TEXT PRIMARY KEY, novel_id TEXT);
CREATE TABLE triple_attr (triple_id TEXT, attr_key TEXT);
CREATE TABLE unified_characters (id TEXT PRIMARY KEY, novel_id TEXT);
CREATE TABLE unified_character_relationships (id INTEGER PRIMARY KEY, character_id TEXT);
CREATE TABLE plot_arcs (id TEXT PRIMARY KEY, novel_id TEXT);
CREATE TABLE plot_points (id INTEGER PRIMARY KEY, plot_arc_id TEXT);
CREATE TABLE checkpoints (id INTEGER PRIMARY KEY, story_id TEXT);
CREATE TABLE checkpoint_heads (story_id TEXT, checkpoint_id TEXT);
CREATE TABLE variable_values (id TEXT PRIMARY KEY, scope_level TEXT, scope_key TEXT);
CREATE TABLE variable_lineage (id TEXT PRIMARY KEY, variable_value_id TEXT);
CREATE TABLE ai_invocation_sessions (id TEXT PRIMARY KEY, metadata_json TEXT);
CREATE TABLE ai_invocation_attempts (id TEXT PRIMARY KEY, session_id TEXT);
CREATE TABLE ai_adoption_decisions (id TEXT PRIMARY KEY, session_id TEXT);
CREATE TABLE ai_adoption_commits (id TEXT PRIMARY KEY, session_id TEXT);
CREATE TABLE ai_adoption_commit_steps (id INTEGER PRIMARY KEY, commit_id TEXT);
"""


@pytest.fixture()
def seeded_db(tmp_path):
    """造一个「1 本活书 + 1 本已删书 + 1 本前缀包含的书」的合成库。"""
    db_path = tmp_path / "synthetic.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO novels (id, title) VALUES (?, ?)",
        # ⚠ DEAD **不在** novels 里 —— 它代表「已被删掉的书」
        [(LIVE, "活书"), (LIVE_SHORT, "短 id 活书"), (LIVE_LONG, "长 id 活书")],
    )
    # ── 有 novel_id 列的表 ────────────────────────────────────────────
    conn.execute("INSERT INTO ai_trace_spans (novel_id, trace_id) VALUES (?, ?)", (LIVE, "t-live"))
    conn.execute("INSERT INTO ai_trace_spans (novel_id, trace_id) VALUES (?, ?)", (DEAD, "t-dead"))
    conn.execute("INSERT INTO ai_trace_spans (novel_id, trace_id) VALUES (?, ?)", ("", "t-global"))
    conn.execute("INSERT INTO ai_trace_artifacts (trace_id, novel_id) VALUES (?, ?)", ("t-dead", DEAD))
    conn.execute("INSERT INTO ai_trace_artifacts (trace_id, novel_id) VALUES (?, ?)", ("t-live", LIVE))
    conn.execute("INSERT INTO cast_snapshots (novel_id, payload) VALUES (?, ?)", (DEAD, "x"))
    conn.execute("INSERT INTO governance_events (id, novel_id) VALUES (?, ?)", ("g1", DEAD))
    conn.execute("INSERT INTO narrative_contracts (id, novel_id) VALUES (?, ?)", ("n1", DEAD))

    # ── 没有 novel_id 列的表（上一轮清理够不到的那批）──────────────────
    conn.execute("INSERT INTO chapters (id, novel_id) VALUES (?, ?)", ("chapter-%s-chapter-1" % LIVE, LIVE))
    conn.execute("INSERT INTO chapter_elements (chapter_id) VALUES (?)", ("chapter-%s-chapter-1" % LIVE,))
    conn.execute("INSERT INTO chapter_elements (chapter_id) VALUES (?)", ("chapter-%s-chapter-1" % DEAD,))
    conn.execute("INSERT INTO chapter_summaries (id, summary) VALUES (?, ?)", ("%s-knowledge-ch1" % LIVE, "s"))
    conn.execute("INSERT INTO chapter_summaries (id, summary) VALUES (?, ?)", ("%s-knowledge-ch1" % DEAD, "s"))
    conn.execute("INSERT INTO triples (id, novel_id) VALUES (?, ?)", ("tri-live", LIVE))
    conn.execute("INSERT INTO triples (id, novel_id) VALUES (?, ?)", ("tri-dead", DEAD))
    conn.execute("INSERT INTO triple_attr (triple_id, attr_key) VALUES (?, ?)", ("tri-live", "k"))
    conn.execute("INSERT INTO triple_attr (triple_id, attr_key) VALUES (?, ?)", ("tri-dead", "k"))
    conn.execute("INSERT INTO unified_characters (id, novel_id) VALUES (?, ?)", ("%s-char-1" % LIVE, LIVE))
    conn.execute("INSERT INTO unified_characters (id, novel_id) VALUES (?, ?)", ("%s-char-1" % DEAD, DEAD))
    conn.execute("INSERT INTO unified_character_relationships (character_id) VALUES (?)", ("%s-char-1" % LIVE,))
    conn.execute("INSERT INTO unified_character_relationships (character_id) VALUES (?)", ("%s-char-1" % DEAD,))
    conn.execute("INSERT INTO plot_arcs (id, novel_id) VALUES (?, ?)", ("arc-live", LIVE))
    conn.execute("INSERT INTO plot_arcs (id, novel_id) VALUES (?, ?)", ("arc-dead", DEAD))
    conn.execute("INSERT INTO plot_points (plot_arc_id) VALUES (?)", ("arc-live",))
    conn.execute("INSERT INTO plot_points (plot_arc_id) VALUES (?)", ("arc-dead",))
    conn.execute("INSERT INTO checkpoints (story_id) VALUES (?)", (LIVE,))
    conn.execute("INSERT INTO checkpoints (story_id) VALUES (?)", (DEAD,))
    conn.execute("INSERT INTO checkpoint_heads (story_id, checkpoint_id) VALUES (?, ?)", (LIVE, "c1"))
    conn.execute("INSERT INTO checkpoint_heads (story_id, checkpoint_id) VALUES (?, ?)", (DEAD, "c2"))
    conn.execute(
        "INSERT INTO variable_values (id, scope_level, scope_key) VALUES (?, ?, ?)",
        ("v-live", "novel", "novel_id:%s|chapter_number:1" % LIVE),
    )
    conn.execute(
        "INSERT INTO variable_values (id, scope_level, scope_key) VALUES (?, ?, ?)",
        ("v-dead", "novel", "novel_id:%s|chapter_number:1" % DEAD),
    )
    conn.execute("INSERT INTO variable_lineage (id, variable_value_id) VALUES (?, ?)", ("l-live", "v-live"))
    conn.execute("INSERT INTO variable_lineage (id, variable_value_id) VALUES (?, ?)", ("l-dead", "v-dead"))
    conn.execute(
        "INSERT INTO ai_invocation_sessions (id, metadata_json) VALUES (?, ?)",
        ("s-live", json.dumps({"novel_id": LIVE})),
    )
    conn.execute(
        "INSERT INTO ai_invocation_sessions (id, metadata_json) VALUES (?, ?)",
        ("s-dead", json.dumps({"novel_id": DEAD})),
    )
    conn.execute(
        "INSERT INTO ai_invocation_sessions (id, metadata_json) VALUES (?, ?)",
        ("s-global", json.dumps({"stage": "x"})),
    )
    conn.execute("INSERT INTO ai_invocation_attempts (id, session_id) VALUES (?, ?)", ("at-live", "s-live"))
    conn.execute("INSERT INTO ai_invocation_attempts (id, session_id) VALUES (?, ?)", ("at-dead", "s-dead"))
    conn.execute("INSERT INTO ai_adoption_decisions (id, session_id) VALUES (?, ?)", ("d-live", "s-live"))
    conn.execute("INSERT INTO ai_adoption_decisions (id, session_id) VALUES (?, ?)", ("d-dead", "s-dead"))
    conn.execute("INSERT INTO ai_adoption_commits (id, session_id) VALUES (?, ?)", ("c-live", "s-live"))
    conn.execute("INSERT INTO ai_adoption_commits (id, session_id) VALUES (?, ?)", ("c-dead", "s-dead"))
    conn.execute("INSERT INTO ai_adoption_commit_steps (commit_id) VALUES (?)", ("c-live",))
    conn.execute("INSERT INTO ai_adoption_commit_steps (commit_id) VALUES (?)", ("c-dead",))
    conn.commit()
    yield conn
    conn.close()


def _row_count(conn, table):
    return conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]


def _apply(conn):
    deleted, snapshot = cleanup.apply_cleanup(conn)
    return deleted, snapshot


class TestCleanupOrphanRows:
    def test_orphans_are_deleted_and_live_rows_untouched(self, seeded_db):
        """孤儿被删；活书（含空 novel_id 的全局行）一行不动。"""
        conn = seeded_db
        before = {
            t: _row_count(conn, t)
            for t in (
                "ai_trace_spans",
                "cast_snapshots",
                "governance_events",
                "narrative_contracts",
                "chapter_elements",
                "chapter_summaries",
                "triple_attr",
                "unified_character_relationships",
                "plot_points",
                "checkpoints",
                "checkpoint_heads",
                "variable_values",
                "variable_lineage",
                "ai_invocation_sessions",
                "ai_invocation_attempts",
                "ai_adoption_decisions",
                "ai_adoption_commits",
                "ai_adoption_commit_steps",
                "ai_trace_artifacts",
            )
        }
        deleted, _ = _apply(conn)

        # 每张表：死书的行归零，活书的行还在
        assert conn.execute(
            "SELECT COUNT(*) FROM ai_trace_spans WHERE novel_id = ?", (DEAD,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ai_trace_spans WHERE novel_id = ?", (LIVE,)
        ).fetchone()[0] == 1
        # ⚠ 空 novel_id 的全局行**不许**被当成孤儿删掉
        assert conn.execute(
            "SELECT COUNT(*) FROM ai_trace_spans WHERE novel_id = ''"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM chapter_summaries WHERE id = ?", ("%s-knowledge-ch1" % DEAD,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM chapter_summaries WHERE id = ?", ("%s-knowledge-ch1" % LIVE,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM variable_values WHERE id = ?", ("v-dead",)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM variable_values WHERE id = ?", ("v-live",)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM variable_lineage WHERE id = ?", ("l-dead",)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ai_invocation_sessions WHERE id = ?", ("s-dead",)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ai_invocation_sessions WHERE id = ?", ("s-global",)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM ai_adoption_commit_steps WHERE commit_id = ?", ("c-dead",)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ai_trace_artifacts WHERE trace_id = ?", ("t-dead",)
        ).fetchone()[0] == 0
        assert sum(deleted.values()) > 0

    def test_parent_rows_are_deleted_after_children(self, seeded_db):
        """父表先删 ⇒ 子表就再也抓不到（顺序错了孤儿就留在库里）。"""
        conn = seeded_db
        _apply(conn)
        # 死书的父行确实没了 ⇒ 说明它是在子行还在的时候被删的
        assert conn.execute(
            "SELECT COUNT(*) FROM triples WHERE novel_id = ?", (DEAD,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM triple_attr WHERE triple_id = ?", ("tri-dead",)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM plot_arcs WHERE novel_id = ?", (DEAD,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM plot_points WHERE plot_arc_id = ?", ("arc-dead",)
        ).fetchone()[0] == 0
        # ⚠ 这条链最容易排反：commit_steps(子) ← commits(父) ← sessions(祖)
        assert conn.execute(
            "SELECT COUNT(*) FROM ai_adoption_commits WHERE session_id = ?", ("s-dead",)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ai_adoption_commit_steps WHERE commit_id = ?", ("c-dead",)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ai_invocation_attempts WHERE session_id = ?", ("s-dead",)
        ).fetchone()[0] == 0

    def test_child_rules_are_ordered_before_their_parents(self):
        """阶段号必须「子 < 父」⇒ 否则父表先删，子表孤儿永远抓不到。

        ⚠ 这条盯的是**规则表里的顺序**（不是运行结果）：
        顺序排反时运行结果也会错，但只有把规则表本身钉住，
        下次改阶段号才会立刻红。
        """
        rules = {r.table: r for r in cleanup._EXPLICIT_RULES}
        assert rules["ai_adoption_commit_steps"].phase < rules["ai_adoption_commits"].phase
        assert rules["ai_adoption_commits"].phase < rules["ai_invocation_sessions"].phase
        assert rules["ai_adoption_decisions"].phase < rules["ai_invocation_sessions"].phase
        assert rules["ai_invocation_attempts"].phase < rules["ai_invocation_sessions"].phase
        assert rules["variable_lineage"].phase < rules["variable_values"].phase
        # 通用扫描（自带 novel_id 列的表）**最后**删 ⇒ 它是很多表父表
        generic_phase = 90
        assert rules["triple_attr"].phase < generic_phase
        assert rules["plot_points"].phase < generic_phase
        assert rules["chapter_elements"].phase < generic_phase
        assert rules["ai_trace_artifacts"].phase < generic_phase

    def test_rescan_after_cleanup_is_zero(self, seeded_db):
        """删完用同一套判据复扫 ⇒ 残留必须是 0（报告口径就靠这条）。"""
        conn = seeded_db
        _apply(conn)
        residual = cleanup.verify(conn)
        assert residual["residual_orphans_total"] == 0, residual

    def test_prefix_sibling_book_ids_are_not_mixed_up(self, seeded_db):
        """``book`` 与 ``book-1`` 同时存活 ⇒ ``book-1-*`` 必须算给 ``book-1``。

        ⚠ 这条盯的是**前缀短的那个先命中**的错：算错了就会把 A 书的行删掉。
        """
        conn = seeded_db
        conn.execute(
            "INSERT INTO chapter_summaries (id, summary) VALUES (?, ?)",
            ("%s-knowledge-ch1" % LIVE_LONG, "s"),
        )
        conn.execute(
            "INSERT INTO chapter_summaries (id, summary) VALUES (?, ?)",
            ("%s-knowledge-ch1" % LIVE_SHORT, "s"),
        )
        _apply(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM chapter_summaries WHERE id = ?",
            ("%s-knowledge-ch1" % LIVE_LONG,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM chapter_summaries WHERE id = ?",
            ("%s-knowledge-ch1" % LIVE_SHORT,),
        ).fetchone()[0] == 1

    def test_uncovered_tables_have_no_hidden_novel_references(self, seeded_db):
        """「未覆盖」不等于「一定干净」⇒ 探针要能验出偷偷引用某本书的表。"""
        conn = seeded_db
        snapshot = cleanup.plan(conn)
        assert snapshot["uncovered_reference_hits"] == []

        # 造一张「没规则、但引用了活书行」的表 ⇒ 探针必须报出来
        conn.execute("CREATE TABLE sneaky (id INTEGER PRIMARY KEY, chapter_id TEXT)")
        conn.execute(
            "INSERT INTO sneaky (chapter_id) VALUES (?)", ("chapter-%s-chapter-1" % LIVE,)
        )
        conn.commit()
        snapshot2 = cleanup.plan(conn)
        hits = snapshot2["uncovered_reference_hits"]
        assert any(h["table"] == "sneaky" for h in hits), hits
