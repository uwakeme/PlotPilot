"""一次性审计脚本（不进 CI）：验「文风漂移告警」这条链在**真实数据**上的行为。

⚠ 只读：全部用 sqlite3 的 ``mode=ro`` URI 打开，绝不写库。

要验的三件事
------------
1. ``chapter_style_scores.similarity_score`` 是 ``NOT NULL`` 列
   ⇒ 「无指纹基准 ⇒ 存 0.0」⇒ **库里存不出 None**
   ⇒ 任何 ``similarity_score IS NOT NULL`` / ``is not None`` 的过滤都是**死条件**；
2. ``_check_drift_alert`` 读的是 ``list_by_novel(limit=10)``
   ⇒ SQL 是 ``ORDER BY chapter_number ASC LIMIT 10`` ⇒ 拿到的是**最早 10 章**
   ⇒ 与 docstring 说的「最近 N 章」不是一回事；
3. 于是「无基准」章节（存 0.0）会被当成「相似度 0 ⇒ 远低于阈值 0.75」
   ⇒ 窗口恒为 True ⇒ 最终 ``last_audit_drift_alert`` 退化成
   「本章相似度低于阈值」，**丢掉了「连续 N 章」语义**。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "plotpilot.db"
LEGACY = ROOT / "data" / "aitext.db"


def open_ro(path: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    path = DB if DB.exists() else LEGACY
    if not path.exists():
        print(f"找不到数据库：{DB} / {LEGACY}")
        return 1
    print(f"只读打开：{path}")
    conn = open_ro(path)

    print("\n── 1. similarity_score 列定义 ──")
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='chapter_style_scores'"
    ).fetchone()
    print(row["sql"] if row else "（表不存在）")

    print("\n── 2. 每本书的评分行数 / 相似度分布 ──")
    rows = conn.execute(
        """
        SELECT novel_id,
               COUNT(*)                                   AS n_rows,
               SUM(CASE WHEN similarity_score = 0.0 THEN 1 ELSE 0 END) AS n_zero,
               SUM(CASE WHEN similarity_score > 0.0 THEN 1 ELSE 0 END) AS n_pos,
               MIN(similarity_score) AS min_s, MAX(similarity_score) AS max_s
        FROM chapter_style_scores
        GROUP BY novel_id
        ORDER BY n_rows DESC
        LIMIT 20
        """
    ).fetchall()
    if not rows:
        print("（chapter_style_scores 是空表）")
    for r in rows:
        print(
            f"  novel={r['novel_id'][:28]:<28} 行数={r['n_rows']:>3} "
            f"0.0占={r['n_zero']:>3} 正值={r['n_pos']:>3} "
            f"min={r['min_s']} max={r['max_s']}"
        )

    print("\n── 3. 逐章看某本书的相似度（验证「最早章节存 0.0」）──")
    if rows:
        novel_id = rows[0]["novel_id"]
        for r in conn.execute(
            """
            SELECT chapter_number, similarity_score, computed_at
            FROM chapter_style_scores
            WHERE novel_id = ?
            ORDER BY chapter_number ASC
            LIMIT 16
            """,
            (novel_id,),
        ):
            print(
                f"  ch{r['chapter_number']:>3}  similarity={r['similarity_score']!r:<8}"
                f" computed_at={r['computed_at']}"
            )

        print("\n── 4. 复刻 _check_drift_alert 的取数（ASC + LIMIT 10）──")
        picked = conn.execute(
            """
            SELECT chapter_number, similarity_score
            FROM chapter_style_scores
            WHERE novel_id = ?
            ORDER BY chapter_number ASC
            LIMIT ?
            """,
            (novel_id, 5 * 2),
        ).fetchall()
        print(f"  取到的是：{[r['chapter_number'] for r in picked]}"
              f"  （期望是**最近** 10 章 ⇒ 章号应当最大）")
        valid = [r for r in picked if r["similarity_score"] is not None]
        print(f"  'is not None' 过滤掉的行数 = {len(picked) - len(valid)}"
              f" / {len(picked)}  ← 全是 0 ⇒ 过滤是**死条件**")
        threshold = 0.75
        recent = valid[-5:]
        all_low = bool(recent) and all(r["similarity_score"] < threshold for r in recent)
        print(f"  recent 5 章相似度 = {[r['similarity_score'] for r in recent]}")
        print(f"  all(< {threshold}) = {all_low}  ← 窗口告警结论")

    print("\n── 5. novels 表上的 last_audit_drift_alert 落库值 ──")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(novels)").fetchall()}
    if "last_audit_drift_alert" in cols:
        rows = conn.execute(
            """
            SELECT id, last_audit_chapter_number, last_audit_similarity,
                   last_audit_drift_alert
            FROM novels
            WHERE last_audit_chapter_number IS NOT NULL
            ORDER BY last_audit_chapter_number DESC
            LIMIT 15
            """
        ).fetchall()
        for r in rows:
            print(
                f"  novel={r['id'][:24]:<24} ch={r['last_audit_chapter_number']} "
                f"similarity={r['last_audit_similarity']!r:<8} "
                f"drift_alert={r['last_audit_drift_alert']!r}"
            )
        if not rows:
            print("  （没有写过章末审阅快照的书）")
    else:
        print("  （novels 表没有 last_audit_drift_alert 列）")

    print("\n── 6. voice_fingerprint 的 sample_count ──")
    fcols = {r["name"] for r in conn.execute("PRAGMA table_info(voice_fingerprint)").fetchall()}
    if fcols:
        for r in conn.execute(
            "SELECT novel_id, sample_count, last_updated FROM voice_fingerprint LIMIT 10"
        ):
            print(f"  novel={r['novel_id'][:24]:<24} sample_count={r['sample_count']} "
                  f"updated={r['last_updated']}")
    else:
        print("  （voice_fingerprint 表不存在）")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
