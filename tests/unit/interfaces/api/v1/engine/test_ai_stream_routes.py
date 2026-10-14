"""AI 事件 SSE 端点测试。"""
import json
import sqlite3

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