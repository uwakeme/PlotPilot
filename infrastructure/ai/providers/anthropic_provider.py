"""Anthropic LLM 提供商实现"""
import json
import logging
from typing import Any, AsyncIterator

import httpx
import httpx2
from anthropic import Anthropic, AsyncAnthropic

from domain.ai.services.llm_service import GenerationConfig, GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.http_timeout import build_httpx_timeout
from .base import BaseProvider, merge_extra_body
from .model_resolution import require_resolved_model_id

logger = logging.getLogger(__name__)

#: SDK 客户端级的默认超时（秒）—— 第 50 批提为常量，
#: 避免「客户端构造」与「单次调用解析」两处各写一个 300。
_SDK_CLIENT_TIMEOUT_SECONDS = 300.0


def _json_response_instruction(response_format: dict[str, Any]) -> str:
    """Build prompt-side JSON constraints for Anthropic Messages API."""
    fmt_type = response_format.get("type")
    if fmt_type == "json_schema":
        schema_payload = response_format.get("json_schema") or {}
        schema = schema_payload.get("schema") or {}
        schema_name = schema_payload.get("name") or "response"
        try:
            schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            schema_text = str(schema)
        return (
            "\n\n请只输出一个有效 JSON 对象，不要包含 Markdown 或额外文字。"
            f"\nJSON 对象需符合 `{schema_name}` schema：{schema_text}"
        )
    if fmt_type == "json_object":
        return "\n\n请只输出一个有效 JSON 对象，不要包含 Markdown 或额外文字。"
    return ""


def _extract_text_from_content_block(block: Any) -> str:
    """尽量从兼容端点返回的 content block 中提取文本。"""
    if block is None:
        return ""

    if isinstance(block, str):
        return block

    text = getattr(block, "text", None)
    if isinstance(text, str) and text.strip():
        return text

    if isinstance(block, dict):
        for key in ("text", "content", "value"):
            value = block.get(key)
            if isinstance(value, str) and value.strip():
                return value
        if block.get("type") == "json" and block.get("json") is not None:
            try:
                return json.dumps(block["json"], ensure_ascii=False)
            except Exception:
                return str(block["json"])

    block_type = getattr(block, "type", None)
    if block_type in {"json", "input_json", "output_json"}:
        json_payload = getattr(block, "json", None)
        if json_payload is not None:
            try:
                return json.dumps(json_payload, ensure_ascii=False)
            except Exception:
                return str(json_payload)

    return ""


class AnthropicProvider(BaseProvider):
    """Anthropic LLM 提供商实现

    使用 Anthropic API 实现 LLM 服务。

    双端点策略：
    - generate() (规划/分析): 使用官方 SDK，走官方 API (HTTPS)
    - stream_generate() (正文生成): 使用自定义 httpx，走代理服务器
    """

    def __init__(self, settings: Settings):
        """初始化 Anthropic 提供商

        Args:
            settings: AI 配置设置

        Raises:
            ValueError: 如果 API key 未设置
        """
        super().__init__(settings)

        if not settings.api_key:
            raise ValueError("API key is required for AnthropicProvider")

        # 归一化 base_url：去掉尾部 /v1（SDK 内部会自动拼 /v1/messages）
        base = settings.base_url.rstrip("/") if settings.base_url else None
        if base and base.endswith("/v1"):
            base = base[:-3]

        official_client_kw = {
            "api_key": settings.api_key,
            "timeout": _SDK_CLIENT_TIMEOUT_SECONDS,  # 5 分钟超时
            "max_retries": 2,
            "default_headers": {
                # 产品标识：伪装官方 CLI 的 UA 会被端点按 CLI 客户端策略对待（限流/封禁口径不同）
                "User-Agent": "plotpilot/1.0 (+https://github.com/shenminglinyi/PlotPilot)",
                **(settings.extra_headers or {}),
            },
            "default_query": settings.extra_query or None,
        }
        if base:
            official_client_kw["base_url"] = base

        # SDK 内置 httpx 默认 trust_env=True，会走系统 HTTP(S)_PROXY，本机代理 TLS 常导致 ConnectError。
        # anthropic 1.x 内部基于 httpx2 包，http_client 必须传 httpx2 实例，否则 SDK 类型检查直接报错。
        _sdk_timeout = build_httpx_timeout(settings.http_timeout_settings)
        self._http_client_sync = httpx2.Client(timeout=_sdk_timeout, trust_env=False)
        self._http_client_async = httpx2.AsyncClient(timeout=_sdk_timeout, trust_env=False)
        self.client = Anthropic(**official_client_kw, http_client=self._http_client_sync)
        self.async_client = AsyncAnthropic(**official_client_kw, http_client=self._http_client_async)

        # 流式端点专用 httpx client（长生命周期，跨请求复用连接池）
        self._stream_http_client = httpx.AsyncClient(
            timeout=build_httpx_timeout(self.settings.http_timeout_settings),
            trust_env=False,
        )

        # 兼容旧字段：若其他模块引用，保留归一化后的值
        self.proxy_base_url = base
    async def generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> GenerationResult:
        """生成文本

        Args:
            prompt: 提示词
            config: 生成配置

        Returns:
            生成结果

        Raises:
            RuntimeError: 当 API 调用失败或返回空内容时
        """
        try:
            create_kwargs = self._build_create_kwargs(prompt, config)

            # 使用 async_client 避免阻塞 asyncio 事件循环。
            # 走流式聚合并取 final message（响应结构与 create 完全一致）：
            # 非流式 create 的 300s 超时是「等完整响应」的总上限，大 prompt 规划类生成
            # 会被网关静默拖到超时（×max_retries=2 后表现为 ~15 分钟无日志挂起）；
            # 流式下 token 持续回流即不触发读超时，慢而活的生成能完整返回，
            # 真停滞仍会按超时快速失败。
            async with self.async_client.messages.stream(**create_kwargs) as stream:
                response = await stream.get_final_message()

            # 防御性检查：验证 content 列表非空
            if not response.content:
                raise RuntimeError("API returned empty content")

            parts = []
            for block in response.content:
                text = _extract_text_from_content_block(block)
                if text:
                    parts.append(text)

            content = "\n".join(part.strip() for part in parts if part and part.strip()).strip()
            if not content:
                raise RuntimeError("API returned no text content")

            # 创建 token 使用统计
            token_usage = TokenUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens
            )

            return GenerationResult(content=content, token_usage=token_usage)

        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to generate text: {str(e)}") from e

    def _resolve_timeout(self, config: GenerationConfig) -> float | None:
        """本次调用的超时上限（秒）；``None`` ⇒ **不覆盖**任何客户端默认。

        ⚠ 第 66 批（B-2）：原来这里在「没配单次超时」时返回 **300.0 浮点**，
          而 httpx 的「每请求浮点」语义是**整体替换**客户端的**分层**超时
          （connect 30→300 / read 120→300）⇒ 端点不可达时不再快速失败，
          流式 chunk 停滞要 5 分钟才判死 —— 这正是第 50 批**引入**的回归
          （第 50 批之前这条路径**根本不传** ``timeout``）。
          ⇒ 现在返回 ``None`` ⇒ 各条路径改用「保持客户端默认」的写法：
            SDK 路径**不传** ``timeout``；httpx 路径传 ``USE_CLIENT_DEFAULT``
          ⇒ 与第 50 批**之前**的行为逐字一致。
        """
        value = getattr(config, "timeout_seconds", None)
        return float(value) if value is not None else None

    def _build_create_kwargs(
        self,
        prompt: Prompt,
        config: GenerationConfig,
    ) -> dict[str, Any]:
        """构建 Messages API 的调用参数（``generate()`` 路径）。

        ⚠ 第 32 批：带上**单次调用**的 provider 参数覆盖（默认 ``None``
        ⇒ 与改造前**完全一致**）。
        ⚠ 第 34 批（账 A）：**也**带上**画像**的 ``extra_body``
        （与流式路径**同源** ⇒ 用户配的 provider 参数在两条路径上行为一致）。
        """
        model_id = require_resolved_model_id(
            config.model,
            self.settings.default_model,
            provider_label="Anthropic / Claude",
        )
        # 构建请求参数
        # anthropic 1.x 的 create() 签名不再含 temperature 等采样参数，需经 extra_body 透传到请求体
        # ⚠ 第 34 批（账 A）：这里**必须**带**画像**的 extra_body ——
        #   以前只有流式路径带 ⇒ 用户配的 provider 参数（reasoning_effort / thinking …）
        #   **在非流式调用里根本没生效**（A1 审计走的就是非流式）。
        # ⇒ 与其它路径**同源**：都用 `merge_extra_body`（画像 + 单次覆盖，同名键以单次为准）。
        create_kwargs = {
            "model": model_id,
            "max_tokens": config.max_tokens,
            "system": prompt.system,
            "messages": [{"role": "user", "content": prompt.user}],
            "extra_body": {
                "temperature": config.temperature,
                **merge_extra_body(self.settings, config),
            },
        }
        # ⚠ 第 66 批（B-2）：单次调用的超时上限（SDK 顶层参数，**不是**请求体字段）。
        #   ⚠ **只在真的配了单次超时时才传**：
        #     不传 ⇒ SDK 用客户端级的 300（= 第 50 批之前的行为，逐字一致）；
        #     传 300.0 ⇒ 数值一样，但语义从「客户端默认」变成「每请求覆盖」
        #     ⇒ 以后把客户端默认改成分层超时时，这里会把它压平（回归的种子）。
        resolved_timeout = self._resolve_timeout(config)
        if resolved_timeout is not None:
            create_kwargs["timeout"] = resolved_timeout
        # Anthropic Messages API does not accept OpenAI-style response_format.
        # Keep structured output provider-agnostic by moving the constraint into
        # the prompt; structured_json_pipeline will parse and validate it.
        if config.response_format:
            fmt = config.response_format
            instruction = _json_response_instruction(fmt)
            if instruction:
                create_kwargs["system"] = create_kwargs["system"] + instruction
        return create_kwargs

    def _build_message_request(
        self,
        prompt: Prompt,
        config: GenerationConfig,
        *,
        stream: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """构建 Messages API 请求体，供 generate / stream 共用。"""
        model_id = require_resolved_model_id(
            config.model,
            self.settings.default_model,
            provider_label="Anthropic / Claude",
        )
        payload: dict[str, Any] = {
            "model": model_id,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
            "system": prompt.system,
            "messages": [{"role": "user", "content": prompt.user}],
        }
        if stream:
            payload["stream"] = True
        # ⚠ 第 32 批：画像的 extra_body + **单次调用**的覆盖（同名键以单次为准）
        payload.update(merge_extra_body(self.settings, config))
        return model_id, payload

    @staticmethod
    def _format_stream_error(exc: BaseException) -> str:
        message = str(exc).strip()
        if message:
            return f"{type(exc).__name__}: {message}"
        return f"{type(exc).__name__}: {exc!r}"

    async def _stream_via_httpx(
        self,
        prompt: Prompt,
        config: GenerationConfig,
    ) -> AsyncIterator[str]:
        """通过 httpx 直接解析 SSE，兼容部分网关的非标准流式响应。"""
        base_url = self.settings.base_url or "https://api.anthropic.com"
        url = f"{base_url}/v1/messages"
        logger.debug("[Stream] Using httpx endpoint: %s", url)

        headers = {
            "x-api-key": self.settings.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "plotpilot/1.0 (+https://github.com/shenminglinyi/PlotPilot)",
            **(self.settings.extra_headers or {}),
        }
        _, payload = self._build_message_request(prompt, config, stream=True)
        # ⚠ 第 66 批（B-2）：见下面 ``timeout=`` 处的说明。
        resolved_timeout = self._resolve_timeout(config)

        logger.debug("[Stream] Calling %s", url)
        async with self._stream_http_client.stream(
            "POST",
            url,
            headers=headers,
            params=self.settings.extra_query or None,
            json=payload,
            # ⚠ 第 66 批（B-2）：单次调用的超时上限（httpx 每请求覆盖）。
            #   ⚠ **没配单次超时时必须传 `USE_CLIENT_DEFAULT`**：
            #     传浮点 ⇒ 整体替换客户端的**分层**超时
            #              （connect 30→300 / read 120→300 ⇒ 慢失败）；
            #     传 `None` ⇒ **彻底没有超时**（更糟）。
            timeout=(
                resolved_timeout
                if resolved_timeout is not None
                else httpx.USE_CLIENT_DEFAULT
            ),
        ) as response:
            if response.status_code != 200:
                error_body = await response.aread()
                raise RuntimeError(
                    f"API error {response.status_code}: {error_body.decode(errors='replace')}"
                )

            buffer = ""
            events_received = 0
            async for chunk in response.aiter_text():
                buffer += chunk.replace("\r\n", "\n")
                while "\n\n" in buffer:
                    event_text, buffer = buffer.split("\n\n", 1)
                    events_received += 1
                    text_content = self._parse_sse_event(event_text)
                    if events_received <= 3:
                        logger.info(
                            "[Stream] SSE event #%d raw=%s parsed=%s",
                            events_received,
                            event_text[:500],
                            text_content[:200] if text_content else "(empty)",
                        )
                    if text_content:
                        yield text_content

    def _build_sdk_stream_request(
        self,
        prompt: Prompt,
        config: GenerationConfig,
    ) -> tuple[str, dict[str, Any]]:
        """SDK 流式调用的**顶层参数**（含 timeout）。

        ⚠ 第 50 批：``timeout`` 必须是 **SDK 顶层参数** ——
        放进 ``extra_body`` 会被当成请求体字段发给服务端（等于没设）。
        """
        model_id, payload = self._build_message_request(prompt, config, stream=False)
        sdk_payload = self._build_sdk_stream_payload(payload)
        # ⚠ 第 66 批（B-2）：同 `_build_create_kwargs` —— 只在真的配了单次超时时才传。
        resolved_timeout = self._resolve_timeout(config)
        if resolved_timeout is not None:
            sdk_payload["timeout"] = resolved_timeout
        return model_id, sdk_payload

    async def _stream_via_sdk(
        self,
        prompt: Prompt,
        config: GenerationConfig,
    ) -> AsyncIterator[str]:
        """通过官方 SDK 流式读取，网关断开 raw SSE 时作为回退。"""
        model_id, sdk_payload = self._build_sdk_stream_request(prompt, config)
        logger.info("[Stream] Falling back to SDK stream for model=%s", model_id)
        async with self.async_client.messages.stream(**sdk_payload) as stream:
            async for text in stream.text_stream:
                if text:
                    yield text

    @staticmethod
    def _build_sdk_stream_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """把请求体拆成「SDK 顶层参数 + extra_body」（第 34 批 · 账 B）。

        ⚠ 为什么拆（第 32 批实测）：``client.messages.stream()`` 的签名是
          **显式关键字**（**没有** ``temperature``、**没有** ``top_p``、
          也没有 ``reasoning_effort`` 之类）⇒ 把它们当**顶层 kwarg** 传进去
          ⇒ **TypeError**（unexpected keyword argument）⇒ 这条兜底路径**根本跑不了**。
          ⚠ 生产日志里它**从未触发**（"Falling back to SDK stream" 全是测试模型名）
          ⇒ 属**潜伏**问题。

        ⚠ 拆法：只有 SDK **认的**那几项留顶层（``model`` / ``max_tokens`` /
          ``messages`` / ``system``），**其余全部**（含 ``temperature`` 与
          画像的 provider 参数）塞进 ``extra_body`` ⇒ SDK 会把它原样并进请求 JSON。
        ⇒ ⇒ **最终请求体与 httpx 路径逐字相同**（httpx 路径就是把整个 payload
          当 JSON 发出去的）⇒ 行为不变，只是**不再崩**。
        """
        reserved = ("model", "max_tokens", "messages", "system")
        sdk_payload: dict[str, Any] = {
            key: value for key, value in payload.items() if key in reserved
        }
        extra = {key: value for key, value in payload.items() if key not in reserved}
        if extra:
            sdk_payload["extra_body"] = extra
        return sdk_payload

    async def stream_generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> AsyncIterator[str]:
        """流式生成内容。

        优先 httpx 解析 SSE（兼容部分代理）；若连接被网关提前断开或零输出，
        自动回退到 Anthropic SDK 的 stream API。
        """
        httpx_error: Exception | None = None
        yielded_any = False

        try:
            async for chunk in self._stream_via_httpx(prompt, config):
                yielded_any = True
                yield chunk
        except Exception as e:
            httpx_error = e
            logger.warning(
                "[Stream] httpx SSE failed (%s), will try SDK fallback",
                self._format_stream_error(e),
            )

        if yielded_any:
            return

        sdk_yielded = False
        try:
            async for chunk in self._stream_via_sdk(prompt, config):
                sdk_yielded = True
                yield chunk
        except Exception as sdk_error:
            sdk_detail = self._format_stream_error(sdk_error)
            if httpx_error is not None:
                httpx_detail = self._format_stream_error(httpx_error)
                logger.error(
                    "[Stream] Failed: httpx=%s; sdk=%s",
                    httpx_detail,
                    sdk_detail,
                )
                raise RuntimeError(
                    f"Failed to stream text: httpx={httpx_detail}; sdk={sdk_detail}"
                ) from sdk_error
            logger.error("[Stream] Failed: %s", sdk_detail)
            raise RuntimeError(f"Failed to stream text: {sdk_detail}") from sdk_error

        if not sdk_yielded:
            model_id = config.model or self.settings.default_model
            detail = (
                f"httpx={self._format_stream_error(httpx_error)}"
                if httpx_error
                else "httpx returned no events"
            )
            raise RuntimeError(
                f"Both streaming paths produced zero output for model={model_id}: {detail}"
            )

    def _parse_sse_event(self, event_text: str) -> str:
        """解析单个 SSE 事件，返回文本内容（如果有）。

        兼容多种 SSE 格式：
        - Anthropic 原生: {"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}
        - OpenAI 兼容:  {"choices":[{"delta":{"content":"..."}}]}
        - 通用 delta:    {"delta":{"text":"..."}} 或 {"text":"..."}
        """
        lines = event_text.strip().split("\n")
        data = None

        for line in lines:
            if line.startswith("data:"):
                data = line[5:].strip()

        if not data:
            return ""

        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            return ""

        # Anthropic 原生
        if parsed.get("type") == "content_block_delta":
            delta = parsed.get("delta", {})
            if delta.get("type") == "text_delta":
                return delta.get("text", "")

        # Anthropic content_block_start (某些模型把 text 放在这里)
        if parsed.get("type") == "content_block_start":
            block = parsed.get("content_block", {})
            if block.get("type") == "text" and block.get("text"):
                return block["text"]

        # OpenAI / DeepSeek 兼容格式
        choices = parsed.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            content = delta.get("content", "")
            if content:
                return content
            # 某些变体
            text = delta.get("text", "")
            if text:
                return text

        # 通用 fallback: 一层 delta.text 或 delta.content
        delta = parsed.get("delta", {})
        if isinstance(delta, dict):
            text = delta.get("text") or delta.get("content")
            if text:
                return text

        # 最通用: 顶层 text/content 字段
        text = parsed.get("text") or parsed.get("content")
        if isinstance(text, str) and text.strip():
            return text

        return ""
