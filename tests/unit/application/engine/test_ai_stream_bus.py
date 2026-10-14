"""ai_stream_bus 单元测试。

_FakeQueue 沿用 tests/unit/application/engine/test_streaming_bus.py 的写法。
"""
import time
from queue import Empty, Full

from application.engine.services import ai_stream_bus as module_under_test
from application.engine.services.ai_stream_bus import (
    AI_DELTA_FLUSH_CHARS,
    AiStreamBus,
)


class _FakeQueue:
    def __init__(self, maxsize: int | None = None):
        self.items = []
        self.maxsize = maxsize

    def put_nowait(self, item):
        if self.maxsize is not None and len(self.items) >= self.maxsize:
            raise Full
        self.items.append(item)

    def get_nowait(self):
        if not self.items:
            raise Empty
        return self.items.pop(0)

    def qsize(self):
        return len(self.items)


class _FrozenClock:
    """替换模块内的 time 引用，让合并窗口的时间断言不依赖真实时钟。"""

    def __init__(self, t: float):
        self._t = t

    def time(self) -> float:
        return self._t


def test_publish_start_emits_full_envelope():
    q = _FakeQueue()
    bus = AiStreamBus(queue=q)
    bus.publish_start(
        novel_id="n1",
        trace_id="t1",
        span_id="s1",
        stage="pipeline.chapter.write",
        stage_label="正文生成",
        model="doubao-pro-32k",
        profile="default",
        prompt={"system": "你是作者", "user": "写第 47 章"},
    )
    assert len(q.items) == 1
    msg = q.items[0]
    assert msg["kind"] == "start"
    assert msg["novel_id"] == "n1"
    assert msg["trace_id"] == "t1"
    assert msg["span_id"] == "s1"
    assert msg["stage_label"] == "正文生成"
    assert msg["model"] == "doubao-pro-32k"
    assert msg["prompt"] == {"system": "你是作者", "user": "写第 47 章"}
    assert isinstance(msg["ts"], float)


def test_publish_delta_coalesces_below_threshold(monkeypatch):
    q = _FakeQueue()
    bus = AiStreamBus(queue=q)
    monkeypatch.setattr(module_under_test, "time", _FrozenClock(time.time()))
    bus.publish_delta(novel_id="n1", trace_id="t1", span_id="s1", text="ab")
    bus.publish_delta(novel_id="n1", trace_id="t1", span_id="s1", text="cd")
    assert q.items == []
    # 确认是「攒住了」而不是「根本没收到」
    assert bus._pending["s1"] == "abcd"


def test_publish_delta_flushes_at_char_threshold():
    q = _FakeQueue()
    bus = AiStreamBus(queue=q)
    bus.publish_delta(novel_id="n1", trace_id="t1", span_id="s1", text="x" * AI_DELTA_FLUSH_CHARS)
    assert len(q.items) == 1
    assert q.items[0]["kind"] == "delta"
    assert q.items[0]["delta"] == "x" * AI_DELTA_FLUSH_CHARS


def test_publish_delta_flushes_after_time_interval():
    q = _FakeQueue()
    bus = AiStreamBus(queue=q)
    bus._last_flush["s1"] = time.time() - 1.0  # 伪造超时
    bus.publish_delta(novel_id="n1", trace_id="t1", span_id="s1", text="晚到的字")
    assert len(q.items) == 1
    assert q.items[0]["delta"] == "晚到的字"


def test_publish_end_flushes_pending_delta_first():
    q = _FakeQueue()
    bus = AiStreamBus(queue=q)
    bus.publish_delta(novel_id="n1", trace_id="t1", span_id="s1", text="未满阈值")
    bus.publish_end(
        novel_id="n1",
        trace_id="t1",
        span_id="s1",
        response="未满阈值全文",
        token_in=3120,
        token_out=1842,
        latency_ms=12400,
    )
    kinds = [m["kind"] for m in q.items]
    assert kinds == ["delta", "end"]
    assert q.items[1]["response"] == "未满阈值全文"
    assert q.items[1]["token_in"] == 3120
    assert q.items[1]["latency_ms"] == 12400


def test_publish_end_clears_pending_state():
    q = _FakeQueue()
    bus = AiStreamBus(queue=q)
    bus.publish_delta(novel_id="n1", trace_id="t1", span_id="s1", text="abc")
    bus.publish_end(novel_id="n1", trace_id="t1", span_id="s1", response="abc")
    assert bus._pending == {}
    assert bus._last_flush == {}


def test_publish_error_emits_error_kind():
    q = _FakeQueue()
    bus = AiStreamBus(queue=q)
    bus.publish_error(novel_id="n1", trace_id="t1", span_id="s1", error="boom", latency_ms=300)
    assert q.items[0]["kind"] == "error"
    assert q.items[0]["error"] == "boom"
    assert q.items[0]["latency_ms"] == 300


def test_publish_is_noop_when_queue_not_injected(monkeypatch):
    """队列未注入时 publish 必须静默 no-op，不得抛异常。

    这是独立启动 daemon 时的降级路径（`scripts/start_daemon.py` 不创建该队列），
    是最值得钉住的一条。断言缓冲确实被填充、又被 publish_end 正常清理——
    只有 publish_delta / publish_end 真的跑过才会同时成立。
    """
    monkeypatch.setattr(module_under_test, "_ai_stream_queue", None)
    bus = AiStreamBus.__new__(AiStreamBus)  # 绕过 __init__ 的注入
    bus._pending = {}
    bus._pending_len = {}
    bus._last_flush = {}

    bus.publish_start(novel_id="n1", trace_id="t1", span_id="s1")
    bus.publish_delta(novel_id="n1", trace_id="t1", span_id="s1", text="abc")
    assert bus._pending["s1"] == "abc"  # 缓冲确实收到了

    bus.publish_end(novel_id="n1", trace_id="t1", span_id="s1", response="abc")
    assert bus._pending == {}
    assert bus._pending_len == {}
    assert bus._last_flush == {}


def test_publish_drops_event_when_queue_full():
    q = _FakeQueue(maxsize=0)  # 永远满
    bus = AiStreamBus(queue=q)
    bus.publish_start(novel_id="n1", trace_id="t1", span_id="s1")
    assert q.items == []  # 不抛异常、不阻塞


def test_drain_returns_only_matching_novel():
    q = _FakeQueue()
    bus = AiStreamBus(queue=q)
    bus.publish_start(novel_id="n1", trace_id="t1", span_id="s1")
    bus.publish_start(novel_id="n2", trace_id="t2", span_id="s2")
    got = bus.drain("n1")
    assert [m["span_id"] for m in got] == ["s1"]


def test_drain_puts_back_other_novels_messages():
    q = _FakeQueue()
    bus = AiStreamBus(queue=q)
    bus.publish_start(novel_id="n2", trace_id="t2", span_id="s2")
    bus.publish_start(novel_id="n1", trace_id="t1", span_id="s1")
    got = bus.drain("n1")
    assert [m["span_id"] for m in got] == ["s1"]
    # n2 的消息被放回队列（可能在队尾，顺序不保证——前端按 ts 排序）
    assert q.qsize() == 1


def test_drain_returns_empty_without_queue(monkeypatch):
    monkeypatch.setattr(module_under_test, "_ai_stream_queue", None)
    bus = AiStreamBus.__new__(AiStreamBus)
    bus._pending = {}
    bus._last_flush = {}
    assert bus.drain("n1") == []


def test_drain_respects_limit():
    q = _FakeQueue()
    bus = AiStreamBus(queue=q)
    for i in range(5):
        bus.publish_start(novel_id="n1", trace_id="t", span_id=f"s{i}")
    got = bus.drain("n1", limit=2)
    assert len(got) == 2
