# -*- coding: utf-8 -*-
"""第 85 批：路由装饰器**不许挂在私有辅助函数上**。

起因（真跑一本书时撞出来的生产 bug）
------------------------------------
``2026-10-27`` 的 ``14619a06``（第 18 批）把辅助函数
``_resolve_start_target_chapters`` **插在了** ``@router.post("/{novel_id}/start")``
**与真正的路由函数** ``start_autopilot`` **之间** ⇒

* FastAPI 把**辅助函数**注册成了路由（它的 ``body_target`` / ``db_target``
  成了**必填查询参数**）⇒ ``POST /api/v1/autopilot/{id}/start`` 恒 **422**；
* 真正的 ``start_autopilot`` **没有路由** ⇒ **前端「启动托管」按钮从那天起就是坏的**。

⚠ 这一族问题的形状：**装饰器与函数之间被插入了一个别的 def**
⇒ 人眼看不出来（文件里明明有 ``@router.post(...)``）
⇒ 只有「路由到底注册给了谁」能看出来。

本文件钉住
----------
1. **通则**：任何 ``@router.<method>`` 都不许挂在 ``_`` 前缀的函数上
   ⇒ 这一族以后在**提交前**就被抓住（不需要真跑一次才发现）；
2. **具体**：``/autopilot/{novel_id}/start`` 这个路由必须由 ``start_autopilot`` 承担
   ⇒ 且**不许**要求查询参数（前端传的是 JSON body）。
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
API_DIR = ROOT / "interfaces" / "api" / "v1"

sys.path.insert(0, str(ROOT))

#: 路由装饰器的前缀（``@router.get`` / ``@router.post`` …）
_ROUTE_DECORATOR_PREFIXES = ("router.", "api_router.", "blueprint.")


def _decorated_functions(path: pathlib.Path):
    """产出 (被装饰的函数名, 装饰器列表) —— 只看函数定义。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = []
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Attribute):
                names.append(target.attr)
            elif isinstance(target, ast.Name):
                names.append(target.id)
        if any(n.startswith("router") or n.startswith("api_router") or n.startswith("blueprint")
               for n in names):
            yield node.name, names


def test_no_route_decorator_sits_on_a_private_helper():
    """⚠ 通则：路由装饰器不许挂在 ``_`` 前缀的函数上。

    ⚠ 这就是 ``14619a06`` 把「启动托管」接口弄坏的形状。
    """
    offenders = []
    for path in sorted(API_DIR.rglob("*.py")):
        for name, _decs in _decorated_functions(path):
            if name.startswith("_"):
                offenders.append("%s :: %s" % (path.relative_to(ROOT).as_posix(), name))
    assert not offenders, (
        "这些路由装饰器挂在**私有辅助函数**上"
        "（⇒ 真正的路由函数没有路由；辅助函数的参数会变成必填查询参数）：\n"
        + "\n".join("  - %s" % o for o in offenders)
    )


def _start_routes(router):
    """``/{novel_id}/start`` 的 POST 路由（router 自带前缀 ⇒ 按**后缀**匹配）。"""
    return [
        route
        for route in router.routes
        if getattr(route, "path", "").endswith("/{novel_id}/start")
        and "POST" in (getattr(route, "methods", set()) or set())
    ]


def test_the_autopilot_start_route_is_owned_by_start_autopilot():
    """⚠ 具体：``/{novel_id}/start`` 必须由 ``start_autopilot`` 承担。"""
    from interfaces.api.v1.engine.autopilot_routes import router

    hits = _start_routes(router)
    assert hits, "没有 POST /{novel_id}/start 路由 ⇒ 前端「启动托管」按钮是坏的"

    for route in hits:
        endpoint = getattr(route, "endpoint", None)
        assert endpoint is not None and endpoint.__name__ == "start_autopilot", (
            "POST /{novel_id}/start 的处理函数是 %r（不是 start_autopilot）"
            "⇒ 辅助函数被当成了路由" % getattr(endpoint, "__name__", endpoint)
        )


def test_the_autopilot_start_route_takes_a_body_not_query_params():
    """⚠ 前端传的是 **JSON body**（``AutopilotStartRequest``）⇒ 路由不许要求查询参数。"""
    import inspect

    from interfaces.api.v1.engine.autopilot_routes import router

    for route in _start_routes(router):
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        params = list(inspect.signature(endpoint).parameters)
        query_like = [
            p for p in params
            if p not in ("novel_id", "body", "background_tasks", "request", "response")
        ]
        assert not query_like, (
            "start 路由要求查询参数 %s ⇒ 前端（只发 JSON body）会拿到 422" % query_like
        )
