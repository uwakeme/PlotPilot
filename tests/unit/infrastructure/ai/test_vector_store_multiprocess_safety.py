# -*- coding: utf-8 -*-
"""第 88 批（N-23）：向量库的**跨进程安全**守卫。

病根（生产实测）
----------------
落盘目录 ``data/chromadb/`` 被**多个进程共用**：
``get_vector_store()`` 是**进程级单例** ⇒ API 进程与守护进程**各持一份**
``ChromaDBVectorStore``，但写同一个目录。

而旧实现里：

1. ``list_collections()`` 只列**本进程内存**里的集合
   ⇒ 另一个进程建的集合「看起来不存在」；
2. ``_create_collection_locked()`` 在「集合不在本进程内存」时
   ⇒ **新建空索引 + ``_save_collection()`` 覆盖磁盘**
   ⇒ 另一个进程写进去的向量**全丢**；
3. ``_save_collection()`` 是**整份覆盖写** ⇒ 两个进程都写时「后写覆盖先写」。

⇒ 生产实测：600 章那本书的向量索引被清成了「本次跑的 5 章」的量
（``metadata.json`` 只剩 5 条，落盘时间=本次跑；而 5 章的书是 455.7KB
≡ 大书当时的 455.1KB）⇒ ⇒ **长书的历史章节在向量检索里不存在**。

本文件钉住
----------
1. **磁盘上有、本进程内存没有** ⇒ ``create_collection`` **不许**清空它；
2. ``list_collections()`` 必须包含**磁盘上存在**但本进程没加载的集合；
3. 两个实例各自写入 ⇒ 落盘时**合并**（不许互相抹掉）；
4. 既有行为不变（写完立刻可查、落盘后重开仍在）。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from infrastructure.ai.chromadb_vector_store import ChromaDBVectorStore  # noqa: E402

DIM = 4


def _vec(seed: float) -> list[float]:
    return [seed, 1.0, 0.5, 0.2]


def _store(tmp_path) -> ChromaDBVectorStore:
    return ChromaDBVectorStore(persist_directory=str(tmp_path / "vs"))


@pytest.mark.asyncio
async def test_creating_a_collection_that_exists_on_disk_must_not_wipe_it(tmp_path):
    """⚠ **核心**：集合在磁盘上、只是**不在本进程内存** ⇒ 不许覆盖磁盘。

    ⚠ 这正是生产实测的那个洞：另一个进程建好的集合被**新建空索引覆盖**。

    ⚠ 场景要摆对：**「本进程内存里没有」只在「实例比集合更早创建」时成立**
      （实例构造时会 ``_load_collections()`` 把磁盘上的集合全载进来）。
      ⇒ 生产实况：守护进程的 store 实例在 12:08 就建好了，而那个集合是**之后**
        才由别的进程写上磁盘的 ⇒ 守护进程的内存里永远没有它。
    """
    other = _store(tmp_path)          # 先建实例（此刻磁盘上还没有集合）
    writer = _store(tmp_path)         # 后建实例

    await writer.create_collection("novel_x_chunks", DIM)
    await writer.insert_batch(
        "novel_x_chunks",
        [("id1", _vec(0.1), {"text": "第一章"}), ("id2", _vec(0.2), {"text": "第二章"})],
    )
    writer.flush()

    # 另一个进程：它的内存里没有这个集合（但磁盘上有）
    assert "novel_x_chunks" not in other.collections, "前提不成立：新实例本应内存为空"
    await other.create_collection("novel_x_chunks", DIM)

    # 磁盘上的向量必须还在
    reopened = _store(tmp_path)
    hits = await reopened.search("novel_x_chunks", _vec(0.1), 5)
    ids = {h["id"] for h in hits}
    assert "id1" in ids and "id2" in ids, (
        "另一个进程建集合时把磁盘上已有的向量清空了 ⇒ 实得 %s" % sorted(ids)
    )


@pytest.mark.asyncio
async def test_list_collections_reports_what_is_on_disk(tmp_path):
    """⚠ 集合存在性必须以**磁盘真相**为准（不然调用方会误判「不存在」）。

    ⚠ 时序要点：``other`` 必须在集合**被创建之前**就构造好 ——
      否则它的 ``_load_collections()`` 会把集合载进内存 ⇒ 测不到「磁盘分支」。
    """
    other = _store(tmp_path)          # 先建实例（此刻磁盘上还没有集合）

    writer = _store(tmp_path)
    await writer.create_collection("novel_y_chunks", DIM)
    await writer.insert_batch("novel_y_chunks", [("id1", _vec(0.3), {"text": "x"})])
    writer.flush()

    assert "novel_y_chunks" not in other.collections, "前提不成立：新实例本应内存为空"
    collections = await other.list_collections()
    assert "novel_y_chunks" in collections, (
        "磁盘上明明有，list_collections 却没报 ⇒ 调用方会误判「不存在」"
    )


@pytest.mark.asyncio
async def test_two_instances_writing_do_not_erase_each_other(tmp_path):
    """⚠ 两个实例各自写入 ⇒ 落盘时**合并**，不许「后写覆盖先写」。

    ⚠ 场景：实例 A 建好集合并写完；实例 B **在 A 写之前**就建好了集合
      （所以 B 的内存视图里没有 A 后来的向量）⇒ B 落盘时必须把 A 的并进来。
    """
    a = _store(tmp_path)
    await a.create_collection("novel_z_chunks", DIM)   # A 先建（空）

    b = _store(tmp_path)
    await b.create_collection("novel_z_chunks", DIM)   # B 也建（此时磁盘上还是空的）

    await a.insert_batch("novel_z_chunks", [("from_a", _vec(0.4), {"text": "A"})])
    a.flush()                                          # A 落盘

    await b.insert_batch("novel_z_chunks", [("from_b", _vec(0.5), {"text": "B"})])
    b.flush()                                          # B 落盘 ⇒ 必须合并 A 的

    reopened = _store(tmp_path)
    hits = await reopened.search("novel_z_chunks", _vec(0.4), 10)
    ids = {h["id"] for h in hits}
    assert "from_a" in ids, "A 写的向量被 B 抹掉了（后写覆盖先写）"
    assert "from_b" in ids, "B 自己写的向量不见了"


@pytest.mark.asyncio
async def test_the_collection_still_survives_a_reopen(tmp_path):
    """⚠ 回归：既有行为不变（落盘后重开仍可查）。"""
    store = _store(tmp_path)
    await store.create_collection("novel_w_chunks", DIM)
    await store.insert_batch("novel_w_chunks", [("id1", _vec(0.6), {"text": "x"})])
    store.flush()

    reopened = _store(tmp_path)
    hits = await reopened.search("novel_w_chunks", _vec(0.6), 3)
    assert hits and hits[0]["id"] == "id1"


@pytest.mark.asyncio
async def test_inserting_into_a_collection_of_another_process_does_not_crash(tmp_path):
    """⚠ 集合只在磁盘上、调用方**没先** ``create_collection`` ⇒ 也不该炸。

    ⚠ 时序要点：``other`` 必须在集合**被创建之前**构造（否则它构造时就载入内存了）。
    """
    other = _store(tmp_path)          # 先建实例

    writer = _store(tmp_path)
    await writer.create_collection("novel_v_chunks", DIM)
    await writer.insert_batch("novel_v_chunks", [("id1", _vec(0.7), {"text": "x"})])
    writer.flush()

    assert "novel_v_chunks" not in other.collections, "前提不成立：新实例本应内存为空"
    await other.insert_batch("novel_v_chunks", [("id2", _vec(0.8), {"text": "y"})])

    reopened = _store(tmp_path)
    hits = await reopened.search("novel_v_chunks", _vec(0.7), 10)
    ids = {h["id"] for h in hits}
    assert "id1" in ids and "id2" in ids
