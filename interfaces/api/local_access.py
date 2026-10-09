"""本机访问信任判定：Origin / Host 白名单的唯一事实来源。

背景：PlotPilot 是本地优先应用（FastAPI 监听 127.0.0.1，Tauri 桌面壳 + Vite 开发服务器）。
后端**没有**用户登录态，唯一的访问边界就是"只有本机进程/本机前端能调用 API"。
如果 CORS 放开为 `*`，任何被用户浏览器打开的恶意网页都能直接读写
`http://127.0.0.1:<port>` 上的接口（读小说正文、读 LLM 密钥、改数据、触发 pip 安装、
关掉后端）——而且它们发起的请求 `request.client.host` 恰恰就是 `127.0.0.1`，
所以仅检查 client host 的"本机守卫"对网页攻击者完全无效。

本模块提供两件事：
1. `DEFAULT_CORS_ORIGINS` —— CORSMiddleware 默认放行的 origin 白名单（不再是 `*`）。
2. `assert_local_request()` —— 供高危端点使用的加强版守卫：既检查 client host，
   也在请求带 `Origin` 头时校验它属于可信本机来源。

放行规则（Origin 存在时）：
- 无 `Origin` 头：放行。curl / 原生客户端 / Tauri IPC / pytest TestClient 都属于这一类。
- `tauri://localhost`：Tauri 桌面壳在 macOS / iOS / Linux (WebKitGTK) 的 webview origin。
- `http://tauri.localhost`：Tauri 桌面壳在 Windows / Android (WebView2) 的 webview origin。
- 显式列出的本机开发源：`http://localhost:3000`、`http://127.0.0.1:3000` 等。
  （`tauri dev` 时 webview 加载的就是 devUrl `http://localhost:3000`，因此必须放行。）

注意 Tauri v2 的 webview origin 在不同平台不同，这是官方明确说明的行为，
并且是同类项目在 Windows 上"静默 Failed to fetch"的头号原因——两个都要写。
参考：tauri `crates/tauri/src/plugin.rs` 关于 register_uri_scheme_protocol 的 Origin 说明。
"""
from __future__ import annotations

from fastapi import HTTPException, Request

# ── CORSMiddleware 默认白名单 ────────────────────────────────────────
# 浏览器开发模式下前端走 Vite 代理（同源，不需要 CORS）；
# 跨域只发生在 Tauri 桌面模式（webview origin → 127.0.0.1:<动态端口>）与 `tauri dev`。
DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    # Tauri 打包版 webview origin（两个平台写法不同，缺一个就有一端直接不可用）
    "http://tauri.localhost",   # Windows / Android (WebView2)
    "tauri://localhost",        # macOS / iOS / Linux (WebKitGTK)
    # 开发态：Vite dev server（tauri dev 的 webview 也加载这个 origin）
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    # 后端自带静态站点（frontend/dist 直连，浏览器形态）
    "http://127.0.0.1:8005",
    "http://localhost:8005",
)

# 高危端点守卫使用的可信 origin 集合。
# 比 CORS 白名单更严格：只接受"无 Origin"、Tauri 壳 origin、以及显式列出的本机开发源。
# 任意其它本机端口（如别人的 dev server）默认不可触发这些端点。
LOCAL_TRUSTED_ORIGINS: frozenset[str] = frozenset(DEFAULT_CORS_ORIGINS)

# 允许的 TCP 对端（回环）
_LOOPBACK_CLIENT_HOSTS: frozenset[str] = frozenset(
    {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}
)

# 允许的 Host 头主机名（TrustedHostMiddleware 用，端口会被剥离）
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = (
    "127.0.0.1",
    "localhost",
    "[::1]",
    "testserver",  # pytest TestClient 的默认 Host
)


def is_trusted_local_origin(origin: str | None) -> bool:
    """判断一个 Origin 值是否属于可信本机来源。

    - 空值（无 Origin 头）视为可信：curl / 原生客户端 / IPC / 测试。
    - 命中显式白名单则可信。
    - `tauri://` 与 `*.localhost` 视为 Tauri 壳 origin 变体。
    """
    if not origin:
        return True
    value = origin.strip()
    if not value:
        return True
    if value in LOCAL_TRUSTED_ORIGINS:
        return True
    # Tauri 壳的 origin 变体（协议/主机名可能被 Tauri 版本调整）
    if value.startswith("tauri://"):
        return True
    host = value.split("://", 1)[-1].split("/", 1)[0].lower()
    if host.endswith(".localhost"):
        return True
    return False


def assert_local_request(request: Request) -> None:
    """高危端点守卫：仅允许本机进程 / 可信本机前端调用。

    相比旧的"只看 request.client.host"版本，这里额外校验 `Origin` 头——
    网页发起的跨域请求虽然 client host 是 127.0.0.1，但会带非本机的 Origin。
    """
    if not request.client:
        raise HTTPException(status_code=403, detail="Forbidden")
    client_host = (request.client.host or "").lower()
    if client_host not in _LOOPBACK_CLIENT_HOSTS:
        raise HTTPException(status_code=403, detail="Only localhost access allowed")

    origin = request.headers.get("origin")
    if not is_trusted_local_origin(origin):
        raise HTTPException(
            status_code=403,
            detail="Cross-origin request from a non-local origin is not allowed",
        )
