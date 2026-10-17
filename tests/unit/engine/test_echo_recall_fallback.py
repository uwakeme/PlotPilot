"""回响召回的 DB 兜底必须取「最近」而非「最早」，且不得伪装成相似度命中。

缺陷本体（``engine/infrastructure/memory/echo_recall.py`` 兜底分支）：

- ``ORDER BY chapter_number ASC`` + ``LIMIT ?`` ⇒ 取的是该角色**最早**的 N 章，
  而不是最近的。角色该想起的是近期自己说过的话，不是开篇第一句台词。
- 下游 ``generate_echo_instruction`` 固定 ``max_results=1`` 并把章号写进提示词，
  于是无论写到第几章，注入的都是「第 1 章的自己」，还让 LLM 照着这个章号编。
- 兜底结果带一个硬编码 ``relevance_score=0.5``——一个看起来正常的相似度中间值，
  下游无法区分「兜底给的 0.5」与「真算出来的 0.5」。

本文件刻意**不走源码文本断言**：全部用真实 SQLite 跑行为。
库由 ``DatabaseConnection`` 建（schema.sql + 全部迁移，含 007 建出的
``character_voice_samples``），不裸开 ``sqlite3``。

关于潜伏性：生产库 ``character_voice_samples`` 为 0 行，且 ``store_sample``
全仓无调用方，所以这条路径当前恒返回空——本文件锁的是**语义**，不是正在发生的
污染。修好排序方向也不会让 T3 有内容，那属于「要不要接线」的功能级决策。
"""
from __future__ import annotations

import pytest

from engine.infrastructure.memory.echo_recall import EchoRecall
from infrastructure.persistence.database.connection import DatabaseConnection

CHAR_ID = "char-linyu"
CHAR_NAME = "林羽"


@pytest.fixture
def db(tmp_path):
    """真实 schema 的临时库（``DatabaseConnection`` 会跑 schema.sql + 全部迁移）。

    ``character_voice_samples.character_id`` 有指向 ``characters(id)`` 的外键，
    连接又开了 ``PRAGMA foreign_keys = ON``，所以必须先落角色行。
    """
    conn = DatabaseConnection(str(tmp_path / "echo.db"))
    with conn.transaction() as raw:
        raw.execute(
            "INSERT INTO characters (id, name, base_layer) VALUES (?, ?, ?)",
            (CHAR_ID, CHAR_NAME, "{}"),
        )
    return conn


def _seed_samples(db, character_id=CHAR_ID, chapters=(1, 2, 3, 4, 5)):
    """写入指定章号的发言样本，章号与正文一一对应。"""
    with db.transaction() as raw:
        for chapter in chapters:
            raw.execute(
                "INSERT OR IGNORE INTO character_voice_samples"
                " (character_id, chapter_number, content) VALUES (?, ?, ?)",
                (character_id, chapter, f"第{chapter}章的台词"),
            )


class _VectorStub:
    """向量检索替身。

    刻意用 ``**kwargs`` 承接生产代码实际发出的调用（``collection_name`` /
    ``query_text`` / ``filter`` / ``limit``），不假装真实 store 的签名。
    """

    def __init__(self, rows):
        self._rows = rows
        self.calls: list[dict] = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return list(self._rows)


# ── 缺陷 A：兜底取最近 N 章，顺序新→旧 ──────────────────────────────


@pytest.mark.asyncio
async def test_fallback_returns_most_recent_chapters_newest_first(db):
    """5 章样本 + max_results=3 ⇒ 必须返回第 5/4/3 章，而不是第 1/2/3 章。

    ``ORDER BY ... ASC`` 时这里拿到 ``[1, 2, 3]``。
    """
    _seed_samples(db)

    results = await EchoRecall(db_pool=db).recall(CHAR_ID, "第6章", max_results=3)

    assert [r["chapter"] for r in results] == [5, 4, 3]
    # 章号与正文必须对齐，防止排序对了但取错字段
    assert results[0]["content"] == "第5章的台词"


# ── 缺陷 B：降级结果必须自报家门，不得冒充相似度 ──────────────────────


@pytest.mark.asyncio
async def test_fallback_result_is_marked_as_recency_fallback(db):
    """兜底结果必须带来源标记，且不得携带「看起来正常的」相似度分数。

    ``relevance_score`` 表示的是向量相似度；兜底分支压根没算过相似度，
    填 0.5 属于伪造。向量路径的结果不带 ``source`` 键，下游据此分辨两条路径。
    """
    _seed_samples(db)

    results = await EchoRecall(db_pool=db).recall(CHAR_ID, "第6章", max_results=1)

    row = results[0]
    assert row.get("source") == "fallback_recent", "兜底结果没有自报家门，下游无法与向量命中区分"
    assert "relevance_score" in row, "降级结果必须显式声明相似度未计算，而不是省掉这个字段"
    assert row["relevance_score"] is None, (
        f"兜底路径没有计算任何相似度，relevance_score 必须是 None，实际是 {row['relevance_score']!r}"
    )


# ── 反向守卫：向量路径不受兜底改造影响 ──────────────────────────────


@pytest.mark.asyncio
async def test_vector_path_results_are_not_marked_or_rewritten(db):
    """走向量路径时，DB 兜底的改造不得污染结果：顺序、字段、来源标记都不动。

    库里故意也有 5 章样本：一旦实现误把 DB 结果混进向量结果，``len`` 与
    ``score`` 断言会当场抓住。
    """
    _seed_samples(db)
    vector_rows = [{"id": "s1", "score": 0.91, "payload": {"chapter": 2, "content": "向量命中"}}]
    stub = _VectorStub(vector_rows)

    results = await EchoRecall(db_pool=db, vector_store=stub).recall(CHAR_ID, "第6章", max_results=3)

    assert results == vector_rows
    assert all("source" not in row for row in results), "向量路径的结果被兜底来源标记污染了"
    assert len(stub.calls) == 1


# ── 缺陷 C：max_results=1 的实际后果（章号会被写进提示词）─────────────


@pytest.mark.asyncio
async def test_max_results_one_yields_the_latest_chapter_echo(db):
    """单条召回必须是**最新**那条，且注入指令里的章号随之是第 5 章。

    这条锁住缺陷的真实后果：``max_results=1`` 时旧实现拿到第 1 章，
    而章号会明文进提示词，LLM 会照着编。
    """
    _seed_samples(db)
    recall = EchoRecall(db_pool=db)

    results = await recall.recall(CHAR_ID, "第6章", max_results=1)
    assert [r["chapter"] for r in results] == [5]

    instruction = await recall.generate_echo_instruction(CHAR_ID, CHAR_NAME, "第6章")
    assert instruction is not None
    assert "第5章" in instruction
    assert "第1章" not in instruction


# ── 反向守卫：空表不报错、不返回伪造占位 ──────────────────────────────


@pytest.mark.asyncio
async def test_empty_table_returns_empty_results_without_placeholder(db):
    """没有样本时返回空列表 / None，不得编造占位结果。"""
    recall = EchoRecall(db_pool=db)

    assert await recall.recall(CHAR_ID, "第6章", max_results=3) == []
    assert await recall.generate_echo_instruction(CHAR_ID, CHAR_NAME, "第6章") is None
