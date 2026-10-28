"""第 32 批：**单次调用**可覆盖 profile 的 provider 参数（默认零行为变化）。

背景（生产实测，第 31 批）
--------------------------
A1 设定漂移审计单次调用 **~300 秒**，`output_tokens=12560` ⇒ **模型在长思考**
⇒ 而 provider 级的参数（``extra_body``：``reasoning_effort`` / ``thinking`` …）
**只来自激活的生成画像** ⇒ **单次调用改不了** ⇒ 审计这类「核对型」任务
也只能陪着一起深思。

⇒ 这里开一个**单次覆盖**的口子：``GenerationConfig.extra_body``
⇒ 与画像的 ``extra_body`` **合并**（**同名键以单次为准**）
⇒ ⚠ 默认 ``None`` ⇒ 行为与改造前**完全一致**。

⚠ 守卫纪律：走**真实**的 provider 载荷构造方法（不复读源码）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt

PROFILE_EXTRA = {"top_p": 0.95, "reasoning_effort": "max"}
CALL_EXTRA = {"reasoning_effort": "none"}


def _settings(extra_body=None):
    """最小 settings（只放 provider 载荷构造会读到的字段）。"""
    return SimpleNamespace(
        default_model="test-model",
        default_max_tokens=4096,
        default_temperature=0.7,
        extra_body=extra_body,
        extra_headers=None,
        extra_query=None,
        base_url="http://localhost",
        protocol="openai",
        provider_name="test",
        timeout_seconds=30,
        use_legacy_chat_completions=False,
    )


def _prompt() -> Prompt:
    return Prompt(system="你是审查员", user="正文与基准")


# ── ① 配置层：默认零变化 + 合并语义 ───────────────────────────────────


def test_the_extra_body_defaults_to_none():
    """⚠ 默认 None ⇒ **行为零变化**（画像的 extra_body 原样生效）。"""
    assert GenerationConfig().extra_body is None
    assert GenerationConfig(extra_body=None).extra_body is None


def test_the_extra_body_is_copied_not_shared():
    """⚠ 传进来的 dict 要**拷贝** ⇒ 调用方后改不影响已构造的配置。"""
    payload = {"reasoning_effort": "none"}
    config = GenerationConfig(extra_body=payload)
    payload["reasoning_effort"] = "max"

    assert config.extra_body == {"reasoning_effort": "none"}


def test_merge_extra_body_keeps_the_profile_when_the_call_does_not_override():
    """⚠ 单次**没传** ⇒ 结果与画像**逐字相同**（默认零变化的保证）。"""
    from infrastructure.ai.providers.base import merge_extra_body

    settings = _settings(PROFILE_EXTRA)
    merged = merge_extra_body(settings, GenerationConfig())

    assert merged == PROFILE_EXTRA


def test_merge_extra_body_lets_the_call_win_on_the_same_key():
    """⚠ 同名键以**单次**为准（这正是「单点可调」的意义）。"""
    from infrastructure.ai.providers.base import merge_extra_body

    settings = _settings(PROFILE_EXTRA)
    merged = merge_extra_body(settings, GenerationConfig(extra_body=CALL_EXTRA))

    assert merged["reasoning_effort"] == "none"
    # ⚠ **不同名**的键要**合并**而不是整块替换（否则会丢掉画像的 top_p）
    assert merged["top_p"] == 0.95


# ── ② 三个 provider 都要把单次覆盖送进请求体 ─────────────────────────


def test_the_openai_provider_sends_the_call_override():
    """⚠ OpenAI 兼容：请求体的 ``extra_body`` 必须含单次覆盖。"""
    from infrastructure.ai.providers.openai_provider import OpenAIProvider

    provider = object.__new__(OpenAIProvider)
    provider.settings = _settings(PROFILE_EXTRA)
    messages = [{"role": "user", "content": "正文"}]

    kwargs = provider._build_chat_request_kwargs(
        messages, GenerationConfig(extra_body=CALL_EXTRA)
    )

    assert kwargs["extra_body"]["reasoning_effort"] == "none"
    assert kwargs["extra_body"]["top_p"] == 0.95


def test_the_anthropic_provider_sends_the_call_override():
    """⚠ Anthropic：流式载荷必须含单次覆盖。"""
    from infrastructure.ai.providers.anthropic_provider import AnthropicProvider

    provider = object.__new__(AnthropicProvider)
    provider.settings = _settings(PROFILE_EXTRA)

    _model_id, payload = provider._build_message_request(
        _prompt(), GenerationConfig(extra_body=CALL_EXTRA), stream=True
    )

    assert payload["reasoning_effort"] == "none"
    assert payload["top_p"] == 0.95


def test_the_gemini_provider_sends_the_call_override():
    """⚠ Gemini：载荷必须含单次覆盖。"""
    from infrastructure.ai.providers.gemini_provider import GeminiProvider

    provider = object.__new__(GeminiProvider)
    provider.settings = _settings(PROFILE_EXTRA)

    payload = provider._build_payload(_prompt(), GenerationConfig(extra_body=CALL_EXTRA))

    assert payload["reasoning_effort"] == "none"
    assert payload["top_p"] == 0.95


# ── ③ 不传时请求体与改造前**逐字相同** ───────────────────────────────


def test_without_the_override_the_request_body_is_unchanged():
    """⚠ 零行为变化的**硬证明**：不传单次覆盖 ⇒ 请求体里的 extra_body
    就是画像那份（**不多一个键、不少一个键**）。"""
    from infrastructure.ai.providers.openai_provider import OpenAIProvider

    provider = object.__new__(OpenAIProvider)
    provider.settings = _settings(PROFILE_EXTRA)
    messages = [{"role": "user", "content": "正文"}]

    kwargs = provider._build_chat_request_kwargs(messages, GenerationConfig())

    assert kwargs["extra_body"] == PROFILE_EXTRA


# ── ④ **每一条**请求体路径都必须带单次覆盖 ─────────────────────────────
#
# ⚠ 为什么单列这一条：第 32 批**实测**发现 —— OpenAI 的 **Responses API**
#   （非 legacy 的**默认路径**，`generate()` 先试它）**只带画像的 extra_body**
#   ⇒ 单次覆盖在那条路径上**静默失效**（不报错、不告警、面板上看不出来）。
# ⇒ 这里把「所有 provider 的所有请求体路径」列成**一张表**
#   ⇒ 新增路径时必须同时进表 ⇒ 同样的缺口不会再漏第二次。


def _openai_chat_body(provider, config):
    kwargs = provider._build_chat_request_kwargs(
        [{"role": "user", "content": "正文"}], config
    )
    return kwargs["extra_body"]


def _openai_responses_body(provider, config):
    kwargs = provider._build_responses_request_kwargs(_prompt(), config)
    # ⚠ Responses API 没有 extra_body 字段 ⇒ 这些键**平铺**在请求体顶层
    return kwargs


def _anthropic_create_body(provider, config):
    return provider._build_create_kwargs(_prompt(), config)["extra_body"]


def _anthropic_message_body(provider, config):
    _model_id, payload = provider._build_message_request(_prompt(), config, stream=True)
    return payload


def _gemini_payload_body(provider, config):
    return provider._build_payload(_prompt(), config)


def test_every_request_body_path_carries_the_call_override():
    """⚠ **每一条**请求体路径都要带上单次覆盖（否则该路径上能力静默失效）。

    ⚠ 表里的 ``profile`` 列标的是「这条路径**还带不带**画像的 extra_body」
    ⇒ Anthropic 的 ``create_kwargs`` 是**在案未修**的例外
    （登记表「二十点十四·②」：画像的 extra_body 在这条路径上至今没生效）。
    """
    from infrastructure.ai.providers.anthropic_provider import AnthropicProvider
    from infrastructure.ai.providers.gemini_provider import GeminiProvider
    from infrastructure.ai.providers.openai_provider import OpenAIProvider

    def _provider(cls):
        provider = object.__new__(cls)
        provider.settings = _settings(PROFILE_EXTRA)
        return provider

    # (路径名, 取请求体的方法, provider 类, 这条路径带不带画像的 extra_body)
    paths = [
        ("OpenAI / chat completions", _openai_chat_body, OpenAIProvider, True),
        ("OpenAI / responses API", _openai_responses_body, OpenAIProvider, True),
        ("Anthropic / create_kwargs", _anthropic_create_body, AnthropicProvider, False),
        ("Anthropic / messages", _anthropic_message_body, AnthropicProvider, True),
        ("Gemini / payload", _gemini_payload_body, GeminiProvider, True),
    ]

    for label, extract, cls, with_profile in paths:
        body = extract(_provider(cls), GenerationConfig(extra_body=CALL_EXTRA))
        assert body.get("reasoning_effort") == "none", f"{label} 丢了单次覆盖"
        if with_profile:
            assert body.get("top_p") == 0.95, f"{label} 丢了画像的键"
        else:
            assert "top_p" not in body, f"{label} 不该带画像的键"


def test_the_openai_responses_path_sends_the_call_override():
    """⚠ OpenAI 兼容的 **Responses API**（非 legacy 的**默认路径**）也必须带单次覆盖。

    ⚠ 这条是第 32 批**实测**补上的：原实现这条路径只带画像的 extra_body
    ⇒ 单次覆盖在**默认路径**上静默失效。
    """
    from infrastructure.ai.providers.openai_provider import OpenAIProvider

    provider = object.__new__(OpenAIProvider)
    provider.settings = _settings(PROFILE_EXTRA)

    kwargs = provider._build_responses_request_kwargs(
        _prompt(), GenerationConfig(extra_body=CALL_EXTRA)
    )

    assert kwargs["reasoning_effort"] == "none"
    assert kwargs["top_p"] == 0.95


def test_the_anthropic_create_kwargs_sends_the_call_override():
    """⚠ Anthropic 的 ``generate()`` 路径（create_kwargs）也必须带单次覆盖。"""
    from infrastructure.ai.providers.anthropic_provider import AnthropicProvider

    provider = object.__new__(AnthropicProvider)
    provider.settings = _settings(PROFILE_EXTRA)

    kwargs = provider._build_create_kwargs(_prompt(), GenerationConfig(extra_body=CALL_EXTRA))

    assert kwargs["extra_body"]["reasoning_effort"] == "none"


def test_the_openai_responses_path_without_the_override_keeps_the_profile():
    """⚠ 零行为变化：Responses 路径**不传**单次覆盖 ⇒ 画像那份**原样**平铺上去。"""
    from infrastructure.ai.providers.openai_provider import OpenAIProvider

    provider = object.__new__(OpenAIProvider)
    provider.settings = _settings(PROFILE_EXTRA)

    kwargs = provider._build_responses_request_kwargs(_prompt(), GenerationConfig())

    assert kwargs["reasoning_effort"] == "max"
    assert kwargs["top_p"] == 0.95


def test_the_anthropic_create_kwargs_without_the_override_is_unchanged():
    """⚠ 零行为变化：不传单次覆盖 ⇒ extra_body **只有** temperature（与改造前一致）。"""
    from infrastructure.ai.providers.anthropic_provider import AnthropicProvider

    provider = object.__new__(AnthropicProvider)
    provider.settings = _settings(PROFILE_EXTRA)
    config = GenerationConfig()

    kwargs = provider._build_create_kwargs(_prompt(), config)

    assert kwargs["extra_body"] == {"temperature": config.temperature}


def test_the_anthropic_create_kwargs_still_leaves_the_profile_extra_body_out():
    """⚠ **如实钉住在案的 bug**（登记表「二十点十四·②」，本批**不修**）：

    Anthropic 的 ``generate()`` 路径**至今不带画像的 extra_body**
    ⇒ 画像里的 ``reasoning_effort: max`` 在这条路径上没生效。

    ⚠ 这条断言是**故意**钉住现状的：真去修它时，这条会红
    ⇒ 那时必须同步更新登记表（并重新评估 A1 的耗时）。
    """
    from infrastructure.ai.providers.anthropic_provider import AnthropicProvider

    provider = object.__new__(AnthropicProvider)
    provider.settings = _settings(PROFILE_EXTRA)

    kwargs = provider._build_create_kwargs(_prompt(), GenerationConfig())

    assert "reasoning_effort" not in kwargs["extra_body"]
    assert "top_p" not in kwargs["extra_body"]


# ── ⑥ 生产入口（DynamicLLMService）必须把覆盖**透传**下去 ─────────────
#
# ⚠ 为什么单列这一段：``DynamicLLMService._merge_config`` 会**重建**
#   ``GenerationConfig``（为了让 model / max_tokens / temperature 落到画像默认值）
#   ⇒ 重建时漏一个字段，那个字段就**静默消失**
#   ⇒ 本批实测就栽在这：provider 层接得好好的，生产入口却把 extra_body 剥了
#   ⇒ **能力等于不存在**（不报错、不告警）。


def test_the_merged_config_keeps_the_call_override():
    """⚠ ``_merge_config`` 重建配置时**必须**带上单次覆盖。"""
    from infrastructure.ai.provider_factory import DynamicLLMService

    provider = SimpleNamespace(
        settings=SimpleNamespace(
            default_model="test-model",
            default_max_tokens=4096,
            default_temperature=0.7,
        )
    )

    merged = DynamicLLMService._merge_config(
        GenerationConfig(extra_body=CALL_EXTRA), provider
    )

    assert merged.extra_body == CALL_EXTRA


def test_the_dynamic_llm_service_hands_the_call_override_to_the_provider(monkeypatch):
    """⚠ 端到端：provider **收到**的 config 必须带着单次覆盖。"""
    from infrastructure.ai.provider_factory import DynamicLLMService
    from domain.ai.services.llm_service import GenerationResult
    from domain.ai.value_objects.token_usage import TokenUsage

    seen: list = []

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

    asyncio.run(service.generate(_prompt(), GenerationConfig(extra_body=CALL_EXTRA)))

    assert seen, "provider 没被调用"
    assert seen[0].extra_body == CALL_EXTRA


def test_the_dynamic_llm_service_keeps_no_override_as_none(monkeypatch):
    """⚠ 零行为变化：没传单次覆盖 ⇒ provider 收到的也是 ``None``（不是空 dict）。"""
    from infrastructure.ai.provider_factory import DynamicLLMService
    from domain.ai.services.llm_service import GenerationResult
    from domain.ai.value_objects.token_usage import TokenUsage

    seen: list = []

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

    asyncio.run(service.generate(_prompt(), GenerationConfig()))

    assert seen, "provider 没被调用"
    assert seen[0].extra_body is None
