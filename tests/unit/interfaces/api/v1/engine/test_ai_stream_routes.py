"""AI 事件 SSE 端点测试。"""
import asyncio
import json
import sqlite3
import time

import pytest

from engine.infrastructure.persistence.trace_store import SqliteTraceStore
from interfaces.api.v1.engine import ai_stream_routes as routes


class _Pool:
    """与 tests/unit/engine/test_ai_trace_store.py 同款最小连接池。"""

    def __init__(self, path):
        self.path = path

    def get_connection(self):
        return sqlite3.connect(self.path)


def _sse_payloads(raw: str):
    """把 SSE 文本拆成 data: 后的 JSON 列表。"""
    out = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block.startswith("data: "):
            continue
        out.append(json.loads(block[len("data: "):]))
    return out


def test_build_event_start_contains_prompt():
    ev = routes._build_event(
        {
            "kind": "start",
            "novel_id": "n1",
            "trace_id": "t1",
            "span_id": "s1",
            "stage": "writing",
            "stage_label": "正文生成",
            "model": "m1",
            "profile": "default",
            "prompt": {"system": "s", "user": "u"},
            "ts": 1.0,
        }
    )
    assert ev["type"] == "start"
    assert ev["span_id"] == "s1"
    assert ev["prompt"]["user"] == "u"


def test_build_event_from_db_span_maps_llm_request_to_start():
    ev = routes._db_span_to_event(
        {
            "span_id": "s1",
            "trace_id": "t1",
            "novel_id": "n1",
            "phase": "llm_request",
            "stage": "writing",
            "stage_label": "正文生成",
            "model": "m1",
            "generation_profile": "default",
            "prompt_full": {"system": "s", "user": "u"},
            "created_at": "2026-10-14T00:00:00.000+00:00",
        }
    )
    assert ev["type"] == "start"
    assert ev["span_id"] == "s1"


def test_build_event_from_db_span_maps_llm_response_to_end():
    ev = routes._db_span_to_event(
        {
            "span_id": "s1",
            "trace_id": "t1",
            "novel_id": "n1",
            "phase": "llm_response",
            "response_full": "正文",
            "token_input": 100,
            "token_output": 50,
            "latency_ms": 1200,
            "error": None,
            "created_at": "2026-10-14T00:00:01.000+00:00",
        }
    )
    assert ev["type"] == "end"
    assert ev["token_in"] == 100
    assert ev["token_out"] == 50
    assert ev["response"] == "正文"


def test_build_event_from_db_span_maps_error():
    ev = routes._db_span_to_event(
        {
            "span_id": "s1",
            "trace_id": "t1",
            "novel_id": "n1",
            "phase": "error",
            "error": "boom",
            "latency_ms": 300,
            "created_at": "2026-10-14T00:00:02.000+00:00",
        }
    )
    assert ev["type"] == "error"
    assert ev["error"] == "boom"


def test_db_span_to_event_ignores_unknown_phase():
    assert routes._db_span_to_event({"phase": "prompt_rendered", "span_id": "s"}) is None


def test_snapshot_events_keeps_newest_and_returns_oldest_first():
    spans = [
        {"span_id": f"s{i}", "trace_id": "t", "novel_id": "n1", "phase": "llm_response",
         "response_full": "x", "created_at": f"2026-10-14T00:00:{i:02d}.000+00:00"}
        for i in range(30)
    ]
    events = routes._snapshot_events(spans, limit=5)
    assert len(events) == 5
    # 保留最新的 5 条，按时间正序返回（前端 store 自己按 ts 倒序排）
    assert [e["span_id"] for e in events] == ["s25", "s26", "s27", "s28", "s29"]


def test_route_module_declares_novels_prefix():
    assert routes.router.prefix == "/novels"


def test_sse_frames_round_trip_and_keep_chinese_unescaped():
    """钉住 SSE 帧格式：`data: ` 前缀 + 空行终止 + 中文不转义。

    前端 EventSource 按 `\\n\\n` 切块、按 `data: ` 剥前缀，任一处写错
    整个弹窗都收不到事件，而上面那些测试全都不会红。
    """
    frames = [
        routes._sse({"type": "connected", "novel_id": "n1", "queue": "mp_queue"}),
        routes._sse({"type": "end", "response": "他说：「走。」"}),
        routes._sse({"type": "ping"}),
    ]
    raw = "".join(frames)

    assert raw.count("\n\n") == 3
    assert all(f.startswith("data: ") for f in frames)
    assert "\\u" not in raw

    payloads = _sse_payloads(raw)
    assert [p["type"] for p in payloads] == ["connected", "end", "ping"]
    assert payloads[1]["response"] == "他说：「走。」"


# ── 端点级：驱动真实的 async generator ─────────────────────────
#
# ⚠️ `ai_stream` 的 generator 体内部做的是
# `from application.engine.services.ai_stream_bus import get_ai_stream_queue`
# —— 函数内 import，绑定的是**调用那一刻**的模块属性。所以必须打
# `bus_mod.get_ai_stream_queue`；打 `routes.get_ai_stream_queue`（该模块上根本
# 没有这个名字）会被 monkeypatch 静默跳过，用例照样绿，实际跑的却是降级分支。


async def _drive(agen, count):
    """取 count 帧后主动关掉生成器。

    `aclose()` 在 yield 点抛 GeneratorExit，而所有 `yield` 都刻意留在
    `except Exception` 之外（GeneratorExit 继承 BaseException），所以关闭是干净的。
    """
    out = []
    for _ in range(count):
        try:
            raw = await agen.__anext__()
        except StopAsyncIteration:
            break
        out.append(_sse_payloads(raw)[0])
    await agen.aclose()
    return out


@pytest.mark.asyncio
async def test_degraded_generator_opens_with_connected_then_polls_db(monkeypatch, tmp_path):
    """降级模式：首帧 connected 且自报 db_poll，随后是首轮轮询出的真实事件。"""
    import application.engine.services.ai_stream_bus as bus_mod

    monkeypatch.setattr(bus_mod, "get_ai_stream_queue", lambda: None)
    store = SqliteTraceStore(_Pool(tmp_path / "gen.db"))
    _seed(store, "n1")
    monkeypatch.setattr(routes, "_trace_store", lambda: store)

    response = await routes.ai_stream("n1")
    assert response.media_type == "text/event-stream"

    payloads = await _drive(response.body_iterator, 3)
    assert [p["type"] for p in payloads] == ["connected", "start", "end"]
    assert payloads[0]["queue"] == "db_poll"
    assert payloads[1]["prompt"] == {"system": "系统全文", "user": "用户全文"}
    assert payloads[2]["response"] == "完整输出全文"


@pytest.mark.asyncio
async def test_queue_generator_opens_with_connected_then_snapshot(monkeypatch, tmp_path):
    """队列模式：首帧 connected 且自报 mp_queue，随后是快照回填。"""
    import application.engine.services.ai_stream_bus as bus_mod

    monkeypatch.setattr(bus_mod, "get_ai_stream_queue", lambda: object())
    store = SqliteTraceStore(_Pool(tmp_path / "q.db"))
    _seed(store, "n1")
    monkeypatch.setattr(routes, "_trace_store", lambda: store)

    payloads = await _drive((await routes.ai_stream("n1")).body_iterator, 2)
    assert [p["type"] for p in payloads] == ["connected", "start"]
    assert payloads[0]["queue"] == "mp_queue"


class _SlowOnceStore:
    """第一次读阻塞（模拟 DB 被写锁住），之后恢复正常。

    这是超时保护真正能救回来的故障形态：DB 卡住时生成器如果傻等，
    `AI_STREAM_MAX_LIFETIME_SECONDS` 那个检查永远到不了，弹窗就静默永久冻结。
    """

    def __init__(self, delay, rows):
        self._delay = delay
        self._rows = rows
        self._calls = 0

    def list_ai_spans_since(self, *args):
        self._calls += 1
        if self._calls == 1:
            time.sleep(self._delay)
            return []
        return [dict(row) for row in self._rows]


@pytest.mark.asyncio
async def test_db_read_timeout_is_contained(monkeypatch, tmp_path):
    """DB 读取超时只丢这一轮，连接不挂死。

    首读阻塞 1.2s（模拟写锁），超时保护必须立刻放弃它；下一轮恢复正常并
    立刻产出 `start`。整条驱动有 1.0s 上限：**去掉**超时保护后，生成器会卡在
    首读上，帧要等 1.2s 之后才出得来，这条用例就会转红。

    注：这里不能用「把 `_trace_store` 换成会 fail 的替身」来验证——生成器写的是
    `_fetch_spans(None, store().list_ai_spans_since, ...)`，`store()` 作为**实参**
    先于 `_fetch_spans` 求值，超时保护罩不住它，替身必然先炸。
    """
    import application.engine.services.ai_stream_bus as bus_mod

    monkeypatch.setattr(bus_mod, "get_ai_stream_queue", lambda: None)
    monkeypatch.setattr(routes, "AI_STREAM_TICK_TIMEOUT_SECONDS", 0.05)
    store = SqliteTraceStore(_Pool(tmp_path / "slow.db"))
    _seed(store, "n1")
    monkeypatch.setattr(
        routes, "_trace_store", lambda: _SlowOnceStore(1.2, store.list_ai_spans_since("n1"))
    )

    agen = (await routes.ai_stream("n1")).body_iterator
    try:
        payloads = await asyncio.wait_for(_drive(agen, 2), timeout=1.0)
    finally:
        await agen.aclose()
    assert [p["type"] for p in payloads] == ["connected", "start"]


def _seed(store: SqliteTraceStore, novel_id: str) -> None:
    store.record_ai_span(
        {
            "trace_id": "t1",
            "span_id": "s1",
            "novel_id": novel_id,
            "phase": "llm_request",
            "prompt_full": {"system": "系统全文", "user": "用户全文"},
            "created_at": "2026-05-28T00:00:00.000+00:00",
        }
    )
    store.record_ai_span(
        {
            "trace_id": "t1",
            "span_id": "s1",
            "novel_id": novel_id,
            "phase": "llm_response",
            "response_full": "完整输出全文",
            "token_input": 10,
            "token_output": 20,
            "latency_ms": 1200,
            "created_at": "2026-05-28T00:00:01.000+00:00",
        }
    )


def test_list_ai_spans_since_returns_deserialized_payloads(tmp_path):
    """真实往返：prompt_full 必须是 dict、response_full 必须是无引号的 str。

    这条钉住 `list_ai_spans_since` 走 `_row_to_ai_span`。删掉那一步，
    两个字段会退回原始列里的 JSON 字符串——正文会带上字面引号。
    """
    store = SqliteTraceStore(_Pool(tmp_path / "trace.db"))
    _seed(store, "n1")

    rows = store.list_ai_spans_since("n1")
    assert [r["phase"] for r in rows] == ["llm_request", "llm_response"]

    events = [routes._db_span_to_event(r) for r in rows]
    assert events[0]["prompt"] == {"system": "系统全文", "user": "用户全文"}
    assert events[1]["response"] == "完整输出全文"
    assert events[1]["token_in"] == 10


def test_list_ai_spans_since_advances_past_cursor(tmp_path):
    """复合游标：同秒两条靠 id 续接，不会重复也不会漏。"""
    store = SqliteTraceStore(_Pool(tmp_path / "cursor.db"))
    store.record_ai_span(
        {
            "trace_id": "t1", "span_id": "s1", "novel_id": "n1",
            "phase": "llm_response", "response_full": "一",
            "created_at": "2026-05-28T00:00:01.000+00:00",
        }
    )
    store.record_ai_span(
        {
            "trace_id": "t1", "span_id": "s2", "novel_id": "n1",
            "phase": "llm_response", "response_full": "二",
            "created_at": "2026-05-28T00:00:01.000+00:00",
        }
    )

    first = store.list_ai_spans_since("n1", "", 0, 1)
    assert [r["span_id"] for r in first] == ["s1"]

    rest = store.list_ai_spans_since("n1", first[0]["created_at"], int(first[0]["id"]), 10)
    assert [r["span_id"] for r in rest] == ["s2"]


def test_list_ai_spans_since_scopes_to_novel_and_skips_other_phases(tmp_path):
    store = SqliteTraceStore(_Pool(tmp_path / "scope.db"))
    _seed(store, "n1")
    _seed(store, "n2")
    store.record_ai_span(
        {
            "trace_id": "t9", "span_id": "s9", "novel_id": "n1",
            "phase": "prompt_rendered", "created_at": "2026-05-28T00:00:02.000+00:00",
        }
    )

    rows = store.list_ai_spans_since("n1")
    assert {r["novel_id"] for r in rows} == {"n1"}
    assert {r["phase"] for r in rows} == {"llm_request", "llm_response"}