"""第 67 批守卫：B-4 —— 记忆回写的「落库失败」必须**看得见**。

背景（独立审查报告 ``review-briefs/2026-10-30-review-report-85c0f726-closeout.md`` B-4）
------------------------------------------------------------------------------
``MemoryEngine._persist_state`` 以前捕获**全部**异常、只 log、**不上报**
⇒ 上游 ``_run_memory_update`` 只看 ``result["errors"]`` ⇒ errors 为空
⇒ 判定「已更新」⇒ 审计面板**一条「未更新」条目都不写**
⇒ 记忆一条没落库，系统却报「一切正常」。

生产**队列模式**下更严重：``execute()`` 对 mutating SQL 只**入队**就返回
⇒ 真正的 INSERT 在 writer 线程执行 ⇒ writer 线程里的失败**结构上到不了**调用方
⇒ 光看「execute 没抛异常」等于什么都没验。

本文件钉住
----------
1. 落库抛异常 ⇒ **必须**返回错误信息（不许吞）；
2. 落库失败 ⇒ 必须进 ``result["errors"]`` ⇒ 上游判「未更新」；
3. 「入队成功但实际没落库」⇒ 回读确认必须能发现；
4. 成功路径 ⇒ 不许误报（零行为变化）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from application.engine.services.memory_engine import MemoryEngine


class _BibleRepo:
    """只要能过 ``FactLockBuilder.build`` 的最小 Bible 仓储。"""

    def get_by_novel_id(self, novel_id):
        return SimpleNamespace(
            id="b1",
            novel_id=novel_id,
            characters=[],
            timeline_notes=[],
            locations=[],
            world_settings=[],
            style_notes=[],
        )


class _StubLLM:
    """返回一个**合法**的记忆增量载荷。"""

    def __init__(self) -> None:
        self.payload = {
            "completed_beats": [
                {"beat_id": "ch3-first-trace", "summary": "她第一次听见了声痕", "chapter": 3}
            ],
            "revealed_clues": [],
            "fact_violations": [],
        }

    async def generate(self, prompt, config):
        from domain.ai.services.llm_service import GenerationResult
        from domain.ai.value_objects.token_usage import TokenUsage

        return GenerationResult(
            content=json.dumps(self.payload, ensure_ascii=False),
            token_usage=TokenUsage(input_tokens=1, output_tokens=1),
        )


class _StubGateway:
    def render(self, contract, variables):
        return SimpleNamespace(prompt="PROMPT")


class _StubCursor:
    def __init__(self, row=None) -> None:
        self._row = row

    def fetchone(self):
        return self._row


class _StubDB:
    """能记录写入、但**不真的落库**的假连接（模拟队列模式下的丢失）。"""

    def __init__(self, *, row=None, fail_on_write: Exception | None = None) -> None:
        self.row = row
        self.fail_on_write = fail_on_write
        self.writes: list[str] = []

    def execute(self, sql: str, params: tuple = ()):
        if sql.strip().upper().startswith("SELECT"):
            return _StubCursor(self.row)
        if self.fail_on_write is not None:
            raise self.fail_on_write
        self.writes.append(sql)
        return _StubCursor()

    def commit(self) -> None:
        return None


def _engine(db, llm=None):
    return MemoryEngine(
        llm_service=llm or _StubLLM(),
        bible_repository=_BibleRepo(),
        db_connection=db,
    )


def _state(engine, chapter: int = 3):
    state = engine._get_or_load_state("n-1")
    state.last_updated_chapter = chapter
    return state


# ─────────────────────── 1) 落库抛异常 ⇒ 必须报出来 ───────────────────────


def test_a_persist_failure_is_reported_instead_of_swallowed():
    """⚠ ``_upsert_state_row`` 抛异常 ⇒ ``_persist_state`` 必须**返回错误**。"""
    db = _StubDB(fail_on_write=RuntimeError("磁盘炸了"))
    engine = _engine(db)

    error = engine._persist_state("n-1", _state(engine))

    assert error, "落库失败了却什么都没返回 ⇒ 上游会判「已更新」"
    assert "磁盘炸了" in error, error


def test_a_persist_failure_reaches_the_result_errors(monkeypatch):
    """⚠ **生产形状**：真实走 ``update_from_chapter`` ⇒ 错误必须进 ``result["errors"]``。"""
    from application.engine.services import memory_engine as memory_engine_module

    monkeypatch.setattr(
        memory_engine_module, "get_prompt_gateway", lambda: _StubGateway(), raising=True
    )

    db = _StubDB(fail_on_write=RuntimeError("磁盘炸了"))
    engine = _engine(db)

    import asyncio

    result = asyncio.run(
        engine.update_from_chapter(
            novel_id="n-1", chapter_number=3, content="正文", outline="大纲"
        )
    )

    assert result["errors"], f"落库失败却没进 errors：{result}"
    assert any("磁盘炸了" in str(item) for item in result["errors"]), result


async def test_the_caller_reports_not_updated_when_the_persist_fails(monkeypatch):
    """⚠ 上游 ``_run_memory_update`` 必须把「errors 非空」读成「未更新」+ 给出原因。

    ⚠ 这条只钉**链路的一环**：``_run_memory_update`` 只看 ``result["errors"]``。
      ⚠ 刻意**不**在这里走真实落库：那个函数会自建 MemoryEngine（用全局 DB），
        把它换成真引擎会与本文件另一条用例互相递归 ⇒ 变成「因错而绿」。
      ⇒ 落库那一环由上面两条（``_persist_state`` 报错 / 错误进 ``errors``）分别钉住。
    """
    from application.engine.services import memory_engine as memory_engine_module
    from engine.runtime.daemon_host import DaemonHostMixin

    async def _fake_update(self, **kwargs):
        return {
            "new_beats": 1,
            "new_clues": 0,
            "violations": 0,
            "errors": ["MemoryEngine 状态持久化失败: 磁盘炸了"],
        }

    monkeypatch.setattr(
        memory_engine_module.MemoryEngine, "update_from_chapter", _fake_update, raising=True
    )

    result = await DaemonHostMixin._run_memory_update(
        SimpleNamespace(llm_service=None), "n-1", 3, "正文内容", "大纲"
    )

    assert result["updated"] is False, "errors 非空却报成功 ⇒ 静默谎报"
    assert "磁盘炸了" in str(result.get("reason") or ""), result


# ─────────────────────── 2) 「入队成功但没真落库」也要发现 ───────────────────────


def test_a_write_that_never_landed_is_detected():
    """⚠ 队列模式下最隐蔽的形态：**入队成功**，但真正的 INSERT 没落地。

    ⚠ 这种形态**不会抛任何异常** ⇒ 只看「execute 没报错」等于什么都没验
    ⇒ 必须**回读确认**。
    """
    db = _StubDB(row=None)  # 写入「成功」，但回读不到该行
    engine = _engine(db)

    error = engine._persist_state("n-1", _state(engine))

    assert error, "写没落地却报成功 ⇒ 记忆一条没存，系统却报「一切正常」"
    assert "未确认" in error, error


def test_a_row_with_the_wrong_chapter_is_detected():
    """⚠ 回读到的章节号与预期不符 ⇒ 同样算「没落库」。"""
    db = _StubDB(row=(1,))  # 期望 3，回读是 1
    engine = _engine(db)

    error = engine._persist_state("n-1", _state(engine, chapter=3))

    assert error, "回读章节号对不上却报成功"
    assert "未确认" in error, error


# ─────────────────────── 3) 成功路径不许误报 ───────────────────────


def test_a_successful_persist_reports_no_error():
    """⚠ 成功 ⇒ 返回 ``None``（零行为变化：不许凭空多出一条「失败」）。"""
    db = _StubDB(row=(3,))
    engine = _engine(db)

    assert engine._persist_state("n-1", _state(engine, chapter=3)) is None


def test_a_real_database_round_trip_still_works(tmp_path):
    """⚠ 真库往返：落库 + 回读确认都通过（不许把「正常路径」也判成失败）。"""
    from infrastructure.persistence.database.connection import get_database

    db = get_database(str(tmp_path / "memory.db"))
    engine = _engine(db)

    assert engine._persist_state("n-1", _state(engine, chapter=3)) is None, (
        "真库落库被误判成失败 ⇒ 记忆回写会一直报「未更新」"
    )


def test_the_memory_engine_without_a_database_is_not_a_failure():
    """⚠ 仅内存模式（没有 DB 连接）⇒ 不算失败（否则会误报「未更新」）。"""
    engine = MemoryEngine(
        llm_service=None,
        bible_repository=_BibleRepo(),
        db_connection=None,
    )

    assert engine._persist_state("n-1", _state(engine)) is None
