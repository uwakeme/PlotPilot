from abc import ABC, abstractmethod
from typing import AsyncIterator, Dict, Optional
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage


DEFAULT_MAX_OUTPUT_TOKENS = 120000


class GenerationConfig:
    """生成配置"""
    def __init__(
        self,
        model: str = "",
        max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        temperature: float = 1.0,
        response_format: Optional[Dict] = None,
        extra_body: Optional[Dict] = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.response_format = response_format
        #: **单次调用**对 provider 请求体的额外覆盖（第 32 批）。
        #:
        #: ⚠ 为什么需要：provider 级的参数（``extra_body``：``reasoning_effort`` /
        #:   ``thinking`` / ``top_p`` …）只来自**激活的生成画像**
        #:   ⇒ **单次调用改不了** ⇒ 一次调用要「尽力思考」就是 5 分钟，改不掉。
        #:   ⇒ 这里开一个**单次覆盖**的口子：与画像的 ``extra_body`` **合并**
        #:   （**同名键以本值为准**）⇒ 既不改默认行为，也让**单点**可调。
        #:
        #: ⚠ 默认 ``None`` ⇒ 行为与改造前**完全一致**（画像的 extra_body 原样生效）。
        self.extra_body = dict(extra_body) if extra_body else None
        self.__post_init__()

    def __post_init__(self):
        """验证配置参数"""
        if not (0.0 <= self.temperature <= 2.0):
            raise ValueError("Temperature must be between 0.0 and 2.0")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be greater than 0")
        self.max_tokens = max(int(self.max_tokens), DEFAULT_MAX_OUTPUT_TOKENS)


class GenerationResult:
    """生成结果"""
    def __init__(self, content: str, token_usage: TokenUsage):
        self.content = content
        self.token_usage = token_usage
        self.__post_init__()

    def __post_init__(self):
        """验证结果参数"""
        if not self.content or not self.content.strip():
            raise ValueError("Content cannot be empty")


class LLMService(ABC):
    """LLM 服务接口（领域服务）"""

    @abstractmethod
    async def generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> GenerationResult:
        """生成内容"""
        pass

    @abstractmethod
    async def stream_generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> AsyncIterator[str]:
        """流式生成内容"""
        pass
