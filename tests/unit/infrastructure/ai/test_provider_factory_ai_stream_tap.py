"""DynamicLLMService 的 AI 事件埋点测试。

重点钉死两条：
1. 埋点确实发出 start/delta/end/error；
2. 埋点抛异常时 generate 的返回值不受影响（观测绝不能拖垮写作）。
"""
import asyncio

import pytest

from application.ai.trace_context import TraceContext, set_current_trace, reset_current_trace
from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from infrastructure.ai import provider_factory as pf
from infrastructure.ai.provider_factory import DynamicLLMService


class _RecordingBus:
    def __init__(self):
        self.calls = []

    def publish_start(self, **kw):
        self.calls.append(("start", kw))

    def publish_delta(self, **kw):
        self.calls.append(("delta", kw))

    def publish_end(self, **kw):
        self.calls.append(("end", kw))

    def publish_error(self, **kw):
        self.calls.append(("error", kw))

    def kinds(self):
        return [k for k, _ in self.calls]


class _StubProvider:
    """最小 provider：generate 返回固定内容，stream_generate 逐块产出。"""

    def __init__(self, chunks=("风", "雪", "压", "着", "屋", "檐")):
        self._chunks = chunks

    async def generate(self, prompt, config):
        return GenerationResult(
            content="".join(self._chunks),
            token_usage=TokenUsage(input_tokens=120, output_tokens=60),
        )

    async def stream_generate(self, prompt, config):
        for chunk in self._chunks:
            yield chunk


def _service(bus):
    svc = DynamicLLMService.__new__(DynamicLLMService)
    # _resolve_provider 真实签名是 `async def`，替身必须可 await。
    async def _resolve():
        return _StubProvider()

    svc._resolve_provider = _resolve
    svc._merge_config = lambda config, provider: config
    svc._request_metadata = lambda provider, config, stream=False: (None, "default", "test-model", {})
    return svc


@pytest.fixture
def bus(monkeypatch):
    recorder = _RecordingBus()
    monkeypatch.setattr("application.engine.services.ai_stream_bus.ai_stream_bus", recorder)
    return recorder


@pytest.fixture
def trace_ctx():
    ctx = TraceContext.create(novel_id="n1", operation="llm_generate", stage="writing", stage_label="正文生成")
    token = set_current_trace(ctx)
    yield ctx
    reset_current_trace(token)


def test_generate_publishes_start_and_end(bus, trace_ctx):
    svc = _service(bus)
    asyncio.run(svc.generate(Prompt(system="s", user="u"), pf.GenerationConfig()))
    assert bus.kinds() == ["start", "end"]
    start_kw = bus.calls[0][1]
    assert start_kw["novel_id"] == "n1"
    assert start_kw["stage_label"] == "正文生成"
    assert start_kw["model"] == "test-model"
    assert start_kw["prompt"]["user"] == "u"
    assert bus.calls[1][1]["response"] == "风雪压着屋檐"


def test_stream_generate_publishes_deltas_then_end(bus, trace_ctx):
    svc = _service(bus)
    async def run():
        return [c async for c in svc.stream_generate(Prompt(system="s", user="u"), pf.GenerationConfig())]

    chunks = asyncio.run(run())
    assert chunks == ["风", "雪", "压", "着", "屋", "檐"]
    kinds = bus.kinds()
    assert kinds[0] == "start"
    assert kinds[-1] == "end"
    assert "delta" in kinds
    end_kw = bus.calls[-1][1]
    assert end_kw["response"] == "风雪压着屋檐"


def test_publish_failure_does_not_break_generate(monkeypatch, trace_ctx):
    """埋点抛异常时 generate 必须照常返回结果。"""
    class _Boom:
        def publish_start(self, **kw):
            raise RuntimeError("bus exploded")

        def publish_delta(self, **kw):
            raise RuntimeError("bus exploded")

        def publish_end(self, **kw):
            raise RuntimeError("bus exploded")

        def publish_error(self, **kw):
            raise RuntimeError("bus exploded")

    monkeypatch.setattr("application.engine.services.ai_stream_bus.ai_stream_bus", _Boom())
    svc = _service(_Boom())
    result = asyncio.run(svc.generate(Prompt(system="s", user="u"), pf.GenerationConfig()))
    assert result.content == "风雪压着屋檐"


def test_publish_failure_does_not_break_stream_generate(monkeypatch, trace_ctx):
    class _Boom:
        def publish_start(self, **kw):
            raise RuntimeError("bus exploded")

        def publish_delta(self, **kw):
            raise RuntimeError("bus exploded")

        def publish_end(self, **kw):
            raise RuntimeError("bus exploded")

        def publish_error(self, **kw):
            raise RuntimeError("bus exploded")

    monkeypatch.setattr("application.engine.services.ai_stream_bus.ai_stream_bus", _Boom())
    svc = _service(_Boom())

    async def run():
        return [c async for c in svc.stream_generate(Prompt(system="s", user="u"), pf.GenerationConfig())]

    assert asyncio.run(run()) == ["风", "雪", "压", "着", "屋", "檐"]
