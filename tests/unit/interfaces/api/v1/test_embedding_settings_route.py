"""嵌入配置路由：PUT 空密钥保留原值、响应序列化走服务层、连通性测试端点。"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from interfaces.api.v1.core.settings import EmbeddingConfigUpdate, update_embedding_config


def _mock_service(existing_key: str) -> MagicMock:
    svc = MagicMock()
    cfg = SimpleNamespace(
        api_key=existing_key,
        mode="openai",
        base_url="https://api.siliconflow.cn/v1",
        model="Qwen/Qwen3-Embedding-8B",
        use_gpu=True,
        model_path="",
        created_at="2026-09-24T10:27:54",
        updated_at="2026-09-29T09:45:28",
    )
    svc.get_config.return_value = cfg
    svc.to_api_dict.return_value = {"ok": True}
    return svc


def test_blank_api_key_preserves_stored_key():
    svc = _mock_service("sk-existing")
    with patch(
        "application.ai.embedding_config_service.get_embedding_config_service",
        return_value=svc,
    ):
        resp = update_embedding_config(
            EmbeddingConfigUpdate(mode="openai", base_url="https://api.siliconflow.cn/v1", model="m")
        )

    assert resp == {"ok": True}
    kwargs = svc.update_config.call_args.kwargs
    assert kwargs["api_key"] == "sk-existing"


def test_explicit_api_key_overrides():
    svc = _mock_service("sk-old")
    with patch(
        "application.ai.embedding_config_service.get_embedding_config_service",
        return_value=svc,
    ):
        update_embedding_config(
            EmbeddingConfigUpdate(mode="openai", api_key="sk-new", base_url="https://x/v1", model="m")
        )

    assert svc.update_config.call_args.kwargs["api_key"] == "sk-new"


def test_response_uses_service_serialization_not_model_method():
    """回归：update_config 返回的模型无 to_api_dict，曾致 PUT 500。"""
    svc = _mock_service("sk-existing")
    with patch(
        "application.ai.embedding_config_service.get_embedding_config_service",
        return_value=svc,
    ):
        resp = update_embedding_config(EmbeddingConfigUpdate(mode="openai"))

    svc.to_api_dict.assert_called_once()
    assert resp == {"ok": True}


# ── POST /settings/embedding/test ────────────────────────────────


@pytest.mark.asyncio
async def test_embedding_test_success_reports_dimension(monkeypatch):
    from interfaces.api.v1.core.settings import test_embedding_config

    svc = _mock_service("sk-existing")
    monkeypatch.setattr(
        "application.ai.embedding_config_service.get_embedding_config_service",
        lambda: svc,
    )

    class FakeSvc:
        def __init__(self, api_key, base_url, model):
            assert api_key == "sk-existing"  # 空 key 回退到已存密钥
            assert base_url == "https://api.siliconflow.cn/v1"

        async def embed(self, text):
            assert text
            return [0.1] * 1024

    monkeypatch.setattr(
        "infrastructure.ai.openai_embedding_service.OpenAIEmbeddingService", FakeSvc
    )

    resp = await test_embedding_config(
        EmbeddingConfigUpdate(mode="openai", base_url="https://api.siliconflow.cn/v1", model="m")
    )
    assert resp["success"] is True
    assert resp["dimension"] == 1024
    assert "latency_ms" in resp


@pytest.mark.asyncio
async def test_embedding_test_failure_returns_error(monkeypatch):
    from interfaces.api.v1.core.settings import test_embedding_config

    svc = _mock_service("sk-existing")
    monkeypatch.setattr(
        "application.ai.embedding_config_service.get_embedding_config_service",
        lambda: svc,
    )

    class FakeSvc:
        def __init__(self, api_key, base_url, model):
            pass

        async def embed(self, text):
            raise RuntimeError("Failed to generate embedding: Not Found")

    monkeypatch.setattr(
        "infrastructure.ai.openai_embedding_service.OpenAIEmbeddingService", FakeSvc
    )

    resp = await test_embedding_config(
        EmbeddingConfigUpdate(mode="openai", base_url="https://x/v1", model="m")
    )
    assert resp["success"] is False
    assert "Not Found" in resp["error"]


@pytest.mark.asyncio
async def test_embedding_test_missing_model_rejected():
    from interfaces.api.v1.core.settings import test_embedding_config

    svc = _mock_service("sk-existing")
    with patch(
        "application.ai.embedding_config_service.get_embedding_config_service",
        return_value=svc,
    ):
        resp = await test_embedding_config(EmbeddingConfigUpdate(mode="openai"))
    assert resp["success"] is False
    assert "模型 ID" in resp["error"]


@pytest.mark.asyncio
async def test_embedding_test_local_mode_checks_path_only(monkeypatch):
    import tempfile
    from pathlib import Path

    from interfaces.api.v1.core.settings import test_embedding_config

    with tempfile.TemporaryDirectory() as tmp:
        resp = await test_embedding_config(
            EmbeddingConfigUpdate(mode="local", model_path=str(Path(tmp)))
        )
    assert resp["success"] is True
    assert "note" in resp
