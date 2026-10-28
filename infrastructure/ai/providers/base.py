"""LLM 提供商基类"""
import logging
from abc import ABC
from domain.ai.services.llm_service import LLMService
from infrastructure.ai.config.settings import Settings

logger = logging.getLogger(__name__)


def merge_extra_body(settings, config) -> dict:
    """画像的 provider 参数 + **单次调用**的覆盖（同名键以**单次**为准）。

    ⚠ 为什么（第 32 批）：provider 级的参数（``reasoning_effort`` / ``thinking`` /
      ``top_p`` …）只来自**激活的生成画像** ⇒ 单次调用改不了
      ⇒ 一次调用要「尽力思考」就是 5 分钟，改不掉。
    ⇒ 这里把**单次调用**的 ``config.extra_body`` 合并进去
    ⇒ 既不改默认行为（``config.extra_body`` 默认 None ⇒ 结果与画像完全一致），
      也让**单点**可调（例如审计这类核对型任务关掉思考）。
    """
    merged = dict(getattr(settings, "extra_body", None) or {})
    merged.update(getattr(config, "extra_body", None) or {})
    return merged


class BaseProvider(LLMService, ABC):
    """LLM 提供商基类

    所有 LLM 提供商的抽象基类，继承自 LLMService 接口。
    提供 aclose() 清理接口用于关闭 HTTP 连接池。
    """

    def __init__(self, settings: Settings):
        """初始化提供商

        Args:
            settings: AI 配置设置
        """
        self.settings = settings

    async def aclose(self) -> None:
        """关闭 Provider 持有的 HTTP 连接池，释放系统资源（TCP 连接、TLS 上下文等）。

        子类可覆写此方法以关闭各自的 httpx client。
        默认实现自动检测并关闭常见的 client 属性。
        """
        closed_any = False
        # 关闭同步 httpx.Client
        for attr in ('_http_client_sync',):
            client = getattr(self, attr, None)
            if client is not None and hasattr(client, 'is_closed') and not client.is_closed:
                try:
                    client.close()
                    closed_any = True
                except Exception:
                    pass
        # 关闭异步 httpx.AsyncClient
        for attr in ('_http_client_async', '_http_client', '_stream_http_client'):
            client = getattr(self, attr, None)
            if client is not None and hasattr(client, 'aclose'):
                try:
                    await client.aclose()
                    closed_any = True
                except Exception:
                    pass
        if closed_any:
            logger.debug("Provider HTTP 连接池已关闭: %s", self.__class__.__name__)
