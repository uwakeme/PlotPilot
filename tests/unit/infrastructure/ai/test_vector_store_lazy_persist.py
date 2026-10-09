"""D8: 向量库延迟落盘 + 批量插入。"""
import asyncio

import pytest

from infrastructure.ai.chromadb_vector_store import ChromaDBVectorStore


def _store(tmp_path):
    return ChromaDBVectorStore(persist_directory=str(tmp_path / "vs"))


@pytest.mark.asyncio
async def test_batch_insert_visible_immediately_and_persisted(tmp_path):
    store = _store(tmp_path)
    await store.create_collection("novel_x_chunks", 4)
    items = [(f"id{i}", [float(i), 1.0, 0.5, 0.2], {"text": f"chunk{i}"}) for i in range(40)]

    await store.insert_batch("novel_x_chunks", items)

    hits = await store.search("novel_x_chunks", [0.0, 1.0, 0.5, 0.2], 3)
    assert len(hits) == 3

    # 40 条 ≥ 32 阈值：已自动落盘——新实例可读回
    store.flush()
    reopened = _store(tmp_path)
    hits2 = await reopened.search("novel_x_chunks", [3.0, 1.0, 0.5, 0.2], 1)
    assert hits2 and hits2[0]["id"] == "id3"


@pytest.mark.asyncio
async def test_single_insert_below_threshold_is_lazy(tmp_path):
    store = _store(tmp_path)
    await store.create_collection("novel_y", 2)

    await store.insert("novel_y", "a", [1.0, 0.0], {"t": "a"})

    # 阈值之下：内存可查，但未落盘（重开实例看不到）
    hits = await store.search("novel_y", [1.0, 0.0], 1)
    assert hits and hits[0]["id"] == "a"
    reopened = _store(tmp_path)
    assert await reopened.search("novel_y", [1.0, 0.0], 1) == []

    # flush 后持久化
    store.flush()
    reopened2 = _store(tmp_path)
    hits2 = await reopened2.search("novel_y", [1.0, 0.0], 1)
    assert hits2 and hits2[0]["id"] == "a"


@pytest.mark.asyncio
async def test_delete_marks_dirty_and_flush_persists(tmp_path):
    store = _store(tmp_path)
    await store.create_collection("novel_z", 2)
    await store.insert("novel_z", "a", [1.0, 0.0], {"t": "a"})
    store.flush()

    await store.delete("novel_z", "a")
    store.flush()

    reopened = _store(tmp_path)
    assert await reopened.search("novel_z", [1.0, 0.0], 1) == []


@pytest.mark.asyncio
async def test_insert_replaces_existing_id(tmp_path):
    store = _store(tmp_path)
    await store.create_collection("novel_w", 2)
    await store.insert("novel_w", "same", [1.0, 0.0], {"v": 1})
    store.flush()

    await store.insert("novel_w", "same", [0.0, 1.0], {"v": 2})
    store.flush()

    hits = await store.search("novel_w", [0.0, 1.0], 5)
    same_hits = [h for h in hits if h["id"] == "same"]
    assert len(same_hits) == 1
    assert same_hits[0]["payload"]["v"] == 2
