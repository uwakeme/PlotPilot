"""一次性审计脚本（不进 CI）：核验「张力 50.0 假中性分」在生产库里的**实际分布**。

问题（探针 B 的 P0-1 断言）：``chapters`` 表四列张力 DEFAULT 50.0
⇒ 「从未评过张力」与「恰好得 50 分」在存储层同形；
而下游 ``monitor.py`` 那个「分辨默认 50」的启发式条件永远不成立。

本脚本给**事实口径**：
  * tension_score == 50.0 的行，分别是什么 status、其它三列是不是也是 50.0；
  * 各小说的 completed 章里，有多少行是「四列全 50.0」（= 从没评过）；
  * 有没有真正的 NULL。

⚠ 只读：sqlite3 ``mode=ro`` 打开，绝不写库。
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


def main() -> int:
    if not DB.exists():
        print(f"找不到 {DB}")
        return 1
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    print("=== 1. tension_score == 50.0 的行，按 status 与其它列分组 ===")
    rows = conn.execute(
        """
        SELECT status, plot_tension, emotional_tension, pacing_tension, COUNT(*) n
        FROM chapters WHERE tension_score = 50.0
        GROUP BY status, plot_tension, emotional_tension, pacing_tension
        """
    ).fetchall()
    if not rows:
        print("  （没有任何 tension_score = 50.0 的行）")
    for r in rows:
        print(
            f"  status={r['status']:<10} plot={r['plot_tension']} "
            f"emo={r['emotional_tension']} pacing={r['pacing_tension']} n={r['n']}"
        )

    print("\n=== 2. 按小说：四列是否全 50.0 / 是否有 NULL ===")
    for r in conn.execute(
        """
        SELECT novel_id, COUNT(*) n,
               SUM(CASE WHEN tension_score IS NULL THEN 1 ELSE 0 END) t_null,
               SUM(CASE WHEN plot_tension IS NULL THEN 1 ELSE 0 END) p_null,
               SUM(CASE WHEN tension_score = 50.0
                         AND plot_tension = 50.0
                         AND emotional_tension = 50.0
                         AND pacing_tension = 50.0 THEN 1 ELSE 0 END) all_four_50,
               SUM(CASE WHEN tension_score = 50.0 THEN 1 ELSE 0 END) t_eq50
        FROM chapters GROUP BY novel_id
        """
    ):
        print(
            f"  {r['novel_id'][:26]:<26} n={r['n']:>3}  t_null={r['t_null']}  "
            f"p_null={r['p_null']}  四列全50={r['all_four_50']}  t==50={r['t_eq50']}"
        )

    print("\n=== 3. status='draft' 的行（monitor 的 is_default_draft 只认这个）===")
    for r in conn.execute(
        """
        SELECT id, novel_id, number, status, tension_score, plot_tension
        FROM chapters WHERE status = 'draft' LIMIT 10
        """
    ):
        print(
            f"  {r['novel_id'][:22]:<22} ch{r['number']:>4} status={r['status']} "
            f"tension={r['tension_score']} plot={r['plot_tension']}"
        )

    print("\n=== 4. monitor.py 那个判据在真实行上的命中数 ===")
    n_draft = conn.execute(
        "SELECT COUNT(*) FROM chapters WHERE status='draft' "
        "AND tension_score = 50.0 AND plot_tension IS NULL"
    ).fetchone()[0]
    n_draft_any = conn.execute(
        "SELECT COUNT(*) FROM chapters WHERE status='draft' AND tension_score = 50.0"
    ).fetchone()[0]
    print(f"  满足完整判据（draft + tension=50 + plot IS NULL）的行 = {n_draft}")
    print(f"  满足「draft + tension=50」（plot 是不是 NULL 不管）的行 = {n_draft_any}")
    print("  ⇒ 判据里 plot_tension IS NULL 这一条是不是恒假，看第 2 节的 p_null")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
