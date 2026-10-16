#!/usr/bin/env python3
"""运行数据库迁移脚本

应用 ``infrastructure/persistence/database/migrations/`` 下所有未执行的迁移。

历史说明：本脚本最初只为修两个具体报错（macro_diagnosis_results 表缺失、
chapter_summaries.key_events 列缺失）而写，硬编码的验证段只能回答那两件事。
现在它跑的是**全部**迁移（含数据回填类），验证段已改为通用汇报。
"""

import sys
import sqlite3
from pathlib import Path

# Windows 控制台默认 GBK，打印 ✓ / ✗ / ✅ 会直接抛 UnicodeEncodeError，
# 把迁移结果连同堆栈一起吞掉——这正是本脚本此前"跑到一半就没了"的原因。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# 添加项目路径以便导入模块
sys.path.insert(0, str(Path(__file__).parent.parent))

from infrastructure.persistence.database.connection import _apply_migration_files


def _table_exists(conn, name: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def run_migrations():
    """执行数据库迁移"""
    print("=" * 60)
    print("运行数据库迁移")
    print("=" * 60)

    # 获取数据库路径
    from application.paths import get_db_path
    db_path = get_db_path()

    print(f"数据库路径: {db_path}")

    if not Path(db_path).exists():
        print(f"✗ 数据库文件不存在: {db_path}")
        sys.exit(1)

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        if _table_exists(conn, "migrations_applied"):
            before = {
                r[0] for r in conn.execute("SELECT migration_file FROM migrations_applied")
            }
        else:
            before = set()

        print("\n应用迁移文件...")
        _apply_migration_files(conn)

        if _table_exists(conn, "migrations_applied"):
            after_rows = list(
                conn.execute(
                    "SELECT migration_file, applied_at FROM migrations_applied"
                    " ORDER BY applied_at DESC, rowid DESC"
                )
            )
        else:
            after_rows = []
        after = {r[0] for r in after_rows}

        print("\n验证迁移结果...")
        newly = [r for r in after_rows if r[0] not in before]
        if newly:
            print(f"本次新应用 {len(newly)} 个迁移：")
            for name, applied_at in newly:
                print(f"  ✓ {name}  ({applied_at})")
        else:
            print("  数据库已是最新，没有待应用的迁移。")
        print(f"已应用迁移总数: {len(after)}")

        conn.close()

        print("\n" + "=" * 60)
        print("✅ 数据库迁移完成！")
        print("=" * 60)

    except Exception as e:
        print(f"\n✗ 迁移失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    run_migrations()
