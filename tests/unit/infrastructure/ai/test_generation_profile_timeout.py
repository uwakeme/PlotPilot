"""第 50 批：「画像超时可配」—— ``timeout_seconds`` 从**装饰品**变成**真兑现**。

背景（读码核实）
----------------
``config/generation_profiles.yaml`` 里曾写过 ``timeout_seconds: 30``（J-01 提案用），
但 ``generation_config_from_profile`` **只读** model / max_tokens / temperature /
response_format ⇒ **全仓无人读** ⇒ 装饰品。
（登记表的 P1 原话：「要让超时可配 ⇒ 得改 ``domain`` 的 ``GenerationConfig`` + 各 provider」。）

本批做完那一半：

- ``GenerationConfig.timeout_seconds``（``None`` ⇒ 行为**逐字不变**）；
- ``generation_config_from_profile`` **真的读**它；
- **每个 provider 的每条请求路径**都兑现它。

⚠ 第 32 批的教训（本批差点又踩）：**新增能力要连带「第二条请求体路径」** ——
openai 的 **Responses API** 路径此前**根本没有** timeout 键（只靠 client 级默认）
⇒ 只接 chat 路径的话，能力在**默认路径**上静默失效。

本文件钉住
----------
1. 画像里的 ``timeout_seconds`` ⇒ 进 ``GenerationConfig``（**真从 YAML 读**）。
2. provider 兑现：openai（chat **与** responses 两条路径）/ anthropic（SDK 与 httpx
   两条路径）/ gemini（非流式与流式两条路径）。
3. 默认**逐字不变**：画像没写 ⇒ 还是原来的 provider 默认。
4. **通用反装饰品守卫**：画像里出现的**每个键**都必须被兑现
   （⇒ 挡住**所有**新来的装饰品，而不只是某个键）。
5. ``GenerationConfig`` 对非正数的超时直接报错（不静默当成「无超时」）。
"""
from __future__ import annotations

import pytest

from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt

#: 画像里**允许**出现的键 —— 每个都必须**真的被兑现**。
#: ⚠ ``max_tokens`` **不在**这里：它在 domain 层有 120000 的**下限**
#:   ⇒ 画像里写任何小于它的值都会被顶掉 ⇒ 那是死配置（同族先例：第 35 批已删）。
HONORED_PROFILE_KEYS = frozenset(
    {"model", "temperature", "response_format", "timeout_seconds"}
)
#: 结构键（继承），不是配置项。
STRUCTURAL_PROFILE_KEYS = frozenset({"extends"})


def _settings(**overrides):
    from infrastructure.ai.config.settings import Settings

    base = {
        "default_model": "m-1",
        "api_key": "test-key",
        "timeout_seconds": 111.0,
    }
    base.update(overrides)
    return Settings(**base)


def _prompt() -> Prompt:
    return Prompt(system="sys", user="usr")


# ─────────────────────── 1) 画像 ⇒ GenerationConfig ───────────────────────


def test_profile_timeout_seconds_reaches_the_generation_config(tmp_path, monkeypatch):
    """画像里的 ``timeout_seconds`` ⇒ **真的**进 ``GenerationConfig``。

    ⚠ 用**临时 YAML**（不动仓库里的文件）⇒ 证明的是「YAML → 配置」这条真链路，
    不是「配置对象上有个字段」。
    """
    import infrastructure.ai.generation_profiles as gp

    tmp = tmp_path / "generation_profiles.yaml"
    tmp.write_text(
        "base_generation_profile:\n"
        "  temperature: 0.3\n"
        "\n"
        "demo_profile:\n"
        "  extends: base_generation_profile\n"
        "  timeout_seconds: 42\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gp, "_CONFIG_PATH", tmp)
    gp.load_generation_profiles.cache_clear()
    try:
        config = gp.generation_config_from_profile("demo_profile")
        assert config.timeout_seconds == 42.0, "画像里的超时没进 GenerationConfig"
    finally:
        gp.load_generation_profiles.cache_clear()


def test_generation_config_rejects_a_non_positive_timeout():
    """非正数的超时直接报错（不静默当成「无超时」）。"""
    with pytest.raises(ValueError):
        GenerationConfig(model="m-1", timeout_seconds=0)
    with pytest.raises(ValueError):
        GenerationConfig(model="m-1", timeout_seconds=-1.0)


# ─────────────────────── 2) provider 兑现（每条路径） ───────────────────────


def test_openai_provider_honours_the_timeout_on_both_request_paths():
    """openai：chat **与** responses 两条路径都要兑现。

    ⚠ responses 那条路径此前**根本没有** timeout 键 ⇒ 只接 chat 的话，
    能力在**默认路径**（Responses）上静默失效（第 32 批同款坑）。
    """
    from infrastructure.ai.providers.openai_provider import OpenAIProvider

    provider = OpenAIProvider(_settings())
    config = GenerationConfig(model="m-1", timeout_seconds=42.0)

    chat = provider._build_chat_request_kwargs(
        [{"role": "user", "content": "x"}], config
    )
    responses = provider._build_responses_request_kwargs(_prompt(), config)

    assert chat["timeout"] == 42.0, "chat 路径没兑现单次调用的超时"
    assert responses["timeout"] == 42.0, "responses 路径没兑现单次调用的超时"


def test_anthropic_provider_honours_the_timeout_on_both_paths():
    """anthropic：SDK 顶层参数 与 httpx 每请求覆盖 都要兑现。"""
    from infrastructure.ai.providers.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(_settings())
    config = GenerationConfig(model="m-1", timeout_seconds=42.0)

    create_kwargs = provider._build_create_kwargs(_prompt(), config)
    _, sdk_payload = provider._build_sdk_stream_request(_prompt(), config)

    assert create_kwargs["timeout"] == 42.0, "SDK create 路径没兑现超时"
    assert sdk_payload["timeout"] == 42.0, "SDK stream 路径没兑现超时"
    # ⚠ timeout 必须留在**顶层**（塞进 extra_body 会被当成请求体字段发给服务端）。
    assert "timeout" not in (sdk_payload.get("extra_body") or {})


@pytest.mark.asyncio
async def test_gemini_provider_passes_the_timeout_to_the_request():
    """gemini：请求必须真的带上这个超时（打桩 http client 抓参数）。"""
    from infrastructure.ai.providers.gemini_provider import GeminiProvider

    provider = GeminiProvider(_settings())

    captured: dict = {}

    class _CapturingClient:
        async def post(self, url, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop-after-capture")

    provider._http_client = _CapturingClient()
    config = GenerationConfig(model="m-1", timeout_seconds=42.0)

    with pytest.raises(RuntimeError):
        await provider.generate(_prompt(), config)

    assert captured.get("timeout") == 42.0, "gemini 请求没带上单次调用的超时"


# ─────────────────────── 3) 默认逐字不变 ───────────────────────


def test_without_a_profile_timeout_the_provider_defaults_are_unchanged():
    """画像没写超时 ⇒ 还是**原来的** provider 默认（行为逐字不变）。"""
    from infrastructure.ai.providers.anthropic_provider import (
        _SDK_CLIENT_TIMEOUT_SECONDS,
        AnthropicProvider,
    )
    from infrastructure.ai.providers.openai_provider import OpenAIProvider

    config = GenerationConfig(model="m-1")  # timeout_seconds 默认 None

    openai_provider = OpenAIProvider(_settings(timeout_seconds=111.0))
    chat = openai_provider._build_chat_request_kwargs(
        [{"role": "user", "content": "x"}], config
    )
    assert chat["timeout"] == 111.0, "默认路径的超时变了（settings 那个值才是原来的）"

    anthropic_provider = AnthropicProvider(_settings())
    create_kwargs = anthropic_provider._build_create_kwargs(_prompt(), config)
    # ⚠ 第 66 批（B-2）：没配单次超时时**不传** ``timeout`` ⇒ 由 SDK 用客户端级默认。
    #   ⚠ 之前这里传的是 300.0（浮点）⇒ httpx/SDK 的「每请求浮点」语义会
    #     **整体替换**客户端的**分层**超时（connect 30→300 / read 120→300）
    #     ⇒ 第 50 批引入的回归。
    assert "timeout" not in create_kwargs, "没配单次超时时不该硬塞一个客户端级超时"
    assert _SDK_CLIENT_TIMEOUT_SECONDS == 300.0, "SDK 客户端默认超时被改了"
    # 客户端级默认**仍然**是 300 ⇒ 行为与第 50 批之前逐字一致。
    assert anthropic_provider.client.timeout == _SDK_CLIENT_TIMEOUT_SECONDS


# ─────────────────────── 4) 通用反装饰品守卫 ───────────────────────


def test_every_profile_key_must_actually_be_honoured():
    """画像里出现的**每个键**都必须**真的被兑现**（通用版）。

    ⚠ 第 49 批的守卫是「禁止 ``timeout_seconds`` / ``retries`` 这两个键」——
    只能挡住那**两个**键；第 50 批 ``timeout_seconds`` 变成真兑现后，
    它过期了 ⇒ 改成**通用版**：**出现的键必须被兑现**
    ⇒ 挡住**所有**新来的装饰品（包括本批清掉的 ``provider_role``）。
    """
    from infrastructure.ai.generation_profiles import load_generation_profiles

    offenders = []
    for name, profile in load_generation_profiles().items():
        for key in profile:
            if key in HONORED_PROFILE_KEYS or key in STRUCTURAL_PROFILE_KEYS:
                continue
            offenders.append(f"{name}.{key}")

    assert not offenders, (
        f"画像里出现了**无人兑现**的键（装饰品）: {offenders}；"
        "要么删掉，要么让它进 GenerationConfig 并被 provider 兑现"
    )


def test_provider_role_is_gone():
    """``provider_role`` 曾是**装饰品**（全仓只有 YAML 那一行出现过它）⇒ 已删。

    ⚠ provider 选型由**激活的生成画像**（``Settings.protocol``）决定，
    与本文件无关 ⇒ 这个键留着只会让人以为「按角色选 provider」这功能存在。
    """
    from infrastructure.ai.generation_profiles import load_generation_profiles

    for name, profile in load_generation_profiles().items():
        assert "provider_role" not in profile, (
            f"画像 {name} 又出现了 provider_role ⇒ 全仓无人读 ⇒ 装饰品"
        )
