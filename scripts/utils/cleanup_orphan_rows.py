# -*- coding: utf-8 -*-
"""清理 SQLite 库里「指向已删小说」的孤儿行。

⚠ 为什么需要这个工具
------------------
2026-10-31 清理 22 本 ``test-novel-*`` 测试残留时，脚本按
「表里有 ``novel_id`` 列 **且** 值以 ``test-novel-`` 开头」删 ⇒ 留下三类尾巴：

1. **书名不是 ``test-novel-`` 的残留**（``n-1`` / ``n1`` / ``novel-001`` /
   ``novel-b10-*`` / ``cast_snapshots`` 里的 ``test_dynamic_attrs`` …）—— 共 9 万多行；
2. **表里没有 ``novel_id`` 列**的孤儿（``chapter_summaries`` 的 id 是
   ``<novel_id>-knowledge-chN``、``checkpoints.story_id``、
   ``variable_values.scope_key``、``ai_invocation_sessions.metadata_json`` …）
   ⇒ **按 novel_id 清理永远够不到它们**；
3. **报告口径过宽**：「残留 0」只覆盖了「有 novel_id 列的表 + 某个前缀」。

⇒ 本工具把判据换成**「这条行归属的小说还在不在」**（而不是「名字像不像测试」），
   并把每张表的归属判据**写进报告** ⇒ 结论可被第二个人复跑复核。

⚠ 安全网
--------
① **先备份**：用 SQLite 的 ``Connection.backup()``（不是文件复制 ⇒ 事务一致的快照），
   **单独打开备份文件**跑 ``integrity_check`` + 行数核对 ⇒ 验过才算有备份；
② 报告里写的备份路径 **必须就是那个真实文件名**；
③ 删完跑 ``PRAGMA integrity_check``；
④ **真书零减**：删除前后逐表核对「归属存活小说的行数」必须一模一样；
⑤ 残留复扫：删除后用同一套判据再扫一遍，残留必须为 0（否则报告如实写非 0）。

用法
----
```bash
# 只看会删什么（默认，不写库）
python scripts/utils/cleanup_orphan_rows.py

# 真删（先停后端！）
python scripts/utils/cleanup_orphan_rows.py --apply

# 只复核残留（不改库）
python scripts/utils/cleanup_orphan_rows.py --verify-only
```

⚠ **请先停掉占用 ``plotpilot.db`` 的后端进程**（与 ``scripts/run_migrations.py`` 同一套做法：
维护脚本直连 sqlite3，不走应用的写队列）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from application.paths import DATA_DIR, get_db_path  # noqa: E402

BACKUP_DIR = DATA_DIR / "backups"

#: 归属判据里的「字符串里嵌着书 id」的前缀包裹（``chapter-<novel_id>-chapter-1``）。
_STRIP_PREFIXES = ("chapter-",)

#: 「id 里嵌着书 id」的**结构化**形状 ⇒ 能**精确**抽出书 id（不用猜前缀）。
#:
#: ⚠ 为什么必须结构化而不是「前缀匹配」：前缀匹配在
#: 「活书 id 是另一个 id 的前缀」时会误判 ——
#: ``book``（活）与 ``book-dead``（已删）同时存在时，
#: ``book-dead-knowledge-ch1`` 会被判给 ``book`` ⇒ **死书的行被留下**。
_STRING_ID_PATTERNS = (
    re.compile(r"^chapter-(?P<novel>.+)-chapter-\d+$"),
    re.compile(r"^(?P<novel>.+)-knowledge-ch\d+$"),
    re.compile(r"^(?P<novel>.+)-char-\d+$"),
)


@dataclass(frozen=True)
class Rule:
    """一张表怎么把行归属到某本书。

    Attributes:
        table: 表名。
        kind: ``column``（表自带 ``novel_id`` 列）/ ``fk``（外键指向父表）/
            ``scope_key``（``variable_values.scope_key`` 这种拼串键）/
            ``string_id``（id 里嵌着书 id，靠前缀归属）。
        novel_expr: SQL 表达式，值 = 该行归属的书 id（可能为 NULL ⇒ 无法归属）。
        fk: ``fk`` 类型用：本表里指向父表的外键列。
        parent: ``fk`` 类型用：父表名。
        parent_key: ``fk`` 类型用：父表里被引用的那一列（默认主键 ``id``）。
        parent_novel_expr: ``fk`` 类型用：父表里取书 id 的表达式。
        phase: 删除顺序（小的先删 ⇒ 子表先于父表）。
        note: 报告里写给复核人看的一句话。
    """

    table: str
    kind: str
    novel_expr: str = ""
    fk: str = ""
    parent: str = ""
    parent_key: str = "id"
    parent_novel_expr: str = ""
    phase: int = 10
    note: str = ""


#: 显式外键规则（子表 ⇒ 父表）。**phase 小的先删**。
#:
#: ⚠ **phase 排反 = 孤儿抓不到**：父表先删 ⇒ 子表再也 JOIN 不到父行
#:   ⇒ 子表那批孤儿原地留下（而复扫会显示「残留 0」⇒ 又是假绿）。
#:   ⇒ 阶段号按「子 ⇒ 父」排：``ai_adoption_commit_steps``(10) 必须早于
#:   ``ai_adoption_commits``(11)，``ai_adoption_commits`` 早于
#:   ``ai_invocation_sessions``(20)，``variable_lineage``(5) 早于
#:   ``variable_values``(42)，``ai_trace_artifacts``(89) 早于通用扫描(90)。
#:
#: ⚠ 这里只列「表里没有 novel_id 列、但行仍然归属到某本书」的表；
#:   自带 ``novel_id`` 列的表走下面的通用扫描（不手写清单 ⇒ 不会漏）。
_EXPLICIT_RULES: List[Rule] = [
    Rule(
        table="variable_lineage",
        kind="fk",
        fk="variable_value_id",
        parent="variable_values",
        parent_novel_expr=(
            "CASE WHEN scope_level = 'novel' AND scope_key LIKE 'novel_id:%' "
            "THEN substr(scope_key, 10, instr(scope_key || '|', '|') - 10) END"
        ),
        phase=5,
        note="variable_value_id → variable_values（血缘表 ⇒ 必须先于 variable_values 删）",
    ),
    Rule(
        table="ai_invocation_sessions",
        kind="json",
        novel_expr="json_extract(metadata_json, '$.novel_id')",
        phase=20,
        note="metadata_json 里的 novel_id（context_json 只有 3092/3198 行有 ⇒ 以 metadata 为准）",
    ),
    Rule(
        table="ai_invocation_attempts",
        kind="fk",
        fk="session_id",
        parent="ai_invocation_sessions",
        parent_novel_expr="json_extract(metadata_json, '$.novel_id')",
        phase=13,
        note="session_id → ai_invocation_sessions",
    ),
    Rule(
        table="ai_adoption_decisions",
        kind="fk",
        fk="session_id",
        parent="ai_invocation_sessions",
        parent_novel_expr="json_extract(metadata_json, '$.novel_id')",
        phase=12,
        note="session_id → ai_invocation_sessions",
    ),
    Rule(
        table="ai_adoption_commits",
        kind="fk",
        fk="session_id",
        parent="ai_invocation_sessions",
        parent_novel_expr="json_extract(metadata_json, '$.novel_id')",
        phase=11,
        note="session_id → ai_invocation_sessions",
    ),
    Rule(
        table="ai_adoption_commit_steps",
        kind="fk",
        fk="commit_id",
        parent="ai_adoption_commits",
        parent_novel_expr=(
            "(SELECT json_extract(s.metadata_json, '$.novel_id') "
            "FROM ai_invocation_sessions s WHERE s.id = ai_adoption_commits.session_id)"
        ),
        phase=10,
        note="commit_id → ai_adoption_commits → ai_invocation_sessions",
    ),
    Rule(
        table="chapter_elements",
        kind="string_id",
        novel_expr="chapter_id",
        phase=30,
        note="chapter_id = chapter-<novel_id>-chapter-N ⇒ 按前缀归属",
    ),
    Rule(
        table="triple_attr",
        kind="fk",
        fk="triple_id",
        parent="triples",
        parent_novel_expr="novel_id",
        phase=31,
        note="triple_id → triples",
    ),
    Rule(
        table="unified_character_relationships",
        kind="fk",
        fk="character_id",
        parent="unified_characters",
        parent_novel_expr="novel_id",
        phase=32,
        note="character_id = <novel_id>-char-N → unified_characters",
    ),
    Rule(
        table="plot_points",
        kind="fk",
        fk="plot_arc_id",
        parent="plot_arcs",
        parent_novel_expr="novel_id",
        phase=33,
        note="plot_arc_id → plot_arcs",
    ),
    Rule(
        table="checkpoints",
        kind="column",
        novel_expr="story_id",
        phase=40,
        note="story_id 就是书 id",
    ),
    Rule(
        table="checkpoint_heads",
        kind="column",
        novel_expr="story_id",
        phase=41,
        note="story_id 就是书 id",
    ),
    Rule(
        table="variable_values",
        kind="scope_key",
        novel_expr=(
            "CASE WHEN scope_level = 'novel' AND scope_key LIKE 'novel_id:%' "
            "THEN substr(scope_key, 10, instr(scope_key || '|', '|') - 10) END"
        ),
        phase=42,
        note="scope_key = novel_id:<book>|chapter_number:N ⇒ 取 book 段",
    ),
    Rule(
        table="ai_trace_artifacts",
        kind="fk",
        fk="trace_id",
        parent="ai_trace_spans",
        parent_key="trace_id",
        parent_novel_expr="novel_id",
        phase=89,
        note="trace_id → ai_trace_spans（制品表 ⇒ 必须先于 ai_trace_spans 删）",
    ),
    Rule(
        table="chapter_summaries",
        kind="string_id",
        novel_expr="id",
        phase=43,
        note="id = <novel_id>-knowledge-chN ⇒ 按前缀归属",
    ),
]


def _live_clause(live_ids: List[str]) -> str:
    """存活书 id 的 SQL 字面量列表。"""
    if not live_ids:
        return "SELECT NULL WHERE 0"
    return "(" + ",".join("'%s'" % i.replace("'", "''") for i in live_ids) + ")"


def _tables(conn: sqlite3.Connection) -> List[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [c[1] for c in conn.execute("PRAGMA table_info(%s)" % table)]


def build_rules(conn: sqlite3.Connection) -> List[Rule]:
    """拼出全部规则：**显式规则 + 通用扫描（凡有 ``novel_id`` 列的表都收）**。"""
    rules: List[Rule] = list(_EXPLICIT_RULES)
    explicit_tables = {r.table for r in _EXPLICIT_RULES}
    for table in _tables(conn):
        if table in explicit_tables:
            continue
        cols = _columns(conn, table)
        if "novel_id" in cols:
            rules.append(
                Rule(
                    table=table,
                    kind="column",
                    novel_expr="novel_id",
                    phase=90,
                    note="表自带 novel_id 列",
                )
            )
    # 去重：同一张表若被多条规则覆盖，保留 phase 最小（先删）的那条
    best: Dict[str, Rule] = {}
    for r in rules:
        cur = best.get(r.table)
        if cur is None or r.phase < cur.phase:
            best[r.table] = r
    return sorted(best.values(), key=lambda r: (r.phase, r.table))


def _orphan_predicate(rule: Rule, live_clause: str) -> str:
    """「这一行归属的小说已经不存在」的 SQL 谓词。

    ⚠ **归属不到任何书的行不算孤儿**（``novel_id`` 为空 ⇒ 全局数据，不动）。
    """
    if rule.kind == "column":
        return (
            "({e}) IS NOT NULL AND TRIM({e}) <> '' AND ({e}) NOT IN {live}"
        ).format(e=rule.novel_expr, live=live_clause)
    if rule.kind == "json":
        return (
            "({e}) IS NOT NULL AND TRIM({e}) <> '' AND ({e}) NOT IN {live}"
        ).format(e=rule.novel_expr, live=live_clause)
    if rule.kind == "scope_key":
        return (
            "({e}) IS NOT NULL AND TRIM({e}) <> '' AND ({e}) NOT IN {live}"
        ).format(e=rule.novel_expr, live=live_clause)
    if rule.kind == "string_id":
        # 归属靠前缀匹配 ⇒ 在 Python 侧判定（表小 ⇒ 直接算）
        return ""
    if rule.kind == "fk":
        return (
            "{fk} IN (SELECT {pk} FROM {parent} WHERE ({pe}) IS NOT NULL "
            "AND TRIM({pe}) <> '' AND ({pe}) NOT IN {live})"
        ).format(
            fk=rule.fk,
            pk=rule.parent_key,
            parent=rule.parent,
            pe=rule.parent_novel_expr,
            live=live_clause,
        )
    raise ValueError("未知 rule kind: %s" % rule.kind)


def _attr_novel_id(value: Optional[str], live_ids: List[str]) -> Optional[str]:
    """把「id 里嵌着书 id」的值归属到某本存活书（归属不到 ⇒ None）。

    ⚠ **先按结构精确抽**（``-knowledge-chN`` / ``-chapter-N`` / ``-char-N``）
      ⇒ 只有结构不认识的形状才退回「前缀匹配」。
      ⇒ 否则「活书 id 是另一个 id 的前缀」时会误判：
      ``book``（活）与 ``book-dead``（已删）并存时，
      ``book-dead-knowledge-ch1`` 会被算给 ``book`` ⇒ **死书的行留在库里**。
    """
    if not value:
        return None
    text = str(value)
    for pattern in _STRING_ID_PATTERNS:
        m = pattern.match(text)
        if m:
            novel = m.group("novel")
            return novel if novel in live_ids else None
    # 兜底：前缀匹配（**按长度倒序** ⇒ 短的不会先命中）
    stripped = text
    for prefix in _STRIP_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :]
            break
    for novel_id in sorted(live_ids, key=len, reverse=True):
        if stripped == novel_id or stripped.startswith(novel_id + "-"):
            return novel_id
    return None


def _string_id_orphans(
    conn: sqlite3.Connection, rule: Rule, live_ids: List[str], pk: str
) -> List[str]:
    """``string_id`` 类型的孤儿行主键列表（按前缀归属判定）。"""
    rows = conn.execute(
        "SELECT %s AS pk, %s AS v FROM %s" % (pk, rule.novel_expr, rule.table)
    ).fetchall()
    orphans = []
    for row in rows:
        # ⚠ 用位置下标（不依赖 ``row_factory``）⇒ 调用方连什么连接都行
        if _attr_novel_id(row[1], live_ids) is None:
            orphans.append(str(row[0]))
    return orphans


def _pk_column(conn: sqlite3.Connection, table: str) -> str:
    """主键列名（没有显式主键 ⇒ 用 rowid）。"""
    for c in conn.execute("PRAGMA table_info(%s)" % table):
        if c[5]:
            return c[1]
    return "rowid"


def _coverage_probe(
    conn: sqlite3.Connection, covered: Dict[str, str], uncovered: List[Dict[str, int]]
) -> List[Dict[str, object]]:
    """未覆盖的表里有没有「偷偷引用某本书」的外键。

    ⚠ **为什么要有这一步**：上一轮清理报告写「残留 0」，但那个口径只扫了
    「有 ``novel_id`` 列的表 + 某个前缀」⇒ **没被扫到的表 = 没被检查过的表**
    （同一个洞，第二次踩）。⇒ 这里再验一遍：未覆盖表的 ``*_id`` 列里，
    有没有命中「已覆盖表的主键/被引用键」的值 ⇒ 命中就说明它**可能**归属某本书。

    Returns:
        命中的 ``{table, column, matched_count, matched_samples}`` 列表。
    """
    # ① 已覆盖表里「可能被别人引用」的键值全集（主键 + 所有 *_id 列）
    key_values: set = set()
    for table in covered:
        try:
            for c in conn.execute("PRAGMA table_info(%s)" % table):
                col = c[1]
                if col != _pk_column(conn, table) and not col.endswith("_id"):
                    continue
                for r in conn.execute(
                    "SELECT %s FROM %s WHERE %s IS NOT NULL" % (col, table, col)
                ):
                    if r[0] is not None:
                        key_values.add(str(r[0]))
        except sqlite3.Error:
            continue

    hits: List[Dict[str, object]] = []
    for entry in uncovered:
        table = entry["table"]
        if table == "novels":  # 根表（存活书集合的来源本身）
            continue
        for c in conn.execute("PRAGMA table_info(%s)" % table):
            col = c[1]
            if not (col.endswith("_id") or col == "id"):
                continue
            try:
                vals = [
                    str(r[0])
                    for r in conn.execute(
                        "SELECT DISTINCT %s FROM %s WHERE %s IS NOT NULL AND TRIM(%s) <> ''"
                        % (col, table, col, col)
                    )
                ]
            except sqlite3.Error:
                continue
            matched = [
                v
                for v in vals
                if v in key_values and not v.isdigit()  # 纯数字是 rowid，不是引用
            ]
            if matched:
                hits.append(
                    {
                        "table": table,
                        "column": col,
                        "matched_count": len(matched),
                        "matched_samples": matched[:3],
                    }
                )
    return hits


@dataclass
class TablePlan:
    """一张表的清理计划。"""

    table: str
    kind: str
    note: str
    total_rows: int = 0
    orphan_rows: int = 0
    live_rows_before: int = 0
    live_rows_after: int = 0
    deleted_rows: int = 0
    sample_orphans: List[str] = field(default_factory=list)
    predicate: str = ""


def _live_row_count(
    conn: sqlite3.Connection,
    rule: Rule,
    live_clause: str,
    live_ids: Optional[List[str]] = None,
) -> int:
    """归属**存活**小说的行数（删前删后必须一样 ⇒ 真书零减）。"""
    if rule.kind in ("column", "json", "scope_key"):
        expr = rule.novel_expr
        sql = (
            "SELECT COUNT(*) FROM {t} WHERE ({e}) IS NOT NULL AND TRIM({e}) <> '' "
            "AND ({e}) IN {live}".format(t=rule.table, e=expr, live=live_clause)
        )
    elif rule.kind == "fk":
        sql = (
            "SELECT COUNT(*) FROM {t} WHERE {fk} IN (SELECT {pk} FROM {p} "
            "WHERE ({pe}) IS NOT NULL AND TRIM({pe}) <> '' AND ({pe}) IN {live})".format(
                t=rule.table,
                fk=rule.fk,
                pk=rule.parent_key,
                p=rule.parent,
                pe=rule.parent_novel_expr,
                live=live_clause,
            )
        )
    else:  # string_id ⇒ 按前缀/结构归属，在 Python 侧数
        if live_ids is None:
            return -1
        rows = conn.execute(
            "SELECT %s AS v FROM %s" % (rule.novel_expr, rule.table)
        ).fetchall()
        return sum(1 for r in rows if _attr_novel_id(r[0], live_ids) is not None)
    try:
        return conn.execute(sql).fetchone()[0]
    except sqlite3.Error:
        return -1


def plan(conn: sqlite3.Connection) -> Dict[str, object]:
    """算出「每张表会删多少行 + 判据是什么」（不写库）。"""
    live_ids = [r[0] for r in conn.execute("SELECT id FROM novels ORDER BY id")]
    live_clause = _live_clause(live_ids)
    rules = build_rules(conn)

    plans: List[TablePlan] = []
    covered: Dict[str, str] = {}
    for rule in rules:
        pk = _pk_column(conn, rule.table)
        total = conn.execute("SELECT COUNT(*) FROM %s" % rule.table).fetchone()[0]
        tp = TablePlan(
            table=rule.table,
            kind=rule.kind,
            note=rule.note,
            total_rows=total,
        )
        if total == 0:
            plans.append(tp)
            covered[rule.table] = "空表"
            continue
        if rule.kind == "string_id":
            orphans = _string_id_orphans(conn, rule, live_ids, pk)
            tp.orphan_rows = len(orphans)
            tp.sample_orphans = orphans[:5]
            live_before = conn.execute(
                "SELECT COUNT(*) FROM %s" % rule.table
            ).fetchone()[0]
            tp.live_rows_before = live_before - len(orphans)
            tp.live_rows_after = tp.live_rows_before
        else:
            pred = _orphan_predicate(rule, live_clause)
            tp.predicate = pred
            tp.orphan_rows = conn.execute(
                "SELECT COUNT(*) FROM %s WHERE %s" % (rule.table, pred)
            ).fetchone()[0]
            tp.live_rows_before = _live_row_count(conn, rule, live_clause, live_ids)
            tp.live_rows_after = tp.live_rows_before
            if tp.orphan_rows:
                tp.sample_orphans = [
                    str(r[0])
                    for r in conn.execute(
                        "SELECT %s FROM %s WHERE %s LIMIT 5" % (pk, rule.table, pred)
                    )
                ]
        covered[rule.table] = rule.note
        plans.append(tp)

    # 未覆盖的表（如实列出 ⇒ 读者能自己判断「是不是还有没扫到的残留」）
    uncovered = []
    for table in _tables(conn):
        if table in covered:
            continue
        total = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
        uncovered.append({"table": table, "rows": total})

    return {
        "live_novel_ids": live_ids,
        "plans": plans,
        "uncovered_tables": uncovered,
        "uncovered_reference_hits": _coverage_probe(conn, covered, uncovered),
    }


def _jsonable(snapshot: Dict[str, object]) -> Dict[str, object]:
    """计划快照 → 可 JSON 序列化形态。"""
    return {
        "live_novel_ids": snapshot["live_novel_ids"],
        "plans": [asdict(tp) for tp in snapshot["plans"]],
        "uncovered_tables": snapshot["uncovered_tables"],
        "uncovered_reference_hits": snapshot.get("uncovered_reference_hits", []),
    }


def apply_cleanup(conn: sqlite3.Connection) -> Tuple[Dict[str, int], Dict[str, object]]:
    """执行删除。返回（每表删除行数, 计划快照）。"""
    snapshot = plan(conn)
    live_ids = snapshot["live_novel_ids"]
    live_clause = _live_clause(live_ids)
    rules = {r.table: r for r in build_rules(conn)}

    deleted: Dict[str, int] = {}
    # ⚠ 调用方可能留着一个未提交的事务（Python sqlite3 默认自动 BEGIN）
    #   ⇒ 直接 BEGIN IMMEDIATE 会报 "cannot start a transaction within a transaction"
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for tp in snapshot["plans"]:
            if not tp.orphan_rows:
                continue
            rule = rules[tp.table]
            pk = _pk_column(conn, tp.table)
            if rule.kind == "string_id":
                orphans = _string_id_orphans(conn, rule, live_ids, pk)
                for start in range(0, len(orphans), 400):
                    chunk = orphans[start : start + 400]
                    ph = ",".join("?" * len(chunk))
                    cur = conn.execute(
                        "DELETE FROM %s WHERE %s IN (%s)" % (tp.table, pk, ph), chunk
                    )
                    deleted[tp.table] = deleted.get(tp.table, 0) + cur.rowcount
            else:
                pred = _orphan_predicate(rule, live_clause)
                cur = conn.execute("DELETE FROM %s WHERE %s" % (tp.table, pred))
                deleted[tp.table] = deleted.get(tp.table, 0) + cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return deleted, snapshot


def verify(conn: sqlite3.Connection) -> Dict[str, object]:
    """用同一套判据复扫残留（不改库）。"""
    snapshot = plan(conn)
    residual = {
        tp.table: tp.orphan_rows for tp in snapshot["plans"] if tp.orphan_rows
    }
    return {
        "live_novel_ids": snapshot["live_novel_ids"],
        "residual_orphans_by_table": residual,
        "residual_orphans_total": sum(residual.values()),
        "uncovered_tables": snapshot["uncovered_tables"],
        "uncovered_reference_hits": snapshot.get("uncovered_reference_hits", []),
    }


def _backup_db(conn: sqlite3.Connection, db_path: pathlib.Path) -> pathlib.Path:
    """做一份**事务一致**的备份，并**当场验可回滚**。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / f"plotpilot-before-orphan-cleanup-{stamp}.db"
    dest = sqlite3.connect(str(backup))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    # ⚠ **单独打开备份文件**验 ⇒ 证伪「备份其实是坏的 / 是空壳」
    check = sqlite3.connect(str(backup))
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        novels = check.execute("SELECT COUNT(*) FROM novels").fetchone()[0]
    finally:
        check.close()
    if integrity != "ok":
        raise RuntimeError("备份校验失败：integrity_check=%s" % integrity)
    print("backup verified:", backup, "novels=%d" % novels, "integrity=%s" % integrity)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description="清理指向已删小说的孤儿行")
    parser.add_argument("--db", default=None, help="数据库路径（默认取应用配置）")
    parser.add_argument("--apply", action="store_true", help="真删（默认只预览）")
    parser.add_argument("--verify-only", action="store_true", help="只复扫残留")
    parser.add_argument("--report", default=None, help="报告 JSON 输出路径")
    args = parser.parse_args()

    db_path = pathlib.Path(args.db) if args.db else pathlib.Path(get_db_path())
    if not db_path.exists():
        print("DB not found: %s" % db_path, file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    if args.verify_only:
        result = verify(conn)
        conn.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if not args.apply:
        snapshot = plan(conn)
        conn.close()
        if args.report:
            pathlib.Path(args.report).write_text(
                json.dumps(_jsonable(snapshot), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        print(json.dumps(
            {
                "live_novel_ids": snapshot["live_novel_ids"],
                "tables_with_orphans": {
                    tp.table: tp.orphan_rows
                    for tp in snapshot["plans"]
                    if tp.orphan_rows
                },
                "orphan_rows_total": sum(
                    tp.orphan_rows for tp in snapshot["plans"]
                ),
                "uncovered_nonempty_tables": [
                    t for t in snapshot["uncovered_tables"] if t["rows"]
                ],
                "uncovered_reference_hits": snapshot.get("uncovered_reference_hits", []),
            },
            ensure_ascii=True,
            indent=2,
        ))
        return 0

    backup = _backup_db(conn, db_path)
    deleted, snapshot = apply_cleanup(conn)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    residual = verify(conn)

    # 真书零减核对：逐表比对「归属存活小说的行数」删前删后
    live_mismatch = []
    for tp in snapshot["plans"]:
        if tp.live_rows_before < 0:
            continue
        rule = {r.table: r for r in build_rules(conn)}[tp.table]
        after = _live_row_count(
            conn,
            rule,
            _live_clause(snapshot["live_novel_ids"]),
            snapshot["live_novel_ids"],
        )
        if after != tp.live_rows_before:
            live_mismatch.append(
                {"table": tp.table, "before": tp.live_rows_before, "after": after}
            )

    report = {
        "backup": str(backup),
        "backup_exists": backup.exists(),
        "db": str(db_path),
        "live_novel_ids": snapshot["live_novel_ids"],
        "deleted_counts": deleted,
        "deleted_rows_total": sum(deleted.values()),
        "integrity_check": integrity,
        "residual_orphans_total": residual["residual_orphans_total"],
        "residual_orphans_by_table": residual["residual_orphans_by_table"],
        "live_book_rows_mismatch": live_mismatch,
        "uncovered_tables": snapshot["uncovered_tables"],
        "uncovered_reference_hits": snapshot.get("uncovered_reference_hits", []),
        "per_table": [
            {
                "table": tp.table,
                "kind": tp.kind,
                "criterion": tp.note,
                "total_before": tp.total_rows,
                "orphans": tp.orphan_rows,
                "sample_orphans": tp.sample_orphans,
            }
            for tp in snapshot["plans"]
            if tp.orphan_rows or tp.total_rows
        ],
    }
    conn.close()

    if args.report:
        pathlib.Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(
        {
            "backup": report["backup"],
            "deleted_rows_total": report["deleted_rows_total"],
            "integrity_check": integrity,
            "residual_orphans_total": report["residual_orphans_total"],
            "live_book_rows_mismatch": report["live_book_rows_mismatch"],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
