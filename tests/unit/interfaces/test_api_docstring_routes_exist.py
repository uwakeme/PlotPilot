# -*- coding: utf-8 -*-
"""第 97 批：API 文档里提到的**接口路径必须真的存在**。

⚠ 病灶（冷启动验收时撞到）
--------------------------
``interfaces/api/v1/core/novels.py`` 的 ``create_novel`` docstring 写着：

    4. 用户手动触发规划（通过 POST /novels/{novel_id}/structure/plan 接口）

⇒ **这个路由不存在** ⇒ 照文档做的人会拿到 404
⇒ 真实的路径是 ``POST /novels/{novel_id}/macro/generate``。

守卫口径
--------
1. 扫 ``interfaces/api/v1/**`` 的 **docstring**（不是注释）；
2. 只认「**HTTP 方法词 + 路径**」这种形态（例：``POST /novels/{id}/x``）
   ⇒ 字段路径、上游服务路径、文件路径一律不算；
3. 路由表 = **router 的 prefix + 路由自身的 path**（少了前缀就会误报一片）；
4. 提到的路径在路由表里找不到 ⇒ 红。

⚠⚠ **守卫必须自证它扫到了东西**（本守卫第一版把仓库根算少了一级
   ⇒ 扫了不存在的目录 ⇒ 0 路由 0 提及 ⇒ **永远绿**）。
"""
from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
API_DIR = ROOT / "interfaces" / "api" / "v1"

#: 「方法词 + 路径」——只有这种形态才算「文档里写的接口」
_METHOD_THEN_PATH = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_\-{}/.]*)"
)
#: 路径里的占位符（名字不重要 ⇒ 归一化成 {}）
_PLACEHOLDER = re.compile(r"\{[^}]+\}")
#: 像日期的片段（2026-10-14）⇒ 是文件引用，不是路由
_DATE_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _norm(path: str) -> str:
    """归一化：去尾斜杠 · 占位符统一成 {} · **去掉 /api/v1 之类的注册前缀**。

    ⚠ 路由表里存的是「router 前缀 + 路由路径」（注册时的 ``API_V1_PREFIX``
    不在里面）⇒ 而 docstring 里常写**完整公开路径**（``/api/v1/...``）
    ⇒ 比对前要把注册前缀剥掉，否则一片误报。
    """
    p = _PLACEHOLDER.sub("{}", path.strip().rstrip("/"))
    for prefix in ("/api/v1", "/api"):
        if p.startswith(prefix + "/"):
            p = p[len(prefix):]
            break
    return p


def _routes() -> set[str]:
    """所有路由的**完整路径**集合（router 的 prefix + 路由自身的 path）。"""
    routes: set[str] = set()
    for path in sorted(API_DIR.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except SyntaxError:
            continue
        # 先取本模块 router 的 prefix
        prefix = ""
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(getattr(t, "id", "") == "router" for t in node.targets):
                continue
            if not isinstance(node.value, ast.Call):
                continue
            for kw in node.value.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    prefix = str(kw.value.value)
        # 再取路由
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in {
                "get", "post", "put", "patch", "delete",
            }:
                continue
            if not isinstance(func.value, ast.Name) or func.value.id != "router":
                continue
            if not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            routes.add(_norm(prefix + first.value))
    return routes


def _doc_mentions() -> list[tuple[str, int, str]]:
    """（文件, 行号, 路径）——docstring 里「方法词 + 路径」的提及。"""
    out: list[tuple[str, int, str]] = []
    for path in sorted(API_DIR.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            doc = ast.get_docstring(node)
            if not doc:
                continue
            for method, raw in _METHOD_THEN_PATH.findall(doc):
                raw = raw.rstrip(".,;:）)】」")
                if not raw or len(raw.strip("/").split("/")) < 2:
                    continue
                if _DATE_LIKE.search(raw):
                    continue
                out.append(
                    (path.relative_to(ROOT).as_posix(), getattr(node, "lineno", 0), raw)
                )
    return out


def test_the_guard_actually_scans_something():
    """⚠⚠ **守卫必须自证它扫到了东西**。

    ⚠ 本守卫第一版把仓库根算成了 ``tests/``（少了一级 ``parents``）
    ⇒ 它扫了一个**不存在的目录** ⇒ 0 路由、0 提及 ⇒ **永远绿**
    ⇒ 这正是「空守卫」的样子：看上去在守，其实在睡觉。
    """
    assert API_DIR.is_dir(), "API 目录不存在：%s ⇒ 守卫扫了个空" % API_DIR
    routes = _routes()
    mentions = _doc_mentions()
    assert len(routes) > 50, "只扫到 %s 条路由 ⇒ 扫描范围不对（路径算错了？）" % len(routes)
    assert len(mentions) > 0, "一条「方法词 + 路径」的提及都没扫到 ⇒ 扫描范围不对"


def test_every_api_path_mentioned_in_a_docstring_really_exists():
    """⚠ 文档里写的接口路径必须存在（否则照文档做的人拿到 404）。

    ⚠ 匹配口径：**末两段**精确匹配（占位符已归一化）——
    ⚠ 因为「注册前缀」（``/api/v1`` / ``/novels`` / ``/planning``）是在
    ⚠ ``routes.py`` 注册时才拼上去的，路由装饰器里看不到
    ⇒ 只比最后两段才不会误报一片，又仍能抓住「整条路径不存在」。
    """
    routes = _routes()
    route_tails = set()
    for route in routes:
        parts = [p for p in route.strip("/").split("/") if p]
        if len(parts) >= 2:
            route_tails.add("/".join(parts[-2:]))

    offenders: list[str] = []
    for rel, lineno, mentioned in _doc_mentions():
        parts = [p for p in _norm(mentioned).strip("/").split("/") if p]
        if len(parts) < 2:
            continue
        if "/".join(parts[-2:]) not in route_tails:
            offenders.append("%s:%d  %s" % (rel, lineno, mentioned))
    assert not offenders, (
        "这些 docstring 里写的接口路径**在路由表里不存在**（照文档做会 404）：\n  "
        + "\n  ".join(offenders)
    )
