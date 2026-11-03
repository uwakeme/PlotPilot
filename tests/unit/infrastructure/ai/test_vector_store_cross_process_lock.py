# -*- coding: utf-8 -*-
"""第 90 批：向量库的**跨进程写锁**守卫。

病根
----
落盘目录 ``data/chromadb/`` 被 **API 进程与守护进程共用**
（``get_vector_store()`` 是**进程级单例** ⇒ 两个进程各持一份实例）。
⇒ 没有锁时，两个进程会「各自读磁盘、各自合并、各自覆盖」
⇒ ⇒ **后写的把先写的向量静默抹掉**（读-合并-写之间仍有窗口）。

⇒ 现在「**合并 + 写**」在同一把**跨进程文件锁**内 ⇒ 串行 ⇒ 不再丢更新。

本文件钉住
----------
1. 写盘会留下**锁文件**（锁真的被用上了，不是装饰）；
2. **真互斥**：父进程持锁时，**另一个进程**的写必须**被挡住**；
   父进程放锁后，子进程才能写完（⇒ 不是「同进程才生效」的假锁）。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from infrastructure.ai.chromadb_vector_store import ChromaDBVectorStore  # noqa: E402

CHILD_SCRIPT = '''
import asyncio, sys
sys.path.insert(0, %r)
from infrastructure.ai.chromadb_vector_store import ChromaDBVectorStore

async def main():
    store = ChromaDBVectorStore(persist_directory=sys.argv[1])
    await store.create_collection("novel_x_chunks", 4)
    await store.insert_batch(
        "novel_x_chunks",
        [("from_child", [0.1, 1.0, 0.5, 0.2], {"text": "child"})],
    )
    store.flush()
    print("child done")

asyncio.run(main())
'''


def _store(tmp_path) -> ChromaDBVectorStore:
    return ChromaDBVectorStore(persist_directory=str(tmp_path / "vs"))


@pytest.mark.asyncio
async def test_writing_leaves_a_lock_file_behind(tmp_path):
    """⚠ 锁文件必须真的被创建（否则「锁」只是个摆设）。"""
    store = _store(tmp_path)
    await store.create_collection("novel_x_chunks", 4)
    store.flush()

    lock_file = tmp_path / "vs" / ".novel_x_chunks.lock"
    assert lock_file.exists(), "写盘没有留下锁文件 ⇒ 跨进程锁没被用上"


@pytest.mark.asyncio
async def test_the_lock_blocks_another_process(tmp_path):
    """⚠ **真互斥**：父进程持锁 ⇒ 另一个进程的写必须**被挡住**。

    ⚠ 这条必须是**跨进程**的：同进程的线程不会互相挡住
      （``msvcrt.locking`` 是**进程级**的）⇒ 用同进程测等于没测。
    """
    script = tmp_path / "child_write.py"
    script.write_text(CHILD_SCRIPT % str(ROOT), encoding="utf-8")

    store = _store(tmp_path)
    await store.create_collection("novel_x_chunks", 4)
    store.flush()

    with store._cross_process_lock("novel_x_chunks"):
        proc = subprocess.Popen(
            [sys.executable, str(script), str(tmp_path / "vs")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        time.sleep(2.0)
        assert proc.poll() is None, (
            "父进程持锁时子进程的写没有被挡住 ⇒ 这把锁在跨进程场景下无效"
        )
        # 放锁 ⇒ 子进程应当能写完
        proc.wait(timeout=60)

    assert proc.returncode == 0, "子进程写盘失败：%s" % (
        proc.stderr.read().decode("utf-8", "replace")[-500:]
    )


@pytest.mark.asyncio
async def test_two_stores_ending_up_with_the_same_collection_do_not_erase_each_other(tmp_path):
    """⚠ 场景：实例 B 的内存视图**早于**实例 A 的写入 ⇒ B 落盘时必须**合并**。

    ⚠ 这是生产实况：守护进程与 API 进程各有自己的 store 实例，
      各自的内存视图与磁盘都会**错位**。
    """
    store_a = _store(tmp_path)
    store_b = _store(tmp_path)          # B 的内存视图：B 还没有这个集合

    await store_a.create_collection("novel_x_chunks", 4)
    await store_a.insert_batch(
        "novel_x_chunks", [("from_a", [0.1, 1.0, 0.5, 0.2], {"text": "A"})]
    )
    store_a.flush()

    await store_b.create_collection("novel_x_chunks", 4)
    await store_b.insert_batch(
        "novel_x_chunks", [("from_b", [0.3, 1.0, 0.5, 0.2], {"text": "B"})]
    )
    store_b.flush()

    reopened = _store(tmp_path)
    hits = await reopened.search("novel_x_chunks", [0.1, 1.0, 0.5, 0.2], 10)
    ids = {h["id"] for h in hits}
    assert "from_a" in ids, "A 写的向量被 B 抹掉了"
    assert "from_b" in ids, "B 自己写的向量不见了"
