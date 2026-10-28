"""第 34 批 · 账 B：Anthropic 的 **SDK 流式兜底**不该把非 SDK 参数当顶层 kwarg。

⚠ 背景（第 32 批实测发现的**潜伏** bug）
--------------------------------------
``client.messages.stream()`` 的签名是**显式关键字**
（**没有** ``temperature``、**没有** ``top_p``，也没有 ``reasoning_effort`` 之类）
⇒ 把它们当**顶层 kwarg** 传进去 ⇒ **TypeError**（unexpected keyword argument）
⇒ 这条兜底路径**根本跑不了**。
⚠ 生产日志里它**从未触发**（"Falling back to SDK stream" 全是测试模型名）⇒ 潜伏。

⚠ 修法：只有 SDK **认的**那几项留顶层，其余（含 temperature 与画像的 provider 参数）
  塞进 ``extra_body`` ⇒ SDK 会把它原样并进请求 JSON
  ⇒ ⇒ **最终请求体与 httpx 路径逐字相同**（只是不再崩）。
"""
from __future__ import annotations

import inspect

import pytest

from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from infrastructure.ai.providers.anthropic_provider import AnthropicProvider


def _httpx_payload() -> dict:
    """httpx 路径的请求体形状（含画像 extra_body 平铺后的样子）。"""
    return {
        "model": "test-anthropic-model",
        "max_tokens": 1024,
        "temperature": 0.2,
        "system": "你是审查员",
        "messages": [{"role": "user", "content": "正文"}],
        "top_p": 0.95,
        "reasoning_effort": "max",
        "thinking": {"clear_thinking": False},
    }


# ── ① 拆法：非 SDK 参数进 extra_body ───────────────────────────────────


def test_the_non_sdk_params_go_into_extra_body():
    """⚠ temperature / top_p / reasoning_effort / thinking ⇒ 全部进 extra_body。"""
    sdk_payload = AnthropicProvider._build_sdk_stream_payload(_httpx_payload())

    assert set(sdk_payload) == {"model", "max_tokens", "messages", "system", "extra_body"}
    assert sdk_payload["extra_body"] == {
        "temperature": 0.2,
        "top_p": 0.95,
        "reasoning_effort": "max",
        "thinking": {"clear_thinking": False},
    }


def test_every_top_level_kwarg_is_accepted_by_the_installed_sdk():
    """⚠ **这条就是能抓住账 B 的守卫**：用**真装的 SDK 签名**校验。

    ⚠ 旧实现把画像的键当顶层 kwarg 传 ⇒ SDK 签名里没有 ⇒ TypeError。
    ⇒ 这条守卫直接对着**真实签名**查 ⇒ 以后加任何 provider 参数都不会再炸。
    """
    from anthropic.resources.messages import Messages

    accepted = set(inspect.signature(Messages.stream).parameters)
    sdk_payload = AnthropicProvider._build_sdk_stream_payload(_httpx_payload())

    rejected = [key for key in sdk_payload if key not in accepted]
    assert not rejected, f"SDK 不认这些顶层参数（会 TypeError）：{rejected}"


def test_the_final_json_body_is_identical_to_the_httpx_path():
    """⚠ 拆完之后的**最终请求体**必须与 httpx 路径**逐字相同**（不能少字段）。"""
    payload = _httpx_payload()
    sdk_payload = AnthropicProvider._build_sdk_stream_payload(payload)

    final_body = {k: v for k, v in sdk_payload.items() if k != "extra_body"}
    final_body.update(sdk_payload.get("extra_body", {}))

    assert final_body == payload


def test_a_payload_without_extra_fields_needs_no_extra_body():
    """⚠ 没有多余字段时**不要**塞一个空的 extra_body（少一个键就少一个键）。"""
    sdk_payload = AnthropicProvider._build_sdk_stream_payload(
        {
            "model": "m",
            "max_tokens": 1,
            "system": "s",
            "messages": [{"role": "user", "content": "u"}],
        }
    )

    assert "extra_body" not in sdk_payload


# ── ② 集成：兜底路径真的把拆好的 payload 交给 SDK ───────────────────────


class _AsyncStreamCM:
    def __init__(self, text_stream):
        self.text_stream = text_stream

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _AsyncTextStream:
    """异步文本流（provider 侧是 ``async for text in stream.text_stream``）。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()


class _RecordingMessages:
    def __init__(self):
        self.kwargs = None

    def stream(self, **kwargs):
        self.kwargs = kwargs
        return _AsyncStreamCM(_AsyncTextStream(["好"]))


@pytest.mark.asyncio
async def test_the_fallback_path_hands_the_split_payload_to_the_sdk():
    """⚠ 兜底路径必须交出「拆好的」payload（不是原始 payload）。"""
    from types import SimpleNamespace

    provider = object.__new__(AnthropicProvider)
    provider.settings = SimpleNamespace(
        default_model="test-anthropic-model",
        default_max_tokens=1024,
        default_temperature=1.0,
        extra_body={"top_p": 0.95, "reasoning_effort": "max"},
        extra_headers=None,
        extra_query=None,
        base_url="http://localhost",
        protocol="anthropic",
        provider_name="test",
        timeout_seconds=30,
    )
    recorder = _RecordingMessages()
    provider.async_client = SimpleNamespace(messages=recorder)

    config = GenerationConfig(model="test-anthropic-model", max_tokens=256, temperature=0.2)
    chunks = []
    async for chunk in provider._stream_via_sdk(
        Prompt(system="你是审查员", user="正文"), config
    ):
        chunks.append(chunk)

    assert chunks == ["好"]
    assert recorder.kwargs is not None
    assert "top_p" not in recorder.kwargs
    assert recorder.kwargs["extra_body"]["top_p"] == 0.95
    assert recorder.kwargs["extra_body"]["reasoning_effort"] == "max"
    assert recorder.kwargs["extra_body"]["temperature"] == 0.2
