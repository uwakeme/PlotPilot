"""第 66 批守卫：B-1（生产入口透传 ``timeout_seconds``）+ B-2（Anthropic 超时语义）。

本批的由来：独立审查报告
``review-briefs/2026-10-30-review-report-85c0f726-closeout.md`` 的 B-1 / B-2。

B-1
    ``DynamicLLMService._merge_config`` 会**重建** ``GenerationConfig``
    （为了让 model / max_tokens / temperature 落到画像默认值）
    ⇒ 重建时漏一个字段，那个字段就**静默消失**
    ⇒ 第 50 批加的 ``timeout_seconds`` 就没被透传 ⇒ 画像里写超时在
    **生产入口**（``generate`` / ``stream_generate``）被当场剥掉
    ⇒ provider 层接得再好也没用 ⇒ 第 50 批声称兑现的那笔 P1 等于没做。

    ⚠ 这是**第 32 批 ``extra_body`` 同款坑的第二次**（同一个函数、同一类病），
    所以本文件不只钉一个字段，而是钉「**逐字段透传**」这条通则。

B-2
    Anthropic 的 httpx SSE 路径把「没配单次超时」翻译成 **300.0 浮点**
    ⇒ httpx 的「每请求浮点」语义是**整体替换**客户端的**分层**超时
    （connect 30→300 / read 120→300）
    ⇒ 这是第 50 批**引入**的回归（第 50 批之前这条路径根本不传 ``timeout``）
    ⇒ 端点不可达时不再快速失败、流式 chunk 停滞要 5 分钟才判死。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt


def _prompt() -> Prompt:
    return Prompt(system="sys", user="usr")


def _settings(**overrides):
    from infrastructure.ai.config.settings import Settings

    base = {"default_model": "m-1", "api_key": "test-key"}
    base.update(overrides)
    return Settings(**base)


# ───────────────────────── B-1：生产入口逐字段透传 ─────────────────────────


def test_the_merge_never_drops_a_config_field():
    """**显式设过值**的字段，合并后必须原样还在（值相等）。

    ⚠ 第 69 批（R-0）**收窄**了本用例的承诺（原文写「以后新增字段忘了写进来
      ⇒ 守卫立刻红」，**不成立**）：
      本用例遍历 ``vars(config)`` 比值 ⇒ 对**带默认值**的字段，两侧拿到的都是默认值
      ⇒ ``1 == 1`` ⇒ **断言恒真** ⇒ 抓不住「新增字段从未被串进 `_merge_config`」
      —— 而 **B-1 那个 bug 正是这一类**（``timeout_seconds`` 在第 50 批被**新增**进
      ``GenerationConfig``、默认 ``None``，却没串进 ``_merge_config``）。
    ⇒ 「新增字段漏传」由下面那条**集合比对**守卫负责；本条只管**值**。
    """
    from infrastructure.ai.provider_factory import DynamicLLMService

    config = GenerationConfig(
        model="m-not-the-default",
        max_tokens=999999,
        temperature=0.37,
        response_format={"type": "json_schema", "json_schema": {"name": "x", "schema": {}}},
        extra_body={"top_p": 0.5},
        timeout_seconds=42.0,
    )
    provider = SimpleNamespace(
        settings=SimpleNamespace(
            default_model="test-model",
            default_max_tokens=4096,
            default_temperature=0.7,
        )
    )

    merged = DynamicLLMService._merge_config(config, provider)

    for name, value in vars(config).items():
        assert hasattr(merged, name), f"合并后的配置没有字段 {name}"
        assert getattr(merged, name) == value, (
            f"合并后的配置把字段 {name} 丢了：{value!r} → {getattr(merged, name)!r}"
        )


def test_the_merge_passes_every_generation_config_field():
    """⚠ 通则守卫（**真的能抓住新增字段**）：`_merge_config` 传给
    ``GenerationConfig(...)`` 的 kwargs 集合 == ``GenerationConfig.__init__`` 的**全部**参数。

    ⚠ 第 69 批（R-0）新增：上一版守卫**抓不住**「新增字段从未被串进 `_merge_config`」
      （比值恒真）⇒ 而 B-1 那个 bug 正是这一类 ⇒ 这里改成**集合比对**
      （不依赖值）⇒ 以后 ``GenerationConfig`` 加字段忘了写进 `_merge_config`` ⇒ **立刻红**。

    ⚠ 判据用 **AST**（不是文本 contains）⇒ 注释里出现 ``GenerationConfig(`` 不会误判。
    """
    import ast
    import inspect
    import textwrap

    from domain.ai.services.llm_service import GenerationConfig
    from infrastructure.ai.provider_factory import DynamicLLMService

    source = textwrap.dedent(inspect.getsource(DynamicLLMService._merge_config))
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "GenerationConfig"
    ]
    assert len(calls) == 1, (
        f"_merge_config 里应有且只有 1 个 GenerationConfig(...) 调用，实际 {len(calls)} 个"
    )

    passed = {kw.arg for kw in calls[0].keywords}
    expected = set(inspect.signature(GenerationConfig.__init__).parameters) - {"self"}
    missing = expected - passed

    assert not missing, (
        f"_merge_config 没有透传这些字段：{sorted(missing)}"
        f"⇒ 新增字段忘了写进来 ⇒ 生产入口会**静默剥掉**它（B-1 同款）"
    )



def test_the_merged_config_keeps_the_single_call_timeout():
    """⚠ 单独钉一次 ``timeout_seconds``（B-1 的具体形态）。"""
    from infrastructure.ai.provider_factory import DynamicLLMService

    provider = SimpleNamespace(
        settings=SimpleNamespace(
            default_model="test-model",
            default_max_tokens=4096,
            default_temperature=0.7,
        )
    )

    merged = DynamicLLMService._merge_config(
        GenerationConfig(timeout_seconds=42.0), provider
    )

    assert merged.timeout_seconds == 42.0, "生产入口把单次调用的超时剥掉了"


def _recording_service(monkeypatch, seen: list):
    """构造一条**真的走** ``_merge_config`` 的 ``DynamicLLMService``。

    ⚠ 关键：**不**替换 ``_merge_config`` 本身（那是被测对象）。
    只替换 provider 解析与 trace 落库（避免写生产库）。
    """
    from domain.ai.services.llm_service import GenerationResult
    from domain.ai.value_objects.token_usage import TokenUsage
    from infrastructure.ai.provider_factory import DynamicLLMService

    class _RecordingProvider:
        settings = SimpleNamespace(
            default_model="test-model",
            default_max_tokens=4096,
            default_temperature=0.7,
            provider_name="test",
            protocol="openai",
            base_url="http://localhost",
        )

        async def generate(self, prompt, config):
            seen.append(config)
            return GenerationResult(
                content="ok", token_usage=TokenUsage(input_tokens=1, output_tokens=1)
            )

        async def stream_generate(self, prompt, config):
            seen.append(config)
            yield "ok"

    class _NullRecorder:
        def record_span(self, **kwargs):
            return None

    async def _resolve_provider():
        return _RecordingProvider()

    monkeypatch.setattr(
        "infrastructure.ai.provider_factory.get_trace_recorder", lambda: _NullRecorder()
    )
    service = DynamicLLMService.__new__(DynamicLLMService)
    service._resolve_provider = _resolve_provider
    return service


def test_the_dynamic_llm_service_hands_the_timeout_to_the_provider(monkeypatch):
    """⚠ 端到端：provider **收到**的 config 必须带着单次超时。"""
    from infrastructure.ai.provider_factory import DynamicLLMService

    seen: list = []
    service = _recording_service(monkeypatch, seen)

    asyncio.run(service.generate(_prompt(), GenerationConfig(timeout_seconds=42.0)))

    assert seen, "provider 没被调用"
    assert seen[0].timeout_seconds == 42.0, "生产入口把单次调用的超时剥掉了"


def test_the_dynamic_llm_service_hands_the_timeout_to_the_provider_on_stream(monkeypatch):
    """⚠ 流式入口同样走 ``_merge_config`` ⇒ 也要透传。"""
    seen: list = []
    service = _recording_service(monkeypatch, seen)

    async def _drain() -> None:
        async for _ in service.stream_generate(
            _prompt(), GenerationConfig(timeout_seconds=42.0)
        ):
            pass

    asyncio.run(_drain())

    assert seen, "provider 没被调用"
    assert seen[0].timeout_seconds == 42.0, "流式生产入口把单次调用的超时剥掉了"


def test_the_dynamic_llm_service_keeps_no_timeout_as_none(monkeypatch):
    """⚠ 零行为变化：没配单次超时 ⇒ provider 收到的也是 ``None``。"""
    seen: list = []
    service = _recording_service(monkeypatch, seen)

    asyncio.run(service.generate(_prompt(), GenerationConfig()))

    assert seen, "provider 没被调用"
    assert seen[0].timeout_seconds is None


# ───────────────────────── B-2：Anthropic 超时语义 ─────────────────────────


def test_anthropic_resolve_timeout_is_none_when_not_configured():
    """⚠ 没配单次超时 ⇒ **不覆盖**任何客户端默认。

    为什么不能是 300.0：httpx 的「每请求浮点」会**整体替换**分层超时。
    """
    from infrastructure.ai.providers.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(_settings())

    assert provider._resolve_timeout(GenerationConfig(model="m-1")) is None
    assert provider._resolve_timeout(GenerationConfig(model="m-1", timeout_seconds=42.0)) == 42.0


def test_anthropic_sdk_paths_do_not_pass_a_timeout_when_not_configured():
    """⚠ SDK 两条路径都**不传** ``timeout`` ⇒ 用客户端级 300（第 50 批之前的行为）。"""
    from infrastructure.ai.providers.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(_settings())
    config = GenerationConfig(model="m-1")

    create_kwargs = provider._build_create_kwargs(_prompt(), config)
    _, sdk_payload = provider._build_sdk_stream_request(_prompt(), config)

    assert "timeout" not in create_kwargs, "SDK 路径不该硬塞一个客户端级超时"
    assert "timeout" not in sdk_payload, "SDK 流式路径不该硬塞一个客户端级超时"


def test_anthropic_sdk_paths_still_honour_a_configured_timeout():
    """⚠ 能力本身没坏：配了单次超时 ⇒ SDK 顶层参数必须带上。"""
    from infrastructure.ai.providers.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(_settings())
    config = GenerationConfig(model="m-1", timeout_seconds=42.0)

    create_kwargs = provider._build_create_kwargs(_prompt(), config)
    _, sdk_payload = provider._build_sdk_stream_request(_prompt(), config)

    assert create_kwargs["timeout"] == 42.0
    assert sdk_payload["timeout"] == 42.0


@pytest.mark.asyncio
async def test_anthropic_httpx_path_keeps_the_layered_client_timeout():
    """⚠ **生产形状**：真发一次请求，看 httpx 实际用的分层超时。

    为什么不能用「断言传进去的参数」：那只是**形状**；
    真正要钉的是「客户端的 connect 30 / read 120 没有被压平成 300」。
    ⇒ 用 ``MockTransport`` 抓 ``request.extensions['timeout']``。
    """
    import httpx

    from infrastructure.ai.http_timeout import build_httpx_timeout
    from infrastructure.ai.providers.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(_settings())
    original_client = provider._stream_http_client
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = dict(request.extensions.get("timeout", {}))
        return httpx.Response(
            200,
            content=(
                b'event: content_block_delta\n'
                b'data: {"type": "content_block_delta", "delta": '
                b'{"type": "text_delta", "text": "hi"}}\n\n'
            ),
        )

    provider._stream_http_client = httpx.AsyncClient(
        timeout=build_httpx_timeout(provider.settings.http_timeout_settings),
        transport=httpx.MockTransport(_handler),
    )

    try:
        config = GenerationConfig(model="m-1")  # 没配单次超时
        chunks = [chunk async for chunk in provider._stream_via_httpx(_prompt(), config)]
    finally:
        await provider._stream_http_client.aclose()
        await original_client.aclose()

    assert chunks == ["hi"], "SSE 解析没走到（说明这条路径本身坏了，守卫会误判）"
    ext = captured["timeout"]
    assert ext["connect"] == 30.0, f"connect 超时被压平了：{ext}"
    assert ext["read"] == 120.0, f"read 超时被压平了：{ext}"
    assert ext["write"] == 60.0, f"write 超时被压平了：{ext}"
    assert ext["pool"] == 30.0, f"pool 超时被压平了：{ext}"


@pytest.mark.asyncio
async def test_anthropic_httpx_path_applies_a_configured_single_call_timeout():
    """⚠ 配了单次超时 ⇒ 这次请求**就该**用那个值（能力没被 B-2 的修复削掉）。"""
    import httpx

    from infrastructure.ai.http_timeout import build_httpx_timeout
    from infrastructure.ai.providers.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(_settings())
    original_client = provider._stream_http_client
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = dict(request.extensions.get("timeout", {}))
        return httpx.Response(
            200,
            content=(
                b'event: content_block_delta\n'
                b'data: {"type": "content_block_delta", "delta": '
                b'{"type": "text_delta", "text": "hi"}}\n\n'
            ),
        )

    provider._stream_http_client = httpx.AsyncClient(
        timeout=build_httpx_timeout(provider.settings.http_timeout_settings),
        transport=httpx.MockTransport(_handler),
    )

    try:
        config = GenerationConfig(model="m-1", timeout_seconds=42.0)
        async for _ in provider._stream_via_httpx(_prompt(), config):
            pass
    finally:
        await provider._stream_http_client.aclose()
        await original_client.aclose()

    ext = captured["timeout"]
    assert ext["read"] == 42.0, f"单次调用超时没生效：{ext}"
