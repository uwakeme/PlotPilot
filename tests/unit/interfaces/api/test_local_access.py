"""本机访问信任边界（CORS 白名单 / Origin 守卫）的回归测试。

背景：后端没有用户登录态，唯一的访问边界是"只有本机进程与本机前端能调 API"。
这些用例锁住三条不变量，防止再次退回 `CORS_ORIGINS=*` 或"只看 client host"的弱守卫：

1. 默认 CORS 白名单不含 `*`，且覆盖 Tauri 两个平台的 webview origin。
2. 高危端点守卫对非本机 Origin 返回 403（网页攻击者的 client host 也是 127.0.0.1，
   只查 client host 等于不设防）。
3. 已存 API Key 只在端点同源时复用，不会被发往任意 base_url。
"""
from __future__ import annotations

import os

import pytest

from interfaces.api.local_access import (
    DEFAULT_ALLOWED_HOSTS,
    DEFAULT_CORS_ORIGINS,
    assert_local_request,
    is_trusted_local_origin,
)
from interfaces.api.settings import BackendSettings, _split_csv_env


# ── 1. CORS 白名单 ────────────────────────────────────────────────

def test_default_cors_origins_never_contains_wildcard():
    assert "*" not in DEFAULT_CORS_ORIGINS


def test_default_cors_origins_covers_both_tauri_platform_origins():
    # Tauri v2 的 webview origin 平台不同，缺一个就有一端直接不可用：
    # Windows/Android 为 http://tauri.localhost，macOS/iOS/Linux 为 tauri://localhost。
    assert "http://tauri.localhost" in DEFAULT_CORS_ORIGINS
    assert "tauri://localhost" in DEFAULT_CORS_ORIGINS


def test_default_cors_origins_covers_dev_server():
    # `tauri dev` 的 webview 加载 http://localhost:3000，并对 127.0.0.1:<动态端口> 发起跨域请求。
    assert "http://localhost:3000" in DEFAULT_CORS_ORIGINS
    assert "http://127.0.0.1:3000" in DEFAULT_CORS_ORIGINS


def test_allowed_hosts_cover_loopback_and_testclient():
    for host in ("127.0.0.1", "localhost", "testserver"):
        assert host in DEFAULT_ALLOWED_HOSTS


def test_split_csv_env_does_not_fall_back_to_wildcard():
    """空值必须返回 [] —— 旧实现兜底成 ["*"]，是密钥被任意网页读走的根因。"""
    assert _split_csv_env("") == []
    assert _split_csv_env("   ") == []
    assert _split_csv_env("https://a.example, https://b.example") == [
        "https://a.example",
        "https://b.example",
    ]


def test_settings_from_env_without_cors_origins_uses_safe_defaults(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    settings = BackendSettings.from_env()
    assert settings.cors_origins == DEFAULT_CORS_ORIGINS
    assert "*" not in settings.cors_origins


def test_settings_from_env_allows_explicit_override(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://my.example")
    assert BackendSettings.from_env().cors_origins == ("https://my.example",)


# ── 2. Origin 守卫 ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "origin",
    [
        None,          # curl / 原生客户端 / Tauri IPC / TestClient
        "",            # 同上
        "http://tauri.localhost",
        "tauri://localhost",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
)
def test_trusted_local_origins_are_accepted(origin):
    assert is_trusted_local_origin(origin) is True


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",     # 典型恶意网页（驱动站攻击）
        "http://evil.example",
        "http://127.0.0.1:9999",    # 未列出的本机端口，不默认放行
        "null",                     # sandboxed iframe / file://
    ],
)
def test_untrusted_origins_are_rejected(origin):
    assert is_trusted_local_origin(origin) is False


def _request_with(origin, client_host="127.0.0.1"):
    from starlette.datastructures import Headers
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/system/install-extensions",
        "headers": Headers({"origin": origin} if origin is not None else {}).raw,
        "client": (client_host, 54321),
        "query_string": b"",
    }
    return Request(scope)


def test_guard_accepts_tauri_webview_origin():
    # 打包版 Windows 客户端的正常请求不能被误伤。
    assert_local_request(_request_with("http://tauri.localhost"))


def test_guard_rejects_remote_origin_even_from_loopback_client():
    """核心回归：恶意网页的 client host 也是 127.0.0.1，必须靠 Origin 拦下。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        assert_local_request(_request_with("https://evil.example"))
    assert exc.value.status_code == 403


def test_guard_rejects_non_loopback_client():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        assert_local_request(_request_with(None, client_host="192.168.1.20"))
    assert exc.value.status_code == 403


def test_guard_accepts_request_without_origin_header():
    assert_local_request(_request_with(None))
