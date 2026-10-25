"""门禁：生产代码里不许有「用了但没绑定」的名字（NameError 那一族）。

为什么要有这道门
----------------
仓库里**没有** Python linter（没配 ruff/black/flake8），而这类 bug 的特征是
**静态检查能看出来、运行时才炸**。真实跑一本真书时连炸两个：

1. ``engine/pipeline/prose_composer.py`` 的 ``compose()`` 里用了
   ``prose_temperature()``，而那个名字只在**另一个方法** ``_build_config()``
   里做了局部 import ⇒ 运行时 ``NameError``
   ⇒ **整章正文永远生成不出来**（写作连挂 7 次后被熔断）。
2. ``application/checkpoint/services/unified_checkpoint_service.py`` 的
   ``get_checkpoint_graph()`` 里用了 ``slices``，而那个变量只在**另一个方法**
   ``list_checkpoints()`` 里赋值 ⇒ 运行时 ``NameError``
   ⇒ 被 ``except Exception`` 吞掉 ⇒ **世界线可视化静默返回空图**。

两者都过了既有门禁：E-07 那条守卫只数「读单一来源的**调用点个数**」，
**不查名字在那个作用域有没有绑定** ⇒ 少一个 import 照样绿。

本门禁查的就是这一类：作用域分析（模块 / 函数 / 推导式 / class body）
⇒ 任何 ``Load`` 上下文的名字必须能沿作用域链解析到。

口径（保守：宁可漏报不可误报）
----------------------------
* 只查 **生产代码**（``engine/`` ``application/`` ``interfaces/``），
  不查 ``tests/``（测试里大量故意的动态属性）；
* 作用域规则按 Python 语义：
  - 函数局部（参数、赋值、import、def/class、for/with/except 目标、
    海象 ``:=``、``global``/``nonlocal``）；
  - 推导式**有自己的作用域**（Python 3 语义）；
  - class body 里的名字**不**对嵌套函数可见；
* **解析失败 ⇒ 直接失败**（带 BOM 的文件会解析失败 ⇒ 先剥 BOM）。
  绝不「跳过这份文件」—— 跳过等于这份文件上没有守卫。

已知边界（写清楚，别过度声称）
----------------------------
* 不做类型推断，不查属性是否存在（那是另一族问题）；
* ``import *`` 不展开 ⇒ 用到星号导入名的文件会被漏报（保守方向）；
* ``try: import x except ImportError: x = None`` 这类写法按「已绑定」处理
  （它确实绑定了，运行时是否可用是另一回事）。
"""
from __future__ import annotations

import ast
import builtins
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("engine", "application", "interfaces")

#: 模块级就存在的名字（不是 builtins，但同样不需要 import）。
MODULE_GLOBALS = frozenset(
    {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__builtins__"}
)
BUILTINS = set(dir(builtins)) | MODULE_GLOBALS

BOM = "﻿"


class _Scope:
    __slots__ = ("kind", "parent", "names", "globals_")

    def __init__(self, kind: str, parent: "_Scope | None"):
        self.kind = kind
        self.parent = parent
        self.names: set[str] = set()
        self.globals_: set[str] = set()


class _UndefinedNameChecker(ast.NodeVisitor):
    """作用域分析：找出 Load 上下文里解析不到的名字。"""

    def __init__(self) -> None:
        self.problems: list[tuple[int, str]] = []
        self.scope = _Scope("module", None)

    # ---------- 绑定收集 ----------
    def _bind_target(self, node) -> None:
        if isinstance(node, ast.Name):
            self.scope.names.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for elt in node.elts:
                self._bind_target(elt)
        elif isinstance(node, ast.Starred):
            self._bind_target(node.value)

    def _collect(self, body) -> None:
        for node in body:
            self._collect_node(node)

    def _collect_node(self, node) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self.scope.names.add(node.name)
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    continue
                self.scope.names.add(alias.asname or alias.name.split(".")[0])
            return
        if isinstance(node, ast.Assign):
            for target in node.targets:
                self._bind_target(target)
            return
        if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            self._bind_target(node.target)
            return
        if isinstance(node, ast.NamedExpr):
            self._bind_target(node.target)
            return
        if isinstance(node, (ast.For, ast.AsyncFor)):
            self._bind_target(node.target)
            self._collect(node.body)
            self._collect(node.orelse)
            return
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    self._bind_target(item.optional_vars)
            self._collect(node.body)
            return
        if isinstance(node, ast.Try):
            self._collect(node.body)
            for handler in node.handlers:
                if handler.name:
                    self.scope.names.add(handler.name)
                self._collect(handler.body)
            self._collect(node.orelse)
            self._collect(node.finalbody)
            return
        if isinstance(node, ast.If):
            self._collect(node.body)
            self._collect(node.orelse)
            return
        if isinstance(node, ast.While):
            self._collect(node.body)
            self._collect(node.orelse)
            return
        if isinstance(node, ast.Match):
            for case in node.cases:
                self._collect_match(case.pattern)
            self._collect(node.body)
            return
        if isinstance(node, ast.Global):
            self.scope.globals_.update(node.names)
            self.scope.names.update(node.names)
            return
        if isinstance(node, ast.Nonlocal):
            self.scope.names.update(node.names)
            return
        if isinstance(node, ast.Delete):
            for target in node.targets:
                self._bind_target(target)
            return

    def _collect_match(self, pattern) -> None:
        if isinstance(pattern, ast.MatchAs):
            if pattern.name:
                self.scope.names.add(pattern.name)
            if pattern.pattern is not None:
                self._collect_match(pattern.pattern)
        elif isinstance(pattern, ast.MatchStar):
            if pattern.name:
                self.scope.names.add(pattern.name)
        elif isinstance(pattern, ast.MatchMapping):
            for key in pattern.keys:
                self._collect_match(key)
            if pattern.rest:
                self.scope.names.add(pattern.rest)
        elif isinstance(pattern, ast.MatchSequence):
            for elt in pattern.patterns:
                self._collect_match(elt)
        elif isinstance(pattern, ast.MatchOr):
            for elt in pattern.patterns:
                self._collect_match(elt)

    # ---------- 查名字 ----------
    def _lookup(self, name: str) -> bool:
        scope = self.scope
        while scope is not None:
            if name in scope.globals_ or name in scope.names:
                return True
            if scope.kind == "class":
                # class body 里的名字对嵌套函数**不可见** ⇒ 跳到 class 外层。
                scope = scope.parent
                if scope is not None and scope.kind == "class":
                    scope = scope.parent
                continue
            scope = scope.parent
        return name in BUILTINS

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and not self._lookup(node.id):
            self.problems.append((node.lineno, node.id))

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        # 海象 ``(text := ...)``：目标在**当前作用域**绑定。
        self._bind_target(node.target)
        self.visit(node.value)

    # ---------- 作用域切换 ----------
    def _visit_function(self, node) -> None:
        parent = self.scope
        scope = _Scope("function", parent)
        self.scope = scope
        args = node.args
        for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
            scope.names.add(arg.arg)
        if args.vararg:
            scope.names.add(args.vararg.arg)
        if args.kwarg:
            scope.names.add(args.kwarg.arg)
        self._collect(node.body)
        for stmt in node.body:
            self.visit(stmt)
        self.scope = parent

    def visit_FunctionDef(self, node) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node) -> None:
        parent = self.scope
        scope = _Scope("function", parent)
        self.scope = scope
        args = node.args
        for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
            scope.names.add(arg.arg)
        if args.vararg:
            scope.names.add(args.vararg.arg)
        if args.kwarg:
            scope.names.add(args.kwarg.arg)
        self.visit(node.body)
        self.scope = parent

    def visit_ClassDef(self, node) -> None:
        parent = self.scope
        self.scope = _Scope("class", parent)
        self.scope.names.add(node.name)
        self._collect(node.body)
        for stmt in node.body:
            self.visit(stmt)
        self.scope = parent

    def _visit_comprehension(self, node) -> None:
        parent = self.scope
        scope = _Scope("function", parent)
        self.scope = scope
        for gen in node.generators:
            self._bind_target(gen.target)
            self.visit(gen.iter)
            for cond in gen.ifs:
                self.visit(cond)
        for field in ("elt", "key", "value"):
            child = getattr(node, field, None)
            if child is not None:
                self.visit(child)
        self.scope = parent

    def visit_ListComp(self, node) -> None:
        self._visit_comprehension(node)

    def visit_SetComp(self, node) -> None:
        self._visit_comprehension(node)

    def visit_GeneratorExp(self, node) -> None:
        self._visit_comprehension(node)

    def visit_DictComp(self, node) -> None:
        self._visit_comprehension(node)


def _source(path: pathlib.Path) -> str:
    raw = path.read_bytes().decode("utf-8")
    # ⚠ 带 BOM 的文件必须先剥 BOM，否则整份文件解析失败 → 被静默跳过。
    return raw.lstrip(BOM)


def _production_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in SCAN_ROOTS:
        files.extend(sorted((REPO_ROOT / root).rglob("*.py")))
    return files


def test_no_production_file_uses_an_unbound_name():
    """生产代码里不得出现「用了但没绑定」的名字（运行时就是 NameError）。

    这道门是**真实跑一本真书**逼出来的：一次跑崩了两处
    （正文生成器 ``prose_temperature``、checkpoint 图 ``slices``），
    两处都过了既有门禁。
    """
    offenders: list[str] = []
    parse_failures: list[str] = []

    for path in _production_files():
        try:
            tree = ast.parse(_source(path), filename=str(path))
        except SyntaxError as exc:
            # 解析失败必须**报出来**，不能跳过（跳过 = 这份文件上没有守卫）。
            parse_failures.append(f"{path.relative_to(REPO_ROOT)}: {exc}")
            continue
        checker = _UndefinedNameChecker()
        checker._collect(tree.body)
        for stmt in tree.body:
            checker.visit(stmt)
        for lineno, name in checker.problems:
            rel = path.relative_to(REPO_ROOT)
            offenders.append(f"{rel}:{lineno}: {name}")

    assert not parse_failures, (
        "以下文件解析失败（等于这些文件上没有这道守卫）：\n" + "\n".join(parse_failures)
    )
    assert not offenders, (
        "生产代码里出现了未绑定的名字（运行时 NameError）：\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    "snippet",
    [
        # 局部 import 在**另一个**方法里用 ⇒ 真实跑章炸过的那一类。
        "def a():\n    from x import f\n    return f()\n\ndef b():\n    return f()\n",
        # 变量在**另一个**方法里赋值 ⇒ checkpoint 图炸过的那一类。
        "def a(self):\n    v = 1\n    return v\n\ndef b(self):\n    return v\n",
    ],
)
def test_the_checker_catches_the_two_shapes_we_actually_hit(snippet: str):
    """守卫自己也得有牙齿：把实测炸过的两种形态注入进去，必须被抓到。

    ⚠ 不许用「把守卫改弱」来证明它会红 —— 那是不可观测变异。
    这里注入的是**真实违规**。
    """
    tree = ast.parse(snippet)
    checker = _UndefinedNameChecker()
    checker._collect(tree.body)
    for stmt in tree.body:
        checker.visit(stmt)

    assert checker.problems, "这两种形态（局部 import 漏到别的方法 / 变量漏到别的方法）必须被抓到"


def test_the_checker_does_not_flag_the_legit_shapes():
    """合法写法不能被误伤（否则实现方只会去把守卫关掉）。

    覆盖：模块级 import、参数、海象、推导式作用域、class body、
    try/except ImportError、global。
    """
    snippet = (
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "CONST = 1\n"
        "\n"
        "def ok(a, b=2, *args, **kwargs):\n"
        "    local = a + b\n"
        "    text = str(local)\n"
        "    rows = [text for text in [a] if text]\n"
        "    return os, Path, CONST, rows, kwargs, args\n"
        "\n"
        "def ok_walrus():\n"
        "    return [(k, v) for k, v in [('a', 1)] if (v := v)]\n"
        "\n"
        "class K:\n"
        "    attr = 1\n"
        "    def m(self):\n"
        "        return self.attr\n"
        "\n"
        "def ok_try():\n"
        "    try:\n"
        "        import json\n"
        "    except ImportError:\n"
        "        json = None\n"
        "    return json\n"
    )
    tree = ast.parse(snippet)
    checker = _UndefinedNameChecker()
    checker._collect(tree.body)
    for stmt in tree.body:
        checker.visit(stmt)

    assert not checker.problems, f"合法写法被误报了：{checker.problems}"
