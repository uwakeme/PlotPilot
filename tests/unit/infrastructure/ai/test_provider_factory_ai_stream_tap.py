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
    """最小 provider：generate 返回固定内容，stream_generate 逐块产出。

    `boom=True` 时两个入口都抛异常，用来覆盖 publish_error 分支。
    """

    def __init__(self, chunks=("风", "雪", "压", "着", "屋", "檐"), boom=False):
        self._chunks = chunks
        self._boom = boom

    async def generate(self, prompt, config):
        if self._boom:
            raise RuntimeError("provider exploded")
        return GenerationResult(
            content="".join(self._chunks),
            token_usage=TokenUsage(input_tokens=120, output_tokens=60),
        )

    async def stream_generate(self, prompt, config):
        if self._boom:
            raise RuntimeError("provider exploded")
        for chunk in self._chunks:
            yield chunk


def _service(boom: bool = False):
    """构造一个只挂必要替身的 DynamicLLMService，绕开 __init__ 的 provider 解析。

    注意 `_resolve_provider` 在真实实现里是 `async def`——`provider_factory.py:291`
    的调用点是 `await self._resolve_provider()`。替身必须也是 async，否则四条用例
    全部挂在 `TypeError: '...' object can't be awaited`。
    `_merge_config` / `_request_metadata` 是普通 def，照原样。
    """
    async def _resolve_provider():
        return _StubProvider(boom=boom)

    svc = DynamicLLMService.__new__(DynamicLLMService)
    svc._resolve_provider = _resolve_provider
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
    svc = _service()
    asyncio.run(svc.generate(Prompt(system="s", user="u"), pf.GenerationConfig()))
    assert bus.kinds() == ["start", "end"]
    start_kw = bus.calls[0][1]
    assert start_kw["novel_id"] == "n1"
    assert start_kw["stage_label"] == "正文生成"
    assert start_kw["model"] == "test-model"
    assert start_kw["prompt"]["user"] == "u"
    assert bus.calls[1][1]["response"] == "风雪压着屋檐"


def test_stream_generate_publishes_deltas_then_end(bus, trace_ctx):
    svc = _service()
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
    svc = _service()
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
    svc = _service()

    async def run():
        return [c async for c in svc.stream_generate(Prompt(system="s", user="u"), pf.GenerationConfig())]

    assert asyncio.run(run()) == ["风", "雪", "压", "着", "屋", "檐"]


def test_generate_publishes_error_when_provider_raises(bus, trace_ctx):
    """provider 抛异常时必须发出 start → error。

    这条路径靠 `_publish_ai_stream` 的 getattr 按字符串分发，如果事件名拼错
    （例如 "publish_errror"），会抛 AttributeError 并被 helper 吞成 debug 日志——
    事件永远不发，弹窗会在每次 provider 失败时卡在「进行中」。必须有测试钉住。
    """
    svc = _service(boom=True)
    with pytest.raises(RuntimeError, match="provider exploded"):
        asyncio.run(svc.generate(Prompt(system="s", user="u"), pf.GenerationConfig()))
    assert bus.kinds() == ["start", "error"]
    assert bus.calls[-1][1]["error"] == "provider exploded"


def test_stream_generate_publishes_error_when_provider_raises(bus, trace_ctx):
    svc = _service(boom=True)

    async def run():
        return [c async for c in svc.stream_generate(Prompt(system="s", user="u"), pf.GenerationConfig())]

    with pytest.raises(RuntimeError, match="provider exploded"):
        asyncio.run(run())
    assert bus.kinds() == ["start", "error"]


def test_publish_skipped_when_trace_has_no_novel_id(bus):
    """没有 novel_id 的调用一律不发布。

    弹窗按书过滤，`drain()` 只取 `message["novel_id"] == novel_id`，别的书的消息
    会被放回队尾并消耗每轮 200 条的取用预算。空 novel_id 永远匹配不上，会无限回流、
    挤掉真实事件。ai_traces 的持久化不受影响。
    """
    ctx = TraceContext.create(novel_id="", operation="llm_generate", stage="s", stage_label="无小说上下文")
    token = set_current_trace(ctx)
    try:
        svc = _service()
        asyncio.run(svc.generate(Prompt(system="s", user="u"), pf.GenerationConfig()))
    finally:
        reset_current_trace(token)
    assert bus.kinds() == []
