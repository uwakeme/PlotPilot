"""审查发现的三处运行时缺陷 + 四处接线断裂的回归守卫。

背景
----
第一轮交付（``f7a065fb..f7944889``，40 个提交）在 ``pytest tests/unit``
2405 条全绿的状态下交付，但独立审查查出三条**默认写作路径上必然抛异常**的
缺陷，以及两处「字段被赋值 ≠ 字段被消费」的静默失效。

本文件只做一件事：让这七条**各自可复现地变红**。

事故形态
--------
三条运行期缺陷同源 —— 都是「新增代码插在旧代码中间」或「重构时变量搬了家」：

1. ``chapter_preplanning_service``：D-05 新增的 ``_extract_proposed_target_words``
   被插在 ``@staticmethod`` 与 ``_normalize_string_list`` 之间，
   把本属后者的装饰器吃掉，还留下一份重复的。**任何经实例调用该方法的代码
   都会 TypeError**。旧测试恰好用**类名**调用（类访问不绑定 self）所以全绿。
2. ``audit_delegate``：``anti_ai_severe`` 搬进了纯函数 ``_resolve_audit_pause_gate``，
   调用方 ``run_chapter_audit`` 残留旧分支引用它 ⇒ NameError。
   旧测试只测那个纯函数，调用方从未被执行。
3. ``chapter_narrative_sync``：G-03 把 ``chapter_number + 12`` 改成
   ``_storyline_end(chapter_number, description_ai_end)``，而 ``description_ai_end``
   **从来不存在**。异常被外层 ``except Exception`` 吞成一条 warning，
   整段故事线进度落库被跳过。

**共同教训**：单测停在单元层，覆盖不到「装配是否正确」。
本文件因此除行为断言外，还加了一条 AST 守卫，
它拦的是**整类**事故（装饰器错位 / 实例方法无 self / 死代码里的未定义名）。
"""
import ast
import inspect
import json
from pathlib import Path

import pytest

def _repo_root() -> Path:
    """向上找到仓库根。

    不用 ``parents[N]`` —— 本轮刚修掉过一个 `parents[4]` 少一级的真实事故
    （指到 `tests/` 而非仓库根，靠 cwd 兜底掩盖）。向上搜索不可能算错层级。
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "infrastructure" / "ai" / "prompt_packages").is_dir():
            return parent
    raise AssertionError(f"从 {Path(__file__)} 向上找不到仓库根")


REPO_ROOT = _repo_root()


# ── 通用工具 ────────────────────────────────────────────────────────────

def _module_ast(rel_path: str) -> ast.Module:
    return ast.parse((REPO_ROOT / rel_path).read_bytes().decode("utf-8-sig"))


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_bytes().decode("utf-8-sig")


# ── 1. P0：装饰器错位 / 实例方法签名不匹配 ────────────────────────────────
#
# 这条守卫的作用远大于单个用例：它同时拦下
#   · 同一个方法挂了两个 @staticmethod
#   · 经 self.<name>(...) 调用、但签名里没有 self 也没有装饰器的方法
#   · 类内定义了却在本文件里被 self. 调用、实际定义在别处的名字


def _binding_defects(tree: ast.Module) -> list[str]:
    """扫描模块内所有类，返回「实例调用会炸」的绑定问题。"""
    problems: list[str] = []
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        methods: dict[str, ast.FunctionDef] = {}
        for node in cls.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            decorators = [ast.unparse(d) for d in node.decorator_list]
            if decorators.count("staticmethod") > 1:
                problems.append(
                    f"{cls.name}.{node.name} (L{node.lineno}) 挂了 "
                    f"{decorators.count('staticmethod')} 个 @staticmethod"
                )
            args = [a.arg for a in node.args.args]
            has_self = bool(args) and args[0] == "self"
            is_static = "staticmethod" in decorators
            is_class_method = "classmethod" in decorators
            # 实例方法必须以 self 开头；static/classmethod 不该有 self
            if not is_static and not is_class_method and not has_self:
                problems.append(
                    f"{cls.name}.{node.name} (L{node.lineno}) 是实例方法但首参是 "
                    f"{args[0] if args else '(无)'} —— 经实例调用必 TypeError"
                )
            methods[node.name] = node

        # 找出所有 self.<name>(...) 调用，逐个核对绑定
        for node in ast.walk(cls):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in methods):
                continue
            if not (isinstance(func.value, ast.Name) and func.value.id == "self"):
                continue
            target = methods[func.attr]
            decorators = [ast.unparse(d) for d in target.decorator_list]
            args = [a.arg for a in target.args.args]
            if "staticmethod" in decorators or "classmethod" in decorators:
                continue
            if not args or args[0] != "self":
                problems.append(
                    f"{cls.name} 在 L{node.lineno} 调用 self.{func.attr}()，"
                    f"但该方法 (L{target.lineno}) 首参不是 self"
                )
    return problems


def test_no_instance_binding_defects_in_preplanning_service():
    """P0 的根因是装饰器错位；本守卫覆盖同类问题的整个家族。"""
    defects = _binding_defects(
        _module_ast("application/blueprint/services/chapter_preplanning_service.py")
    )
    assert not defects, "实例方法绑定有问题（会 TypeError）：\n  " + "\n  ".join(defects)


@pytest.mark.parametrize(
    "rel_path",
    [
        "engine/pipeline/base.py",
        "engine/runtime/audit_delegate.py",
        "application/world/services/chapter_narrative_sync.py",
        "application/workflows/auto_novel_generation_workflow.py",
        "application/engine/services/context_budget_policy.py",
        "domain/ai/value_objects/assessment_state.py",
    ],
)
def test_no_instance_binding_defects_in_rewritten_modules(rel_path):
    """本轮改动过的模块都过一遍绑定守卫。"""
    assert not _binding_defects(_module_ast(rel_path))


def test_normalize_string_list_is_callable_on_an_instance():
    """**这条就是 P0 本身**。

    旧测试写的是 ``ChapterPreplanningService._normalize_string_list(...)`` ——
    类访问不绑定 self，因此装饰器错位照样通过。必须经**实例**调用。
    """
    from application.blueprint.services.chapter_preplanning_service import (
        ChapterPreplanningService as C,
    )

    inst = C.__new__(C)  # 绕开 __init__ 的协作依赖，只验绑定
    assert inst._normalize_string_list(["a", "b", "a", None, ""]) == ["a", "b"]
    assert inst._normalize_string_list(None) == []
    assert inst._normalize_string_list("单独一句") == ["单独一句"]


# ── 2. P1：重构后残留的未定义名 ──────────────────────────────────────────


def _module_scope_names(tree: ast.Module) -> set[str]:
    """**只**收集模块级绑定。

    早期版本把全模块所有 ``Name(Store)`` 都当成模块绑定，于是
    ``_resolve_audit_pause_gate`` 里的 ``anti_ai_severe = ...`` 把调用方的
    引用也「掩护」了 —— 事故的形态反而躲过了这条守卫（变异验证发现）。
    模块作用域就是 ``tree.body`` 那一层，不含函数体内部。
    """
    import builtins

    names = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
    for node in tree.body:  # ← 只有模块体，不是 ast.walk
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            # 顶层条件/异常块里也可能绑定（平台分支、__all__ 等）
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    names.add(sub.id)
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for alias in sub.names:
                        names.add((alias.asname or alias.name).split(".")[0])
    return names


def _binding_nodes(fn):
    """收集**绑定**时用的遍历：遇到嵌套 def / class 整个止步。

    与 :func:`_load_nodes` 的区别正是类体：
      · 判定 Load 时要**进**类体（`class C: attr = missing` 该报）
      · 收集绑定时**不进**类体（`class C: attr = 1` 的 attr 不是外层绑定；
        类作用域对方法不构成闭包，`def f(): return attr` 运行时就是 NameError）

    第五轮把类体 Store 并进外层 local，造成 3 类新漏报；第六轮修正。
    """
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _scope_local_names(fn) -> set[str]:
    """函数自身作用域里**直接**绑定的名字（含推导式自己的目标变量）。

    不含嵌套函数的形参/名字 —— 那属于它们各自的作用域。
    """
    local: set[str] = set()
    args = getattr(fn, "args", None)
    if args is not None:  # ast.Module 没有 .args
        for arg in (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)):
            local.add(arg.arg)
        if args.vararg:
            local.add(args.vararg.arg)
        if args.kwarg:
            local.add(args.kwarg.arg)

    # ⚠ 用 _binding_nodes（遇嵌套 def/**class** 止步）而不是 _load_nodes。
    # _load_nodes 要进类体（判 Load 用），但类体里的赋值不是外层绑定。
    # 用 _load_nodes 会让 `class C: attr = 1` 的 attr 泄漏到外层。
    for node in _binding_nodes(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            local.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                local.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            local.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # 嵌套 def/class 的**名字**绑定在外层作用域（虽然它们的体不进来）。
            # 漏了这一支，`def _replace` / `class _LLM` 会被报成未绑定。
            local.add(node.name)
            # ⚠ 只加**名字**，不加类体里的赋值：类作用域不是闭包，
            # `class C: attr = 1` 里的 attr 不是外层可见的名字。
            # （第六轮修：并入外层会造成 `def f(): return attr` 漏报。）
            #
            # ⚠ 第八轮复审：签名部件在**外层作用域**求值，
            # 所以其中 lambda 的形参也绑定在外层 ——
            # `def g(cb=lambda q: q)` 的 q 有定义，不是未绑定名。
            # _binding_nodes 在此止步进不了函数体，所以这里显式取签名部件。
            for expr in _signature_exprs(node):
                for sub in ast.walk(expr):
                    if isinstance(sub, ast.Lambda):
                        local |= _params_of(sub)
        elif isinstance(node, ast.Lambda):
            # lambda 的**形参**绑定在外层作用域（lambda 体也在外层求值），
            # 漏掉这一支会把 `key=lambda item: item[0]` 里的 item 报成未绑定。
            local |= _params_of(node)
        elif isinstance(node, ast.expr):
            # ⚠ 第八轮复审：函数签名部件里嵌的 lambda（`def g(cb=lambda q: q)`、
            # `def m(self, r=lambda s: s)`）—— `_load_nodes` 会把签名表达式
            # 下降进去扫（它们在外层作用域求值），但 lambda 体里的**形参**
            # 绑定在 lambda 自己身上，不在 `_scope_local_names` 的
            # ast.walk 覆盖范围内（那条只在遇到 Lambda 节点本身时才收，
            # 而这里是把 Lambda 作为**表达式的一部分**下降进去的）。
            for sub in ast.walk(node):
                if isinstance(sub, ast.Lambda):
                    local |= _params_of(sub)
        elif isinstance(node, ast.comprehension):
            # 推导式有自己的作用域，但目标变量在本作用域可见
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    local.add(sub.id)
    return local


class _ClassScopeMarker(ast.AST):
    """`_load_nodes` 在进入/离开类体时 yield 的边界标记：携带该类作用域的绑定名。

    它不是真实语法节点，只是一条「从这里开始的 Load 可以额外看见这些名字」
    的通道。**不得**把这些名字并入外层 visible —— 类作用域不是闭包
    （第五轮把类体 Store 并入外层 local，直接造成 3 类漏报）。

    ⚠ 第八轮复审：没有这条通道时，5 类合法 Python 写法全部假红
    （类体互引、方法默认值引用类属性、方法注解引用类内别名、
      模块级/类方法默认 lambda 形参）。

    ⚠ 第九轮复审修正：最初这只是一个「加进去」的单向标记、且生成顺序与
    遍历顺序不一致，导致 4 类**新漏报**：
      · 类定义**之前**的同级语句看见了类属性
      · 类体**之后**的语句也看见了类属性（`visible` 一旦并入就不收回）
      · 类体**前向引用**被放行（`class C: B = A; A = 1` 运行期是 NameError）
      · marker 顺序与 LIFO 栈遍历不一致
    现改为 `enter` / `leave` 成对标记，由 `_check` 用栈按**源码顺序**压栈/弹出，
    类体之外的节点完全不受影响。
    """

    _fields = ("names", "enter")

    def __init__(self, names=None, enter=True):
        self.names = names or []
        self.enter = enter


#: 会把子语句包进**新一层**的语句类型。
#: 它们的分支体与父语句同属一个作用域（`if`/`for`/`try`/`with` 都不引入作用域），
#: 但分支体里若嵌套 def/class，那个 def 的**体**另属函数作用域。
_COMPOUND_STMTS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try,
                   ast.TryStar, ast.With, ast.AsyncWith, ast.Match)


def _compound_header_exprs(stmt):
    """复合语句里在**当前作用域**求值的头部表达式。

    ⚠ 第十一轮复审：`except MissingExc:` 的 ``MissingExc`` 在 handler 的
    **type** 上，既不属于分支体、也不属于迭代目标 —— 两处都不产出它，
    于是 `class C: try: ... except MissingExc:` 整条漏报。

    返回顺序与源码一致：
      · If            → test（elif 的 test 靠 orelse 递归覆盖）
      · For           → iter
      · While         → test（第十二轮复审：原先这里写 `pass`，
                         注释还称「While 没有头部表达式」—— 是错的，
                         `while cond:` 的 cond 就在 test 上，
                         类体路径下它压根没被扫 ⇒ `class C: while missing: pass` 漏报）
      · With          → 每个 item.context_expr
      · Try           → 每个 handler.type
      · Match         → subject / 每个 case.guard
    """
    out = []
    if isinstance(stmt, ast.If):
        out.append(stmt.test)
    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        out.append(stmt.iter)
    elif isinstance(stmt, ast.While):
        out.append(stmt.test)
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        out += [i.context_expr for i in stmt.items]
    elif isinstance(stmt, (ast.Try, ast.TryStar)):
        out += [h.type for h in stmt.handlers if h.type is not None]
    elif isinstance(stmt, ast.Match):
        if stmt.subject is not None:
            out.append(stmt.subject)
        out += [c.guard for c in stmt.cases if c.guard is not None]
    return [e for e in out if e is not None]


def _match_pattern_names(pattern) -> set[str]:
    """从 ``match`` 的模式里取出**在 case 体中已绑定**的捕获名。

    ⚠ 第十二轮复审：``case [a, b]: B = a`` 的 a/b 是捕获模式（as 模式），
    运行期在进入 case 体前就已绑定。此前完全没建模 ⇒ 3 类假红。
    同时 ``case MissingPoint(p)`` 的 ``MissingPoint`` 是**要加载的类名**
    （必须在外层作用域可见），不能算捕获名 —— 它属于 header 表达式。
    """
    names: set[str] = set()
    for node in ast.walk(pattern):
        if isinstance(node, ast.MatchAs):
            if node.pattern is None and node.name:
                # 捕获名：`case x:` / `case [a]:` 里的 a
                names.add(node.name)
            elif node.pattern is not None and node.name:
                # `case [a] as whole:` 的 whole
                names.add(node.name)
        elif isinstance(node, ast.MatchStar):
            if node.name:
                names.add(node.name)
        elif isinstance(node, ast.MatchMapping):
            # ⚠ 第十三轮复审：`case {'k': val, **rest}` 的 rest 是捕获名
            # （**rest 形式的 rest 绑定到整个剩余字典），
            # 此前完全没建模 ⇒ `B = rest` 被误报。
            if node.rest:
                names.add(node.rest)
    return names


def _match_pattern_loads(pattern) -> list:
    """模式里需要在外层作用域**解析**的 Load（类名等）。

    ``case MissingPoint(p)`` 的 ``MissingPoint`` 必须已定义；``case int(x)``
    的 ``int`` 同理。捕获名本身不是 Load（它是绑定）。

    ⚠ 第十四轮复审：``case {Keys.KEY: val}`` 的 ``Keys.KEY`` 也要算 ——
    mapping 的**键**是表达式（与 MatchClass 的 `kwd_attrs` 不同，后者是字符串）。
    字面键 ``{'k': val}`` 的 'k' 是字符串常量，不产生 Load。
    """
    out = []
    for node in ast.walk(pattern):
        if isinstance(node, ast.MatchClass):
            if node.cls is not None:
                out.append(node.cls)
            # ⚠ `kwd_attrs` 是**字符串**（属性名），不是表达式 —— 第十三轮复审
            # 指出这里原有一句 `[kw for kw in node.kwd_attrs if False]` 的死表达式，
            # 已删。属性名不产生 Load。
        elif isinstance(node, ast.MatchValue):
            out.append(node.value)
        elif isinstance(node, ast.MatchMapping):
            # ⚠ 第十四轮复审：mapping 的**键**是表达式（`Keys.KEY`），
            # 在外层作用域求值 ⇒ 未定义必须报出。
            # （值 `val` 是捕获名，由 `_match_pattern_names` 负责。）
            out += [k for k in node.keys if k is not None]
    return out


def _compound_target_bindings(stmt) -> set[str]:
    """复合语句里「头部之后、分支体之前」绑定的名字。

    Python 的求值顺序：``for x in xs:`` 先算 ``xs``、再绑 ``x``；
    ``with C() as x:`` 先求上下文表达式、再绑 ``x``；
    ``except E as e:`` 先解包异常、再绑 ``e``。这三类都落在
    「头部表达式之后、分支体之前」那一层，漏了会让
    ``for _i in (1,): B = _i`` 把 ``_i`` 误报成未绑定。
    """
    out: set[str] = set()
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        for sub in ast.walk(stmt.target):
            if isinstance(sub, ast.Name):
                out.add(sub.id)
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        for item in stmt.items:
            if item.optional_vars is not None:
                for sub in ast.walk(item.optional_vars):
                    if isinstance(sub, ast.Name):
                        out.add(sub.id)
    if isinstance(stmt, (ast.Try, ast.TryStar)):
        for h in stmt.handlers:
            if h.name:
                out.add(h.name)
    return out


def _compound_sub_bodies(stmt):
    """取复合语句里所有「同作用域」的分支体语句列表。

    ⚠ 第十二轮复审（语义区分）：并非所有分支都「各自独立」。
      · ``try`` / ``except`` / ``finally``、``for`` / ``while`` 的 body 与
        orelse —— **顺序执行、共享一个作用域**：
        ``try: A=1; B=A / except: C=B`` 里 B 在 except 体可见。
      · 只有 ``if`` / ``elif`` / ``else`` 是**互斥分支**：
        ``if: A=1 / else: B=A`` 里 B=A 在运行期是 NameError。
    用 ``exclusive=True`` 标记互斥分支，调用方据此决定 seen 是否共享。
    """
    if isinstance(stmt, ast.If):
        out = [stmt.body]
        if stmt.orelse:
            out.append(stmt.orelse)
        return out, True          # if/else 互斥
    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
        # body 与 orelse 顺序执行（循环可能跑 0 次，但 Python 里它们同属
        # 一个函数/类作用域，名字可见性不按分支切分）
        return [stmt.body] + ([stmt.orelse] if stmt.orelse else []), False
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return [stmt.body], False
    if isinstance(stmt, ast.Match):
        return [c.body for c in stmt.cases], True    # case 互斥（但捕获名另算）
    # Try / TryStar：body → handlers → orelse → finalbody 顺序执行
    out = [stmt.body, stmt.orelse, stmt.finalbody]
    out += [h.body for h in stmt.handlers]
    return [b for b in out if b], False


def _scope_local_names_of_body(body) -> set[str]:
    """收集「一段类体语句」在自己作用域内定义的名字。

    与 `_scope_local_names` 的区别：这里**要**收类体里的赋值
    （`class C: A = 1` 里 A 是类属性），而 `_scope_local_names` 用于外层可见性、
    必须**不**收它。两者刻意相反。

    ⚠ 第九轮复审修正：原实现用 ``ast.walk(stmt)`` 逐语句展开，会把
    **方法体里的局部变量**收成类属性 ——
    ``class C: def m(self): hidden = 1; B = hidden`` 里的 hidden
    运行期是该方法的局部，不是类属性。现改为：FunctionDef/AsyncFunctionDef/ClassDef
    只取**签名部件**（含其中的 lambda 形参），绝不下降进它们的**体**。

    ⚠ 这里**不处理前向引用**：``class C: B = A; A = 1`` 的 B 在运行期是
    NameError，应由 `_load_nodes` 的顺序化检查负责（见 `_ClassScopeMarker` 的用法）。

    ⚠ 第十一轮复审修正：复合语句（``if`` / ``for`` / ``while`` / ``try`` /
    ``with``）原先落到最后的 ``ast.walk(stmt)`` 分支，**无差别下降**，
    于是
      ``class C: if True: B = hidden; def m(self): hidden = 1``
    里 B（类体层）看得见 hidden（方法**体**的局部）⇒ 漏报。
    Python 里 ``if`` 块属于类作用域、``def`` 体属于函数作用域 ——
    两层不可互相穿透。现改为对复合语句**逐分支**处理：
    分支内的赋值算类作用域，嵌套 def/class 只取签名部件。
    """
    names: set[str] = set()
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # 方法/嵌套类的**名字**绑定在类作用域（它们是类属性）
            names.add(stmt.name)
            # ⚠ 只有签名部件在类作用域求值（含其中的 lambda 形参）；
            # 方法**体**的局部一律不是类属性。
            for expr in _signature_exprs(stmt):
                for sub in ast.walk(expr):
                    if isinstance(sub, ast.Lambda):
                        names |= _params_of(sub)
            continue
        if isinstance(stmt, _COMPOUND_STMTS):
            # 复合语句：分支内是**同一个**作用域（类作用域），
            # 但嵌套 def/class 的体另算 —— 递归时必须再进一层。
            # ⚠ 循环目标（`for _i in xs` 的 _i）绑定在**当前**作用域 ——
            # 漏了它 `B = _i` 会被误报成未绑定。
            if isinstance(stmt, (ast.For, ast.AsyncFor)):
                for sub in ast.walk(stmt.target):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
            # `with ... as x` 的 x 同样绑定在当前作用域
            if isinstance(stmt, (ast.With, ast.AsyncWith)):
                for item in stmt.items:
                    if item.optional_vars is not None:
                        for sub in ast.walk(item.optional_vars):
                            if isinstance(sub, ast.Name):
                                names.add(sub.id)
            # `except E as e` 的 e 绑定在当前作用域
            if isinstance(stmt, (ast.Try, ast.TryStar)):
                for h in stmt.handlers:
                    if h.name:
                        names.add(h.name)
            # `match` 的捕获名（`case [a, b]:` 的 a/b）绑定在**该 case 体内**，
            # 不是外层。收集时逐 case 处理（第十二轮复审）。
            #
            # ⚠ 第十三轮复审：捕获名**不能**并进外层 names ——
            # 并进去后 case 体里的 `a` 反而被判成「外层可见但未定义」。
            # 只收 case **体**里定义的普通名字（那些对后续 case 可见）。
            if isinstance(stmt, ast.Match):
                for case in stmt.cases:
                    names |= _scope_local_names_of_body(case.body)
                continue
            # ⚠ 第十二/十三轮复审：`_compound_sub_bodies` 的返回值是
            # `(bodies, exclusive)` **元组**，这里必须解包 ——
            # 上一轮改了它的签名却漏改这一处，于是
            # `class C: if True: A=1 / elif False: B=2` 这类
            # **类体内含 elif 或嵌套复合语句**的代码让探针直接抛
            # `AttributeError: 'list' object has no attribute '_fields'`。
            # 守卫文件恰好都没触发，所以一直没人撞上。
            # `_ex`（exclusive）在这里用不到 —— 名字收集不关心分支是否互斥，
            # 只有**遍历**路径才需要它（见 `_load_nodes` / `_class_scope_nodes`）。
            _bodies, _ex = _compound_sub_bodies(stmt)
            for inner in _bodies:
                names |= _scope_local_names_of_body(inner)
            # 头部表达式（条件 / except 类型 / 迭代目标本身）只产生 Load，
            # 不产生绑定，这里无需处理。
            continue
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for alias in stmt.names:
                names.add((alias.asname or alias.name).split(".")[0])
            continue
        if isinstance(stmt, ast.Lambda):
            names |= _params_of(stmt)
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, ast.Lambda):
                # ⚠ 第九轮复审：类体语句里嵌的 lambda 形参绑定在**类作用域**
                # —— `class C: dec = staticmethod(lambda f: f)` 的 f 在类体求值，
                # 不是外层绑定。原先漏收 ⇒ 误报。
                names |= _params_of(node)
    return names


def _signature_exprs(node):
    """取 FunctionDef/AsyncFunctionDef 的签名部件表达式。

    装饰器 / 默认值 / 注解 / 返回注解 —— 这些在**定义它的那层作用域**求值，
    不是在被定义的函数里求值。第五、六轮复审的「签名部件未扫描」就是漏在这里。

    收敛成单一实现：此前同样的表达式列表在 `_load_nodes` 里内联了两遍
    （函数级与类体方法级），容易只改一处 ⇒ 覆盖不均。

    ⚠ 只接受 FunctionDef —— ``ClassDef`` 没有 ``.args``，调用方需自行判断。
    """
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return list(getattr(node, "decorator_list", []))
    out = list(node.decorator_list)
    out += [d for d in node.args.defaults if d is not None]
    out += [d for d in node.args.kw_defaults if d is not None]
    if getattr(node, "returns", None) is not None:
        out.append(node.returns)
    for a in (list(node.args.posonlyargs) + list(node.args.args)
              + list(node.args.kwonlyargs)
              + ([node.args.vararg] if node.args.vararg else [])
              + ([node.args.kwarg] if node.args.kwarg else [])):
        if getattr(a, "annotation", None) is not None:
            out.append(a.annotation)
    return out


def _match_scope_nodes(stmt, seen):
    """产出 `match` 语句的节点序列，返回结束时的 `seen`。

    ⚠ 第十二轮复审：`match` 里有**两种**可见性，不能混为一谈：
      · subject 与**模式类名**（``case MissingPoint(p)`` 的 MissingPoint、
        ``case int(x)`` 的 int、``case CONST`` 的值）—— 在**外层**作用域求值，
        未定义必须报出
      · **捕获名**（``case [a, b]:`` 的 a/b、``case x:`` 的 x、
        ``case [a] as whole:`` 的 whole、``case [a, *rest]:`` 的 rest）——
        运行期在进入 case 体**之前**已绑定，在 case 体内可见
    """
    outer = set(seen)
    if stmt.subject is not None:
        # ⚠ 第十三轮复审：原先 `yield stmt.subject` 之后又
        # `yield from _ordered_subtree(stmt.subject)` —— subject 被扫**两遍**，
        # 于是 `class C: v=[1,2]; match v:` 的 v 被报两次未绑定。
        # 而且 subject 没包 `_ClassScopeMarker` ⇒ 它看不见外层已定义的 v。
        with_marker = sorted(outer)
        if with_marker:
            yield _ClassScopeMarker(with_marker, enter=True)
        yield from _ordered_subtree(stmt.subject)
        if with_marker:
            yield _ClassScopeMarker(enter=False)

    for case in stmt.cases:
        case_names = _match_pattern_names(case.pattern)
        with_marker = sorted(outer)
        if with_marker:
            yield _ClassScopeMarker(with_marker, enter=True)
        for load in _match_pattern_loads(case.pattern):
            yield from _ordered_subtree(load)
        if with_marker:
            yield _ClassScopeMarker(enter=False)

        # ⚠ 第十三轮复审：guard 表达式在 case 体**之前**求值，
        # 但此时捕获名**已经**绑定（`case [a, b] if a > 0:` 的 a 在 guard 里可用）。
        # 原先 guard 只包 outer marker ⇒ 把 a 报成未绑定。
        if case.guard is not None:
            guard_names = sorted(outer | case_names)
            if guard_names:
                yield _ClassScopeMarker(guard_names, enter=True)
            yield from _ordered_subtree(case.guard)
            if guard_names:
                yield _ClassScopeMarker(enter=False)

        body_names = sorted(outer | case_names)
        if body_names:
            yield _ClassScopeMarker(body_names, enter=True)
        for s2 in case.body:
            if isinstance(s2, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield s2
                for e2 in _signature_exprs(s2):
                    yield from _ordered_subtree(e2)
                continue
            if isinstance(s2, ast.ClassDef):
                yield from _class_scope_nodes(s2, outer_seen=set(body_names))
                continue
            yield from _ordered_subtree(s2)
        if body_names:
            yield _ClassScopeMarker(enter=False)
        # case 体里定义的类属性对后续 case 可见（match 是顺序执行的）
        # ⚠ 第十三轮复审（已知保守近似）：不同 case 之间**实际是互斥**的
        # （`case 1: A=1 / case 2: B=A` 在 v≠1 时 B=A 会 NameError），
        # 但这里把前一个 case 的定义并进了 outer ⇒ 该情形被**放行**（漏报）。
        # 这是刻意的保守近似：match 的 case 何时命中取决于运行期数据，
        # 静态判定「B=A 是否可达」需要值流分析，代价远大于收益。
        #
        # ⚠ 第十四轮复审（另一处保守近似）：**捕获名**不进 outer，
        # 所以 `match v: case [a]: X=1` 之后的 `B=a` 会被报未绑定 ——
        # 而运行期若该 case 命中，a 是有绑定的。
        # 这里同样选择保守（宁可多报），因为「该 case 是否命中」是运行期数据。
        # 两个保守方向相反（跨 case 的普通定义放行、捕获名不放行），
        # 根源都是同一个：静态无法判定 case 是否命中。
        outer |= _scope_local_names_of_body(case.body) | case_names


def _ordered_subtree(node):
    """产出 ``node`` 及其全部后代，顺序严格等于源码顺序。

    ⚠ 第十轮复审：不能用外层 LIFO ``stack`` 下降 —— 那些节点会在
    ``_ClassScopeMarker(enter=False)`` **之后**才被弹出，落���类作用域之外。
    这里用**独立** LIFO 做有序 DFS（push 反序、pop 正序）。
    """
    stack = [node]
    stack.reverse()
    while stack:
        cur = stack.pop()
        yield cur
        kids = list(ast.iter_child_nodes(cur))
        kids.reverse()
        stack.extend(kids)


def _class_scope_nodes(node, outer_seen=None):
    """产出一个 ClassDef 自身的类作用域节点序列（供嵌套类复用）。

    ⚠ 嵌套类有**自己的**类作用域：内层的类体不该看见外层的绑定
    （``class A: ax=1; class B: bz=ax`` 运行期是 NameError，必须报）。

    ⚠ 但嵌套类的**头**（bases / keywords / 装饰器）在**外层**作用域求值 ——
    ``class A: Base = object`` 后 ``class B(Base)`` 是**合法**的，
    基类名要在 `outer_seen` 里才可见。
    第十一轮复审：原先头表达式无条件 yield、不带可见集 ⇒ 3 个误报。

    ⚠ 复合语句分支与本类同属一个作用域，逐分支递归处理。
    """
    seen: set[str] = set(outer_seen or ())
    # 头（基类 / metaclass / 装饰器）在**外层**作用域求值
    for expr in list(node.bases) + list(node.keywords) + list(node.decorator_list):
        if seen:
            yield _ClassScopeMarker(sorted(seen), enter=True)
        yield from _ordered_subtree(expr)
        if seen:
            yield _ClassScopeMarker(enter=False)
    # ⚠ 类体本身**不继承** outer_seen：类作用域不是闭包。
    # `class A: ax=1; class B: bz=ax` 在运行期是 NameError（须报），
    # 而 `class B(Base)` 的 Base 在**头**里求值（上面已按外层可见集处理）。
    seen = set()

    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # ⚠ 第十二轮复审：嵌套类里的方法。方法**体**另有作用域 ——
            # 整条下降会把 `def m: z=1; return z` 的 z 当成类作用域的名字
            # ⇒ 假红。只扫签名部件，方法名绑定在类作用域。
            yield stmt
            for expr in _signature_exprs(stmt):
                yield from _ordered_subtree(expr)
            seen.add(stmt.name)
            continue
        if isinstance(stmt, ast.ClassDef):
            # 头继承**外层** seen（含本类此前定义的属性），
            # 类体用自己的作用域 —— 见上面 outer_seen / seen=set() 的分工。
            yield from _class_scope_nodes(stmt, outer_seen=seen)
            seen.add(stmt.name)
            continue
        if isinstance(stmt, _COMPOUND_STMTS):
            # 分支与本类同作用域：分支体里的赋值也要进 seen；
            # 头部表达式（except 类型 / if 条件 / 迭代目标）在当前作用域求值。
            # ⚠ 第十二轮复审：头部与分支体都用**前置** seen（顺序语义），
            # 全部跑完后才把分支内定义并回 seen。
            prefix = set(seen)
            body_prefix = _compound_target_bindings(stmt)
            if isinstance(stmt, ast.Match):
                for n in _match_scope_nodes(stmt, prefix):
                    yield n
                seen = prefix | _scope_local_names_of_body([stmt])
                continue
            for expr in _compound_header_exprs(stmt):
                if prefix:
                    yield _ClassScopeMarker(sorted(prefix), enter=True)
                yield from _ordered_subtree(expr)
                if prefix:
                    yield _ClassScopeMarker(enter=False)
            branch_defs: set[str] = set()
            _shared_seen: set[str] = set()
            _bodies, _exclusive = _compound_sub_bodies(stmt)
            for inner in _bodies:
                sub_seen = (set(prefix) if _exclusive
                            else set(prefix) | _shared_seen | body_prefix)
                for s2 in inner:
                    if isinstance(s2, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        # ⚠ 第十二轮复审：嵌套类方法体曾被当类作用域语句下降
                        # ⇒ `class A: class B: def m: z=1; return z` 假红 z。
                        # 方法**体**另有作用域，只扫签名部件。
                        yield s2
                        for e2 in _signature_exprs(s2):
                            yield from _ordered_subtree(e2)
                        sub_seen = sub_seen | {s2.name}
                        branch_defs.add(s2.name)
                        continue
                    if isinstance(s2, ast.ClassDef):
                        yield from _class_scope_nodes(s2, outer_seen=sub_seen)
                        sub_seen = sub_seen | {s2.name}
                        branch_defs.add(s2.name)
                        continue
                    # ⚠ 第十四轮复审：这里**不需要**特判 ast.Match ——
                    # 类体复合分支里的 match 由 `_load_nodes` 那条路径处理
                    # （`class C: if True: match v: case [a]: B=a`）。
                    # 本函数是**嵌套类**路径，类体里的 match 走
                    # `_class_scope_nodes` 自己的 Match 分支（见上）。
                    # ⚠ 我曾在这里也加过一份 Match 特判，变异验证显示它
                    # **完全不被走到**（GREEN）⇒ 属冗余，已删。
                    here = sub_seen | _scope_local_names_of_body([s2])
                    if here:
                        yield _ClassScopeMarker(sorted(here), enter=True)
                    yield from _ordered_subtree(s2)
                    if here:
                        yield _ClassScopeMarker(enter=False)
                    sub_seen = here
                if not _exclusive:
                    _shared_seen |= (sub_seen - prefix)
                branch_defs |= (sub_seen - prefix)
            seen = prefix | branch_defs | _shared_seen
            continue
        here = seen | _scope_local_names_of_body([stmt])
        if here:
            yield _ClassScopeMarker(sorted(here), enter=True)
        yield from _ordered_subtree(stmt)
        if here:
            yield _ClassScopeMarker(enter=False)
        seen = here


def _load_nodes(fn):
    """函数体内需要在**该函数作用域**下判定的 Load 节点。

    语义（Python 作用域规则）：
    - 嵌套 ``def`` / ``async def`` / ``class`` 的**体**有自己的作用域 ⇒ 跳过，
      它们会被单独检查；否则会把它们的局部变量算到外头（假阳性）。
    - 嵌套 ``def`` / ``class`` 的**名字**绑定在外层 ⇒ 由 ``_scope_local_names`` 收录。
    - ``lambda`` 的体**在外层作用域求值**（闭包）⇒ 必须下降进去，
      它的形参由 ``_scope_local_names`` 的 Lambda 分支收录。

    第三版把整类嵌套体都跳过了，连 lambda 与类体一起丢 —— 那才是覆盖回归的根因。
    """
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 签名部件（装饰器/默认值/注解/返回注解）在**定义它的作用域**求值，
            # 不是在被定义的函数里求值 —— 所以它们属于外层，要继续扫。
            stack.extend(_signature_exprs(node))
            continue  # 函数**体**另有作用域
        if isinstance(node, ast.ClassDef):
            # 类体本身在**外层作用域**求值（``class C: attr = missing`` 里的
            # ``missing`` 该报），但类**方法体**有自己的作用域 —— 类作用域
            # 对方法不构成闭包。所以进类体、跳过方法。
            # 基类列表同理：``class C(MissingBase)`` 里的 MissingBase 在外层求值。
            #
            # ⚠ 类体里的 ``Name(Store)``（``class C: attr = 1``）**不能**算进
            # 外层绑定 —— 类名不是闭包，`def f(): return attr` 在运行期就是
            # NameError。第五轮把类体 Store 并入外层 local 造成 3 类新漏报。
            # 这里只把「读取」交给外层判，类体自己的赋值不进 visible。
            #
            # ⚠ 第八轮复审：类体构成一个**独立命名空间**，类体内的引用
            # 能看见类体的绑定 —— ``class C: A = 1; B = A`` 里的 A 有定义，
            # ``class C: helper = 1; def m(self, x=helper)`` 的默认值在
            # 类体作用域求值也能看见 helper，``class C: T = int; def m(self, x: T)``
            # 的注解同理。把它们报成未绑定是假阳性。
            #
            # ⚠ 第九轮复审修正：这些绑定**只**对类体子树可见。
            #   · 基类/关键字/装饰器在外层作用域求值 ⇒ 用**外层**可见集，
            #     必须在 enter 标记**之前**
            #   · 类体语句按源码顺序 ⇒ 用 enter/leave 成对包住
            #   · 类体之后的同级语句看不见它们
            #
            # ⚠ 类体是**顺序执行**的，`class C: B = A; A = 1`
            # 在运行期就是 NameError。若用整段类体的绑定快照，B 那行会
            # 看见后面才定义的 A ⇒ 漏报。
            # 改为**逐语句** enter/leave：每条语句只看见它**之前**已定义的名字。
            #
            # 基类 / metaclass 关键字 / 类装饰器在**外层作用域**求值，
            # 且必须**子树下降** —— 第十轮复审：
            # 只 `yield expr` 根节点会让 `class C(undefined_mod.Base)`、
            # `@undefined_mod.deco`、`x: undefined_mod.T` 全部漏报。
            for expr in list(node.bases) + list(node.keywords) + list(node.decorator_list):
                yield from _ordered_subtree(expr)
            seen: set[str] = set()
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # 方法签名部件（装饰器/默认值/注解）在**类体作用域**求值，
                    # 单独扫它们的表达式（第六轮复审 §3.3）。
                    # ⚠ 方法签名部件的可见集 = **之前**已定义的类属性
                    # （不是整段快照 —— 否则 `def m(self, x=later)` + 后面的
                    # `later=1` 会被放行，而 def 执行时 later 尚未绑定）
                    #  ∪ 签名部件里那些 lambda 的形参（它们绑定在类作用域）。
                    sig_lambda_params: set[str] = set()
                    for expr in _signature_exprs(stmt):
                        for sub in ast.walk(expr):
                            if isinstance(sub, ast.Lambda):
                                sig_lambda_params |= _params_of(sub)
                    names_here = sorted(seen | sig_lambda_params)
                    if names_here:
                        yield _ClassScopeMarker(names_here, enter=True)
                    yield stmt
                    for expr in _signature_exprs(stmt):
                        yield from _ordered_subtree(expr)
                    if names_here:
                        yield _ClassScopeMarker(enter=False)
                    seen.add(stmt.name)
                    continue  # ⚠ 方法**体**另有作用域，绝不能下降进去
                if isinstance(stmt, ast.ClassDef):
                    # ⚠ 嵌套类有**自己的**类作用域：内层的 x 不该看见外层的，
                    # 外层的属性也不该被内层看见（Python 里那是 NameError）。
                    # 把嵌套类当普通语句下降会让 `class A: class B: x=1; y=x`
                    # 的 y 看见**外层** seen ⇒ 假红。
                    # ⚠ 第十一轮复审：头表达式（基类/装饰器）在**外层**作用域
                    # 求值 —— `class A: Base = object` 后 `class B(Base)` 合法。
                    # 所以把外层 seen 传进去，让头在外层可见集里判。
                    yield from _class_scope_nodes(stmt, outer_seen=seen)
                    seen.add(stmt.name)
                    continue
                if isinstance(stmt, _COMPOUND_STMTS):
                    # 复合语句的分支与本类**同属一个作用域**：
                    # `if True:` 不引入新作用域，类属性在分支内外都可见。
                    # 但分支内的 def/class 体另算 —— 逐分支递归。
                    #
                    # ⚠ 第十二轮复审（顺序语义）：头部与分支体都只能用
                    # **前置**的 seen。原先先把整条语句的绑定并进 seen 再扫头部，
                    # 于是 `if later: later = 1` / `while cond: cond = False`
                    # 的条件提前看见**后面才定义**的名字 ⇒ 4 类漏报
                    # （运行期都是 NameError）。
                    # 正确顺序：
                    #   ① 头部用前置 seen（条件里的名字必须在**之前**已绑定）
                    #   ② 分支体各自从前置 seen 起步，逐语句增量推进
                    #   ③ 全部分支跑完后，才把分支体内定义的并回 seen
                    prefix = set(seen)
                    _shared_seen: set[str] = set()
                    # `for _i in xs` 的 _i、`with as x`、`except E as e`
                    # 绑定在**当前**作用域，但**晚于**头部表达式求值
                    # （`for x in xs:` 先算 xs 再绑 x）⇒ 头部用 prefix 即可，
                    # 它们属于 body_prefix（头部与分支体之间那一层）。
                    body_prefix = _compound_target_bindings(stmt)
                    if isinstance(stmt, ast.Match):
                        for n in _match_scope_nodes(stmt, prefix):
                            yield n
                        seen = prefix | _scope_local_names_of_body([stmt])
                        continue
                    for expr in _compound_header_exprs(stmt):
                        if prefix:
                            yield _ClassScopeMarker(sorted(prefix), enter=True)
                        yield from _ordered_subtree(expr)
                        if prefix:
                            yield _ClassScopeMarker(enter=False)
                    branch_defs: set[str] = set()
                    _bodies, _exclusive = _compound_sub_bodies(stmt)
                    for inner in _bodies:
                        # ⚠ 互斥分支（if/else、match case）各自从前置 seen 起步 ——
                        # `if: A=1 / else: B=A` 的 B=A 运行期是 NameError。
                        # 顺序执行的块（try/except、for/while）共享 seen：
                        # `try: A=1; B=A / except: C=B` 里 B 在 except 可见。
                        # ⚠ 循环目标 / with-as / except-as 在**头部之后、
                        # 分支体之前**绑定 —— Python 先算 `xs` 再绑 `_i`。
                        # 所以顺序块的起点是 prefix ∪ 这些绑定。
                        sub_seen = (set(prefix) if _exclusive
                                    else set(prefix) | _shared_seen | body_prefix)
                        for s2 in inner:
                            if isinstance(s2, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                yield s2
                                for expr in _signature_exprs(s2):
                                    yield from _ordered_subtree(expr)
                                sub_seen = sub_seen | {s2.name}
                                branch_defs.add(s2.name)
                                continue
                            if isinstance(s2, ast.ClassDef):
                                yield from _class_scope_nodes(s2, outer_seen=sub_seen)
                                sub_seen = sub_seen | {s2.name}
                                continue
                            if isinstance(s2, ast.Match):
                                # ⚠ 第十四轮复审：同上，嵌套类路径的
                                # 复合分支 inner 循环也要特判 Match。
                                yield from _match_scope_nodes(s2, sub_seen)
                                sub_seen = sub_seen | _scope_local_names_of_body([s2])
                                branch_defs |= (sub_seen - prefix)
                                continue
                            h2 = sub_seen | _scope_local_names_of_body([s2])
                            if h2:
                                yield _ClassScopeMarker(sorted(h2), enter=True)
                            yield from _ordered_subtree(s2)
                            if h2:
                                yield _ClassScopeMarker(enter=False)
                            sub_seen = h2
                        if not _exclusive:
                            # 顺序块：把已定义的并回共享 seen
                            _shared_seen |= (sub_seen - prefix)
                        branch_defs |= (sub_seen - prefix)
                    seen = prefix | branch_defs | _shared_seen
                    continue
                # ⚠ 本条语句的可见集 = 之前已定义的 ∪ **本条自己定义的**。
                # 后者不可省：`dec = staticmethod(lambda f: f)` 是首条语句，
                # seen 为空，但 lambda 形参 f 在这条语句里就要能看见。
                here = seen | _scope_local_names_of_body([stmt])
                if here:
                    yield _ClassScopeMarker(sorted(here), enter=True)
                # ⚠ 这里**不需要**特判 ast.Match —— 类体里的 match 已被上面的
                # `_COMPOUND_STMTS` 分支先拦截（Match 在该元组里），
                # 走的是 `_match_scope_nodes` 路径。
                # 函数/模块级的 match 走下面的**通用下降路径**（:980 附近）。
                yield stmt
                yield from _ordered_subtree(stmt)
                if here:
                    yield _ClassScopeMarker(enter=False)
                seen = here
            continue
        if isinstance(node, ast.Match):
            # ⚠ 第十三轮复审：函数/模块级的 match 也需要捕获名建模 ——
            # 通用下降路径会把 match 子树铺平下降，捕获名
            # \case [a]:\ 的 a 因而被判未绑定。改走 _match_scope_nodes。
            yield from _match_scope_nodes(node, set())
            continue
        stack.extend(ast.iter_child_nodes(node))


def _unbound_in_tree(tree: ast.Module) -> list[str]:
    """扫一棵树，返回未绑定 Load 列表。**这是探针的唯一实现**。"""
    parent: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node

    module_bound = _module_scope_names(tree)
    unbound: list[str] = []

    def _check(label, scope_fn, scope_nodes) -> None:
        visible = set(module_bound)
        cursor = scope_fn
        while cursor is not None:
            if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.Lambda, ast.Module)):
                visible |= _params_of(cursor)
                visible |= _scope_local_names(cursor)
            cursor = parent.get(id(cursor))
        class_scope: list[set[str]] = []
        for node in scope_nodes:
            if isinstance(node, _ClassScopeMarker):
                # enter/leave 成对维护栈 —— 类体之外的节点看不见这些名字。
                # ⚠ 第九轮复审：原实现只做 `visible |= names` 且不收回，
                # 于是类定义**之前**与**之后**的同级语句都被放行（4 类新漏报）。
                if node.enter:
                    class_scope.append(set(node.names))
                elif class_scope:
                    class_scope.pop()
                continue
            extra = set().union(*class_scope) if class_scope else set()
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id not in visible and node.id not in extra):
                unbound.append(f"{label}:{node.lineno} 引用未绑定的 {node.id!r}")

    # 函数体扫描（含其内 lambda 体、类体；跳过嵌套 def/class 的体 —— 它们另有作用域）
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        _check(fn.name, fn, _load_nodes(fn))

    # 模块级（顶层代码）。
    # ⚠ 必须用 _load_nodes 而不是 ast.walk(tree)：ast.walk 会把函数体再扫一遍，
    # 而那次扫描的可见集合只有模块作用域 ⇒ 大批假阳性。
    # ⚠ 更不能直接传 tree.body：那里面只有语句，没有 ast.Name，
    # 模块级的未绑定名会被整个漏掉（第四轮复审实证）。
    # ⚠ scope_fn 必须传 tree 本身：传 None 时 `_scope_local_names` 从不执行，
    # 模块级的 lambda 形参与推导式目标就不在可见集合里 ⇒ 误报
    #（第五轮复审实证：`f = lambda item: item` 与 `X=[i for i in ...]` 被报未绑定）。
    _check("<module>", tree, _load_nodes(tree))
    return unbound


def _unbound_names(rel_path: str) -> list[str]:
    """找出**模块内**只被 Load、在本函数与所有外层作用域都无绑定的名字。

    针对本轮事故的形态：重构时把局部变量搬进 helper，调用方还留着旧引用。
    不跟跨模块导入 —— 只报模块内可见的未绑定名。
    """
    return _unbound_in_tree(_module_ast(rel_path))


def _params_of(fn) -> set[str]:
    """函数/lambda 的形参名（含 *args / **kwargs）。Module 没有 .args ⇒ 返回空集。"""
    a = getattr(fn, "args", None)
    if a is None:
        return set()
    names = {x.arg for x in (list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs))}
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)
    return names


@pytest.mark.parametrize(
    "rel_path",
    [
        # `anti_ai_severe` 事故的现场
        "engine/runtime/audit_delegate.py",
        # `description_ai_end` 事故的现场
        "application/world/services/chapter_narrative_sync.py",
        # `PlotArc` NameError 的现场：另一个函数里有同名 import，
        # 本函数直接用会被外层 except 吞成 warning。f7944889 已存在，
        # 是靠「独立审查 + unbound 扫描」抓到的，不是靠单测。
        "application/workflows/auto_novel_generation_workflow.py",
        # 本轮反复改动的两个高风险文件
        "engine/pipeline/base.py",
        "application/blueprint/services/chapter_preplanning_service.py",
        # 第四轮复审：扫描器本身能检出该文件的问题，只是此前
        # 不在守卫名单里，于是「死方法里的 NameError」长期无人报警。
        "application/blueprint/services/setup_main_plot_suggestion_service.py",
        # 第八轮复审（阻塞）：`_resolve_plant_offset` 的返回注解用了 `Tuple`
        # 而 import 行没有它。本地 Python 3.14 的 PEP 649 懒求值掩盖了它，
        # CI 的 3.11 在函数定义时求值注解 ⇒ import 该模块直接 NameError。
        # ⚠ 教训同上一条：**问题在「名单里没有这个文件」**，不是「扫描器看不见」。
        "application/analyst/services/state_updater.py",
        # G-01/G-03 的裁决器本体（第八轮复审的 NaN/Inf/假 note 三条都在这里）
        "application/world/services/foreshadow_pacing.py",
    ],
)
def test_no_unbound_names_in_rewritten_modules(rel_path):
    """跨步骤/重构后残留的未绑定名 ⇒ NameError。

    这类缺陷单测抓不到：旧的纯函数测试全绿，坏的是**调用方**。
    """
    unbound = _unbound_names(rel_path)
    assert not unbound, (
        f"{rel_path} 引用了未绑定名（NameError）：\n  " + "\n  ".join(unbound)
    )


# ── 3. G-03：AI 入参必须真的可达 ──────────────────────────────────────────


def test_make_storyline_accepts_ai_end_and_falls_back_when_absent():
    """**这条就是 `description_ai_end` 事故本身**。

    旧实现用了一个不存在的名字 ⇒ 每建一条新故事线就 NameError，
    且被外层 `except Exception` 吞成 warning，整段落库被跳过。
    """
    from application.world.services import chapter_narrative_sync as cns

    fn = cns._make_storyline_from_progress_item
    params = list(inspect.signature(fn).parameters)
    assert "ai_end" in params, f"缺 ai_end 入参：{params}"
    assert "description_ai_end" not in params

    # AI 给了 → 用 AI 的
    made = fn("n1", 10, "支线", "婚约", "推进中", 42)
    assert made.estimated_chapter_end == 42
    # AI 没给 → 显式兜底（当前章 + span），不是 NameError
    made_none = fn("n1", 10, "支线", "婚约", "推进中")
    assert made_none.estimated_chapter_end > 10


def test_storyline_progress_call_site_passes_ai_end():
    """**G-03 的 AI 入参必须从 progress_item 真的传进去**。

    此前 `resolve_storyline_end` 单元测试全绿（直接喂 ai_end），
    但两个生产调用点一个传空、一个 NameError —— 函数对，没人有数据喂它。
    """
    from application.world.services import chapter_narrative_sync as cns

    src = inspect.getsource(cns)
    assert "progress_item.get(\"estimated_chapter_end\")" in src, (
        "新建故事线时没有把 AI 的 estimated_chapter_end 传进去"
    )


def test_storyline_progress_schema_declares_the_field():
    """提示词不声明该字段 ⇒ 模型不可能给 ⇒ G-03 永远走兜底。"""
    text = _read(
        "infrastructure/ai/prompt_packages/nodes/chapter-narrative-sync/system.md"
    )
    assert "estimated_chapter_end" in text, "schema 里没有 estimated_chapter_end"


# ── 4. D-01：节奏指令必须真的进模型 ──────────────────────────────────────


def test_pacing_directive_reaches_the_preplan_prompt():
    """**这条就是 D-01 失效事故本身**。

    旧实现把指令拼到 `ctx.outline`，而：
      · 步骤 3 用 `ctx.outline = preplan.text` 整体覆盖它
      · `outline` 根本不是 preplan 的 prompt 变量，指令连模型都进不去
    现在改走独立变量 `pacing_directive`。
    """
    from application.blueprint.services.chapter_preplanning_service import (
        ChapterPreplanningService as C,
    )
    from infrastructure.ai.prompt_contracts.continuous_planning import (
        PLANNING_CHAPTER_PREPLAN_CONTRACT as CONTRACT,
    )

    assert "pacing_directive" in PLANNING_CHAPTER_PREPLAN_CONTRACT_vars(), (
        "preplan 契约没有声明 pacing_directive"
    )
    assert "pacing_directive" in list(inspect.signature(C.ensure_execution_plan).parameters), (
        "ensure_execution_plan 没有 pacing_directive 入参"
    )
    tpl = _read(
        "infrastructure/ai/prompt_packages/nodes/planning-chapter-preplan/user.md"
    )
    assert "{pacing_directive}" in tpl, "preplan 模板没有渲染 pacing_directive"


def PLANNING_CHAPTER_PREPLAN_CONTRACT_vars():
    from infrastructure.ai.prompt_contracts.continuous_planning import (
        PLANNING_CHAPTER_PREPLAN_CONTRACT,
    )

    return set(PLANNING_CHAPTER_PREPLAN_CONTRACT.variables_schema.model_fields)


def test_pacing_directive_is_actually_passed_to_the_render_call():
    """**这条堵住上一步的洞**：契约、签名、模板都对了还不够 ——
    ``ensure_execution_plan`` 里那次 ``render(CONTRACT, {...})`` 必须真的带上它。

    否则会出现「schema 声明了变量、方法签名收了参数、模板也渲染了，
    但调用点忘了往字典里放」—— 模型照样看不到指令，而前三项检查全绿。
    """
    import textwrap

    from application.blueprint.services.chapter_preplanning_service import (
        ChapterPreplanningService as C,
    )

    src = textwrap.dedent(inspect.getsource(C.ensure_execution_plan))
    tree = ast.parse(src)
    render_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "render"
    ]
    assert render_calls, "没找到 render 调用"
    # render(CONTRACT, {变量字典}) —— 变量字典是**位置参数**，不是 variables= 关键字
    passed = any(
        "pacing_directive" in ast.unparse(arg) for call in render_calls for arg in call.args
    ) or any(
        "pacing_directive" in ast.unparse(kw.value)
        for call in render_calls for kw in call.keywords
    )
    assert passed, "render 调用里没有把 pacing_directive 放进变量字典"


def test_pacing_directive_is_empty_when_not_a_buffer_chapter():
    """非余韵章时变量必须是空串 —— 模板整段不渲染，「没施加」仍可分辨。"""
    from infrastructure.ai.prompt_contracts.continuous_planning import (
        PLANNING_CHAPTER_PREPLAN_CONTRACT,
    )

    fields = PLANNING_CHAPTER_PREPLAN_CONTRACT.variables_schema.model_fields
    assert fields["pacing_directive"].default == "", (
        "pacing_directive 默认值必须是空串，不能是任何形式的占位文案"
    )


def test_pipeline_context_separates_decision_from_application():
    """`needs_buffer`（AI 判定）与 `buffer_directive`（真的会送进模型）必须分开。

    观察期 apply=false 时前者为 True 而后者为空 —— 这两种状态不能同形。
    """
    from engine.pipeline.context import PipelineContext

    fields = PipelineContext.__dataclass_fields__
    assert "needs_buffer" in fields
    assert "buffer_directive" in fields
    assert PipelineContext().buffer_directive == ""
    assert PipelineContext().needs_buffer is False


# ── 5. D-02：slot_weights 必须在 workflow 主路径转发 ──────────────────────


def test_workflow_main_path_forwards_slot_weights():
    """**这条就是 D-02 静默失效事故本身**。

    生产 daemon 恒注入 `chapter_workflow`，走的是 `prepare_chapter_generation`
    主路径；此前该路径调 `build_structured_context` 时不转发 `slot_weights`，
    只有降级路径转发 ⇒ 算好的权重算完即丢，而日志仍打「已施加」。
    """
    from application.workflows.auto_novel_generation_workflow import (
        AutoNovelGenerationWorkflow as W,
    )

    src = inspect.getsource(W.prepare_chapter_generation)
    call_sites = re_findall_build_calls(src)
    assert call_sites, "没找到 build_structured_context 调用点"
    assert all("slot_weights" in body for body in call_sites), (
        "主路径存在不转发 slot_weights 的 build_structured_context 调用："
        + repr(call_sites)
    )


def re_findall_build_calls(src: str) -> list[str]:
    """把源码里每个 `self.context_builder.build_structured_context(...)` 抠出来。"""
    tree = ast.parse(_dedent_to_module(src))
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "build_structured_context"):
            out.append(ast.unparse(node))
    return out


def _dedent_to_module(src: str) -> str:
    import textwrap

    return textwrap.dedent(src)


def test_slot_weights_log_does_not_claim_success():
    """日志不能宣称「已施加」—— 本函数看不到下游有没有转发。"""
    src = _read("engine/pipeline/base.py")
    assert "槽位权重已施加" not in src, (
        "日志在权重真正落地之前就宣称已施加 —— 正是本轮红线「假成功」"
    )


# ── 6. CI：迁移测试必须 hermetic ──────────────────────────────────────────


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """收集所有 docstring 常量节点的 id。"""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


def _non_docstring_constants(rel_path: str) -> list[ast.Constant]:
    """所有**非 docstring** 的字符串字面量。

    为什么不能靠 ``src = read(); assert "kw" not in src``：
    那会把说明文字里的关键字也算进去 —— 注释里提一句就足以让断言空转。
    （审查建议 8 批评的正是这种「源码文本断言」。）
    """
    tree = _module_ast(rel_path)
    docs = _docstring_nodes(tree)
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docs
    ]


def test_migration_test_does_not_depend_on_local_data_dir():
    """`data/` 被 .gitignore 排除，干净 checkout 上不存在。

    旧实现的守卫写成了 `if not prod:`（`prod` 是非空字面量，恒假），
    `pytest.skip` 永远不可达 ⇒ CI 上必然 FileNotFoundError。
    """
    rel = ("tests/unit/infrastructure/persistence/database/"
           "test_macro_diagnosis_migration_order.py")

    offenders = [
        (n.lineno, n.value[:60])
        for n in _non_docstring_constants(rel)
        if "plotpilot.db" in n.value
    ]
    assert not offenders, f"代码里仍硬编码生产库路径：{offenders}"

    copies = [
        n.lineno for n in ast.walk(_module_ast(rel))
        if isinstance(n, ast.Attribute) and n.attr == "copy2"
    ]
    assert not copies, f"仍靠拷贝生产库构造夹具：L{copies}"


def test_migration_test_resolves_paths_without_cwd_fallback():
    """路径解析要向上搜索，不能靠 cwd 兜底 —— 那是隐性运行目录依赖。

    旧写法 `Path(__file__).resolve().parents[4]` 指向 `tests/` 而非仓库根，
    只因后面跟着 cwd 兜底才没暴露。
    """
    tree = _module_ast(
        "tests/unit/infrastructure/persistence/database/"
        "test_macro_diagnosis_migration_order.py"
    )
    for node in ast.walk(tree):
        # os.getcwd(...) 调用
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "getcwd":
            pytest.fail(f"L{node.lineno} 用了 os.getcwd() 兜底 —— 运行目录依赖")
        # parents[<字面量整数>] 形式
        if (isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, int)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "parents"):
            pytest.fail(
                f"L{node.lineno} 用 parents[{node.slice.value}] 定位仓库根 —— "
                f"层级极易算错，应改为向上搜索"
            )


# ── 7. novel 作用域 ──────────────────────────────────────────────────────


def _novel_none_preset_nodes(fn) -> list[ast.Assign]:
    """`_step_find_next_chapter` 里 `novel = None`（字面量 None）的赋值节点。"""
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "novel" for t in node.targets):
            continue
        if isinstance(node.value, ast.Constant) and node.value.value is None:
            out.append(node)
    return out


def test_novel_preset_protects_the_decide_chapter_mode_call():
    """不变式：**预置的作用域必须覆盖 `_decide_chapter_mode(ctx, novel)` 的作用域**。

    事故形态是「预置（或赋值）在某个 try 的 body 内，而调用在 try 外」——
    try 抛错走 except，变量没绑上，调用处 UnboundLocalError。

    单纯要求「预置不在 try 内」是错的：整个代码块本来就常被一个外层 try 包着，
    只要调用也在同一个 try 内就是安全的。所以判据是**支配关系**：

        对每个 try T：若预置 ∈ T.body，则必须 也有 调用 ∈ T.body

    这样既挡住事故，又不会把合法的外层 try 误判成缺陷。
    """
    tree = _module_ast("engine/pipeline/base.py")
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_step_find_next_chapter"
    )
    presets = _novel_none_preset_nodes(fn)
    assert presets, "`novel` 没有预置为 None —— get_by_id 抛错时 UnboundLocalError"
    preset_lines = {p.lineno for p in presets}

    call_lines = {
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_decide_chapter_mode"
    }
    assert call_lines, "没找到 _decide_chapter_mode 调用"

    for tr in [n for n in ast.walk(fn) if isinstance(n, ast.Try)]:
        span = range(tr.lineno, (tr.end_lineno or tr.lineno) + 1)
        preset_in = bool(preset_lines & set(span))
        call_in = bool(call_lines & set(span))
        assert not (preset_in and not call_in), (
            f"try(L{tr.lineno}) 包住了 `novel = None` 却没包住 "
            f"_decide_chapter_mode —— 该 try 抛错时调用处仍会 UnboundLocalError"
        )
    assert min(preset_lines) < min(call_lines), (
        f"`novel = None` ({sorted(preset_lines)}) 必须早于调用 ({sorted(call_lines)})"
    )


# ── 8. 编码：不允许双重编码残留 ──────────────────────────────────────────


def test_no_double_encoding_in_touched_files():
    """双重编码的特征：Latin-1 补充区字符成串 + C1 控制字符。

    A-02 曾把 21 行写成乱码，其中用户可见的 ``ValueError`` 文案变成
    「必颜是数组」，三个中文别名键永久失配。
    """
    touched = [
        "application/blueprint/services/setup_plot_outline_continuation.py",
        "application/blueprint/services/stage_plan_invariants.py",
        "application/engine/services/context_weight_catalog.py",
        "application/engine/services/chapter_mode_planner.py",
    ]
    for rel in touched:
        text = _read(rel)
        for i, line in enumerate(text.splitlines(), 1):
            latin1 = sum(1 for ch in line if 0x80 <= ord(ch) <= 0xFF)
            ctrl = [c for c in line if ord(c) < 0x20 and c != "\t"]
            assert latin1 < 4, f"{rel}:{i} 疑似双重编码：{line.strip()[:70]}"
            assert not ctrl, f"{rel}:{i} 含控制字符：{line.strip()[:70]!r}"


def test_stage_plan_error_message_is_readable_chinese():
    """用户可见文案曾因编码损坏变成「必颜是数组」。"""
    src = _read("application/blueprint/services/setup_plot_outline_continuation.py")
    assert "必须是数组" in src, "stage_plan 的类型错误文案损坏"
    assert "必颜" not in src


# ── 9. bundle 版本 ────────────────────────────────────────────────────────


def test_bundle_changelog_covers_current_version():
    """changelog 必须有当前版本的条目 —— 否则升级日志对使用者不可见。"""
    import re as _re

    text = _read("infrastructure/ai/prompt_packages/bundle_meta.yaml")
    version = _re.search(r"^version:\s*([0-9]+)\.([0-9]+)\.([0-9]+)", text, _re.M)
    assert version, "读不出 bundle 版本号"
    tag = f"v{version.group(1)}.{version.group(2)}.{version.group(3)}"
    assert f"{tag}:" in text, f"changelog 缺 {tag} 条目"

# ── unbound 探针自身的正向验证 ───────────────────────────────────────────
#
# ⚠ 上一版 commit message 写「补 5 组已知用例正向验证」，**树里实际只有 1 条
# 零件级自测**（只验 `_scope_local_names`，不验 `_unbound_names`）。第四轮复审
# 抓到了这个过度声称。本节把承诺兑现：5 组正负样本，覆盖探针改过的每一条语义。
#
# 为什么必须有：探针返回 [] 时，「没找到问题」与「探针坏了」在断言里长得一模一样。
# 第四轮我就把探针改成过完全失效（只 yield 语句节点却去找 ast.Name 叶子），
# 而 `assert not unbound` 依然全绿。


def _probe_run(source: str) -> list[str]:
    """把源码喂给**真实**探针实现，返回问题列表。

    ⚠ 这里刻意调用 `_unbound_in_tree` 而不是复刻一份逻辑。
    第四轮复审抓到：我最初的版本在自测里**重写了一份同样的扫描逻辑**，
    于是真实探针退化时自测全绿 —— 替身与真实脱钩，
    自测测的是替身，不是被测对象。变异 S6 就是这么漏过去的。
    """
    return _unbound_in_tree(ast.parse(source))


def test_probe_catches_a_plain_unbound_name():
    """正样本：函数体里直接引用未定义名。"""
    problems = _probe_run("def f():\n    return undefined_name + 1\n")
    assert any("undefined_name" in p for p in problems), (
        f"探针对最基础的未绑定名都没报：{problems}"
    )


def test_probe_does_not_flag_params_or_nested_def():
    """负样本：形参、嵌套 def 的局部、嵌套 def 的名字，都不算未绑定。"""
    problems = _probe_run(
        "def outer(a):\n"
        "    def inner(b):\n"
        "        local = b + a\n"
        "        return local\n"
        "    return inner(a)\n"
    )
    assert problems == [], f"误报：{problems}"


def test_probe_still_descends_into_lambda_bodies():
    """lambda 体在外层作用域求值 ⇒ 里面的未绑定名必须被抓到；形参则不算。"""
    problems = _probe_run(
        "def outer():\n"
        "    return sorted([1], key=lambda captured: missing_global)\n"
    )
    assert any("missing_global" in p for p in problems), (
        f"没抓到 lambda 体里的未绑定名：{problems}"
    )

    ok = _probe_run(
        "def outer():\n"
        "    return sorted([1], key=lambda item: item)\n"
    )
    assert ok == [], f"lambda 形参被误报：{ok}"


def test_probe_catches_module_level_unbound():
    """模块级（顶层代码）也要扫 —— 第三版曾把它整个丢掉。"""
    problems = _probe_run("value = missing_global\n")
    assert any("missing_global" in p for p in problems), (
        f"模块级未绑定名没被抓到：{problems}"
    )


def test_probe_would_notice_if_it_went_inert():
    """哨兵：探针一旦退化成恒返回 []，这条先红。

    它守的是「探针还活着」，不是「探针的结论」。
    第四轮探针曾完全失效而 `assert not unbound` 依然全绿。
    """
    problems = _probe_run(
        "def f():\n"
        "    return undefined_name + other_missing\n"
        "x = undefined_name\n"
    )
    assert len(problems) >= 3, (
        f"探针只报了 {len(problems)} 处，期望 >=3 —— 它可能已失效：{problems}"
    )


def test_probe_handles_module_level_lambda_and_comprehension():
    """负样本：模块级 lambda 形参与推导式目标**不是**未绑定名。

    第五轮复审实证的**新误报**：模块级扫描的可见集合没算上模块作用域，
    于是 `f = lambda item: item` 与 `X=[i for i in range(3)]` 被报未绑定。
    根因是 `_check` 的上溯链不认 ast.Module，模块作用域的绑定永远算不进来。
    """
    assert _probe_run("f = lambda item: item\n") == [], "模块级 lambda 形参被误报"
    assert _probe_run("X = [i for i in range(3)]\n") == [], "模块级推导式变量被误报"


def test_probe_catches_class_bodies_and_bases():
    """正样本：类体与基类列表在外层作用域求值，未绑定名该报。

    第五轮复审实证的**漏报**：`class C: attr = missing` 与
    `class C(MissingBase)` 都返回 []。类体不是闭包，它**能**看见外层名字；
    只有方法体才有自己���作用域。
    """
    p1 = _probe_run("class C:\n    attr = missing\n")
    assert any("missing" in x for x in p1), f"类体未绑定名漏报：{p1}"

    p2 = _probe_run("class C(MissingBase):\n    pass\n")
    assert any("MissingBase" in x for x in p2), f"类基类未绑定名漏报：{p2}"

    p3 = _probe_run("def f():\n    class D:\n        attr = missing\n    return D\n")
    assert any("missing" in x for x in p3), f"函数内嵌套类体漏报：{p3}"


def test_probe_does_not_treat_method_params_as_outer_bindings():
    """负样本：方法体有自己的作用域 —— 其形参/局部不该算进类体外层。

    防的是「修类体漏报时矫枉过正」：把方法里的东西误报成外层未绑定。
    """
    problems = _probe_run(
        "class C:\n"
        "    def method(self, value):\n"
        "        local = value + 1\n"
        "        return local\n"
    )
    assert problems == [], f"方法体作用域被误算到外层：{problems}"


def test_probe_does_not_leak_class_body_store_to_outer_scope():
    """⚠ 第六轮复审 §3.2：为扫类体而把类体 Store 并入外层 local，造成 3 类**新漏报**。

    类作用域不是闭包：`class C: attr = 1` 之后，`def f(): return attr`
    在运行期就是 NameError。判 Load 要进类体，收绑定必须停在类体 —— 两件事。
    """
    for src, want in [
        ("def outer():\n    class C:\n        attr = 1\n    return attr\n", "attr"),
        ("class C:\n    leak = 1\ndef f():\n    return leak\n", "leak"),
        ("class C:\n    attr = 1\n    def m(self):\n        return attr\n", "attr"),
    ]:
        problems = _probe_run(src)
        assert any(want in p for p in problems), (
            f"类体赋值泄漏成外层绑定，漏报了 {want!r}：{problems}"
        )


def test_probe_scans_signature_parts():
    """⚠ 第六轮复审 §3.3：decorator / defaults / annotations / 返回注解此前完全没扫。

    它们在**定义它的那一层作用域**求值（不是函数体里），所以「跳过函数体」
    时必须显式把它们留下，否则 `@missing_deco`、`x=missing_default`、
    `x: MissingType`、`-> MissingRet` 全都漏报。
    """
    cases = [
        ("@missing_fn_deco\ndef g():\n    pass\n", "missing_fn_deco"),
        ("def g(x=missing_default):\n    pass\n", "missing_default"),
        ("class C:\n    @missing_deco\n    def m(self):\n        pass\n", "missing_deco"),
        ("class C:\n    def m(self, x=missing_default):\n        pass\n", "missing_default"),
        ("class C:\n    def m(self, x: MissingType):\n        pass\n", "MissingType"),
        ("class C:\n    def m(self) -> MissingRet:\n        pass\n", "MissingRet"),
    ]
    for src, want in cases:
        problems = _probe_run(src)
        assert any(want in p for p in problems), f"签名部件漏报 {want!r}：{problems}"


def test_class_body_forward_reference_is_reported():
    """⚠ 第九轮复审：类体是**顺序执行**的，`B = A; A = 1` 运行期是 NameError。

    原实现用整段类体的绑定快照，`B` 那行就看见了后面才定义的 `A` ⇒ 漏报。
    修法：逐语句 enter/leave，每条语句只看见它**之前**已定义的名字。
    """
    problems = _probe_run("class C:\n    B = A\n    A = 1\n")
    assert any("'A'" in p for p in problems), (
        f"类体前向引用漏报（运行期 NameError）：{problems}"
    )


def test_class_scope_does_not_leak_to_neighbouring_statements():
    """⚠ 第九轮复审：类作用域的名字只对**类体子树**可见。

    类定义之前 / 之后的同级语句都看不见它 ——
    `use = attr` 在 `class C: attr = 1` 之前，运行期就是 NameError。
    """
    before_fn = "def outer():\n    use = attr\n    class C:\n        attr = 1\n    return use\n"
    after_fn = "def outer():\n    class C:\n        attr = 1\n    return attr\n"
    module_level = "use = attr\nclass C:\n    attr = 1\n"
    for src, label in ((before_fn, "类定义之前"), (after_fn, "类定义之后"),
                       (module_level, "模块级")):
        problems = _probe_run(src)
        assert any("'attr'" in p for p in problems), (
            f"{label}的语句不应看见类属性：{problems}"
        )


def test_class_level_lambda_params_are_bound():
    """⚠ 第九轮复审 §3.2：类体语句里嵌的 lambda 形参绑定在**类作用域**。

    `class C: dec = staticmethod(lambda f: f)` 的 f 有定义，不是未绑定名。
    """
    for src in ("class C:\n    dec = staticmethod(lambda f: f)\n",
                "class C:\n    fn = lambda z: z + 1\n",
                "class C:\n    def m(self, r=lambda s: s):\n        return r\n"):
        problems = _probe_run(src)
        assert not problems, f"类级 lambda 形参被误报：{src!r} ⇒ {problems}"


def test_method_local_is_not_visible_to_the_class_body():
    """⚠ 第九轮复审 §3.1 的**真正形态**。

    报告给的样例 `class C: def m(self): hidden = 1; B = hidden` 其实**不是漏报** ——
    B=hidden 在方法体内，hidden 是该方法的局部，Python 运行期合法，探针返回 [] 是对的。

    真正的漏报形态是：B 在**类体层**、hidden 在**方法体层** ——
    方法局部对类作用域不可见，`class C: B = hidden; def m(self): hidden = 1`
    在运行期是 NameError，必须报出。
    """
    problems = _probe_run(
        "class C:\n    B = hidden\n    def m(self):\n        hidden = 1\n")
    assert any("'hidden'" in p for p in problems), (
        f"方法局部被当成类属性放行：{problems}"
    )


def test_class_body_traversal_is_source_ordered():
    """⚠ 第九轮复审：类体子树必须**整体按源码顺序**产出。

    原实现把类体语句的子节点塞进外层 LIFO 栈，它们的**后代**
    （Assign→Call→Lambda→Name）会在 leave 标记**之后**才被弹出，
    于是 `dec = staticmethod(lambda f: f)` 的 f 落在类作用域之外 ⇒ 误报。

    这条直接断 `_load_nodes` 的**产出顺序**：leave 标记必须排在
    该类全部后代节点**之后**。
    """
    tree = ast.parse("class C:\n    dec = staticmethod(lambda f: f)\n")
    nodes = list(_load_nodes(tree))
    enter_idx = next(i for i, n in enumerate(nodes)
                     if isinstance(n, _ClassScopeMarker) and n.enter)
    leave_idx = next(i for i, n in enumerate(nodes)
                     if isinstance(n, _ClassScopeMarker) and not n.enter)
    assert enter_idx < leave_idx, "enter 必须先于 leave"
    # f 的 Load 节点必须在 [enter, leave] 区间内
    f_load = next(i for i, n in enumerate(nodes)
                  if isinstance(n, ast.Name) and n.id == "f"
                  and isinstance(n.ctx, ast.Load))
    assert enter_idx < f_load < leave_idx, (
        f"lambda 形参的 Load (idx={f_load}) 落在类作用域区间 "
        f"[{enter_idx}, {leave_idx}] 之外"
    )


def test_probe_descends_into_class_header_expressions():
    """⚠ 第十轮复审：**本笔新引入的漏报**。

    ClassDef 分支原先对 bases/keywords/decorator_list 只 ``yield expr``
    产出根节点、不再下降 ⇒ 子树里的未绑定名全部看不见：
      · `class C(undefined_mod.Base)`
      · `class C(metaclass=undefined_mod.Meta)`
      · `@undefined_mod.deco class C`
      · `def m(self, x: undefined_mod.T)`
      · `def m(self, x=missing_factory())`
    而函数级的同类写法仍能报 —— 说明只有类/方法分支受影响。
    """
    cases = [
        ("class C(undefined_mod.Base):\n    pass\n", "undefined_mod"),
        ("class C(metaclass=undefined_mod.Meta):\n    pass\n", "undefined_mod"),
        ("@undefined_mod.deco\nclass C:\n    pass\n", "undefined_mod"),
        ("class C:\n    def m(self, x: undefined_mod.T):\n        return x\n",
         "undefined_mod"),
        ("class C:\n    def m(self, x=missing_factory()):\n        return x\n",
         "missing_factory"),
    ]
    for src, want in cases:
        problems = _probe_run(src)
        assert any(want in p for p in problems), f"类头部表达式漏报 {want!r}：{problems}"


def test_nested_class_has_its_own_scope():
    """⚠ 第十轮复审：嵌套类不该继承外层类作用域，反之亦然。

    `class A: ax = 1` 后 `class B: bz = ax` 在运行期是 NameError
    （类作用域不是闭包）；而 `class A: class B: x=1; y=x` 里的 y
    看得见 B 自己的 x，不该被外层作用域影响。
    """
    ok = "class A:\n    class B:\n        x = 1\n        y = x\n"
    assert not _probe_run(ok), f"嵌套类内自引用被误报：{_probe_run(ok)}"

    bad = "class A:\n    ax = 1\nclass B:\n    bz = ax\n"
    problems = _probe_run(bad)
    assert any("'ax'" in p for p in problems), (
        f"嵌套类不该看见兄弟类的属性：{problems}"
    )


def test_method_signature_cannot_forward_reference_a_later_class_attr():
    """⚠ 第十轮复审：方法签名部件的可见集只能用**之前**已定义的类属性。

    `def m(self, x=later)` 执行时 `later` 尚未绑定 ⇒ 运行期 NameError。
    原实现用整段 class_local 快照，把后定义的 later 放行了。
    """
    src = "class C:\n    def m(self, x=later):\n        return x\n    later = 1\n"
    problems = _probe_run(src)
    assert any("'later'" in p for p in problems), f"方法签名前向引用漏报：{problems}"


def test_compound_statements_inside_a_class_body():
    """⚠ 第十一轮复审：复合语句（if/for/while/try/with/match）的分支
    与类体**同属一个作用域** —— `if` 不引入新作用域。

    原先它们落到 `ast.walk(stmt)` 的无差别下降分支，于是
      ``class C: if True: B = hidden; def m(self): hidden = 1``
    里 B（类体层）看得见 hidden（方法**体**的局部）⇒ 漏报。
    Python 里这两层不可互相穿透。

    ⚠ 判据必须锁定**报出的是哪个名字**（``assert problems`` 太松）：
    我第一版写的就是那样，把整条分支禁掉后它仍全绿 —— 因为
    for 目标的 `_i` 那条会报另一个名字，凑巧让 ``assert problems`` 成立。

    ⚠ 已知的**不可观测变异**（双重防线，写下来免得后人重复踩）：
    「关掉 `_load_nodes` 主循环里的复合语句分支」测不出来 ——
    `_scope_local_names_of_body` 已独立算出正确的可见集
    （实测返回 ``{'B', 'm'}``），两处任一失效另一处仍生效。
    能转红的是关掉 `_scope_local_names_of_body` 里那一处（V4b）。
    """
    must_report = [
        # 报出的必须是 hidden（方法**体**的局部，类体层看不见）
        ("class C:\n    if True:\n        B = hidden\n        def m(self):\n"
         "            hidden = 1\n", "hidden"),
        ("class C:\n    for _i in (1,):\n        B = missing\n", "missing"),
        ("class C:\n    while cond:\n        B = missing2\n", "missing2"),
        ("class C:\n    try:\n        A = 1\n    except MissingExc:\n"
         "        pass\n", "MissingExc"),
    ]
    for src, want in must_report:
        problems = _probe_run(src)
        assert any(want in p for p in problems), (
            f"复合语句内应报出 {want!r}：{src!r} ⇒ {problems}"
        )

    must_not = [
        # 类属性在分支内外都可见
        "class C:\n    A = 1\n    if True:\n        B = A\n",
        # 分支内先定义后引用
        "class C:\n    if True:\n        A = 1\n        B = A\n",
        # try/except 同理
        "class C:\n    try:\n        A = 1\n        B = A\n    except Exception:\n        C = B\n",
        # ⚠ for 目标的绑定在本作用域可见（这一条是 for 循环的正常语义）
        "class C:\n    for _i in (1,):\n        B = _i\n",
    ]
    for src in must_not:
        problems = _probe_run(src)
        assert not problems, f"复合语句内合法写法被误报：{src!r} ⇒ {problems}"


def test_nested_class_header_sees_the_enclosing_class_scope():
    """⚠ 第十一轮复审：嵌套类的**头**与**体**可见性不同。

    `class A: Base = object` 后 `class B(Base)` 合法 —— 基类名在**外层**求值。
    但 `class A: ax = 1` 后 `class B: bz = ax` 在运行期是 NameError ——
    类作用域不是闭包，类体看不见外层类属性。
    """
    ok = [
        "class A:\n    Base = object\n    class B(Base):\n        pass\n",
        "class A:\n    class B:\n        x = 1\n        y = x\n",
        # 两层嵌套：头要继承**本类**此前定义的属性
        "class A:\n    class B:\n        C = object\n        class D(C):\n            pass\n",
    ]
    for src in ok:
        problems = _probe_run(src)
        assert not problems, f"嵌套类合法写法被误报：{src!r} ⇒ {problems}"

    bad = [
        ("class A:\n    class B(Missing):\n        pass\n", "Missing"),
        ("class A:\n    ax = 1\nclass B:\n    bz = ax\n", "ax"),
        ("class A:\n    ax = 1\n    class B:\n        bz = ax\n", "ax"),
        ("class A:\n    class B:\n        C = 1\n        class D(Missing):\n            pass\n",
         "Missing"),
    ]
    for src, want in bad:
        problems = _probe_run(src)
        assert any(want in p for p in problems), (
            f"嵌套类漏报 {want!r}：{src!r} ⇒ {problems}"
        )


def test_compound_statement_ordering_semantics():
    """⚠ 第十二轮复审（顺序语义）：条件与头部只能用**前置**可见名。

    原先把整条语句的绑定并进 `seen` 再扫头部，于是条件提前看见
    **后面才定义**的名字 ⇒ 4 类漏报（运行期都是 NameError）。
    """
    must_report = [
        ("class C:\n    if later:\n        later = 1\n", "later"),
        ("class C:\n    for x in xs:\n        xs = []\n", "xs"),
        ("class C:\n    while cond:\n        cond = False\n", "cond"),
        # if/else 互斥：else 里看不见 if 分支定义的 A
        ("class C:\n    if cond:\n        A = 1\n    else:\n        B = A\n", "A"),
    ]
    for src, want in must_report:
        problems = _probe_run(src)
        assert any(want in p for p in problems), (
            f"顺序语义漏报 {want!r}：{src!r} ⇒ {problems}"
        )


def test_sequential_blocks_share_their_scope():
    """⚠ 顺序执行的块**共享**一个作用域，与 if/else 互斥分支不同。

    `try: A=1; B=A / except: C=B` 里 B 在 except 体可见 ——
    上一版把 if/else 的「分支互斥」语义套到 try 上 ⇒ 误报。

    ⚠ 关于「反转 exclusive」的变异（第十三轮复审更正了我上一轮的说法）：
    真正承重的是 **`_compound_sub_bodies` 返回的 `exclusive` 标志本身** ——
    反转它（if/else 互斥 → 顺序，或 try 顺序 → 互斥）两个方向都 RED。

    ⚠ 我上一轮说的「两方向不可观测」是**错的**：那次变异打的是下游
    ``sub_seen = (set(prefix) if _exclusive else ...)`` 那两处**表达式**，
    而那两处即使语义反了，输出也不变（``sub_seen`` 的差别只影响分支体起点，
    分支内 ``B = A`` 的 A 在该分支自己的 seen 里就已判定，
    跑完后 ``branch_defs`` 又无条件并回 seen ⇒ 差异被抹平）。
    ⇒ **变异打错位置 ≠ 变异不成立**。这次是审查员反转标志本身才发现的。
    """
    ok = [
        "class C:\n    try:\n        A = 1\n        B = A\n    except Exception:\n        C = B\n",
        "class C:\n    for _i in (1,):\n        B = _i\n",
        # ⚠ 上下文表达式里用已绑定的名字（`f` 若是未定义，报出来才是对的）
        "class C:\n    f = 'p'\n    with open(f) as fh:\n        B = fh\n",
        # for 的 orelse 与 body 共享作用域（循环跑完 _i 已绑定）
        "class C:\n    for _i in (1,):\n        B = _i\n    else:\n        C = _i\n",
    ]
    for src in ok:
        problems = _probe_run(src)
        assert not problems, f"顺序块内合法写法被误报：{src!r} ⇒ {problems}"


def test_match_case_captures_and_pattern_classes():
    """⚠ 第十二轮复审：`match` 有**两种**可见性，不能混为一谈。

      · 捕获名（``case [a, b]:`` 的 a/b）—— 运行期在进入 case 体前已绑定
      · 模式类名（``case MissingPoint(p)`` 的 MissingPoint）—— 必须在外层可见
    """
    ok = "class C:\n    match v:\n        case [a, b]:\n            B = a\n"
    problems = _probe_run(ok)
    assert not any("'a'" in p or "'b'" in p for p in problems), (
        f"match 捕获名被误报：{problems}"
    )

    bad = "class C:\n    match v:\n        case MissingPoint(p):\n            B = 1\n"
    problems = _probe_run(bad)
    assert any("MissingPoint" in p for p in problems), (
        f"模式类名漏报：{bad!r} ⇒ {problems}"
    )


def test_nested_class_method_body_is_not_class_scope():
    """⚠ 第十二轮复审（§3.1-4，第十轮遗留）：嵌套类里的方法体另有作用域。

    `class A: class B: def m(self): z = 1; return z` —— z 是方法的局部，
    报未绑定是假红。
    """
    src = "class A:\n    class B:\n        def m(self):\n            z = 1\n            return z\n"
    problems = _probe_run(src)
    assert not problems, f"嵌套类方法体被误报：{problems}"


def test_probe_never_crashes_on_class_bodies_with_elif_or_nested_compounds():
    """⚠ 第十三轮复审【需修】：本轮重构引入的**崩溃回归**。

    `_compound_sub_bodies` 的返回值从 list 改成 `(bodies, exclusive)` 元组，
    `_scope_local_names_of_body` 里那处调用**忘了解包** ⇒
    ``inner`` 变成 ``(list, bool)``，被当语句体递归时抛
    ``AttributeError: 'list' object has no attribute '_fields'``。

    触发条件：类体内任何 `elif` 或嵌套复合语句 ——
    `class C: if True: A=1 / elif False: B=2`、if-in-if、for-in-if、try-in-if。
    `elif` 是极常见写法，只是当前 8 个守卫文件恰好没触发。

    ⚠ 这条守卫的形态是「**不许抛异常**」而不是「报出某物」——
    崩溃与误报是两种不同的失败，前者必须单独锁住。

    ⚠ 第十四轮复审指出：只断言「不抛」的话，一个**恒返回 [] 的探针**
    也能通过（探针彻底失灵时不会崩）。所以这里补一组「应当有输出」的
    对照样本 —— 同一批形态里放一个**真未绑定名**，必须被报出。
    这样「探针不崩」与「探针还在干活」两件事都被锁住。
    """
    cases = [
        ("elif", "class C:\n    if True:\n        A = 1\n    elif False:\n        B = 2\n"),
        ("if-in-if", "class C:\n    if True:\n        if True:\n            A = 1\n"),
        ("for-in-if", "class C:\n    for _i in (1,):\n        if True:\n            A = 1\n"),
        ("try-in-if", "class C:\n    if True:\n        try:\n            A = 1\n"
                     "        except Exception:\n            pass\n"),
        ("elif-in-method",
         "class C:\n    def m(self):\n        if True:\n            A = 1\n"
         "        elif False:\n            B = 2\n"),
        # 复合分支内的 match（第十四轮复审：这类曾误报捕获名）
        ("match-in-if", "class C:\n    v=[1]\n    if True:\n        match v:\n"
                        "            case [a]:\n                B=a\n"),
    ]
    for label, src in cases:
        try:
            _probe_run(src)
        except Exception as e:
            raise AssertionError(
                f"探针在「{label}」上崩溃：{type(e).__name__}: {e}\n{src!r}"
            ) from e

    # ⚠ 对照组：同样形态里放一个**真未绑定名**，必须被报出 ——
    # 否则「探针彻底失灵、恒返回 []」也能通过上面的 no-crash 断言。
    probes = [
        ("elif", "class C:\n    if True:\n        A = missing1\n"
                 "    elif False:\n        B = 2\n", "missing1"),
        ("if-in-if", "class C:\n    if True:\n        if True:\n            A = missing2\n",
         "missing2"),
        ("for-in-if", "class C:\n    for _i in (1,):\n        if True:\n"
                      "            A = missing3\n", "missing3"),
        ("try-in-if", "class C:\n    if True:\n        try:\n            A = missing4\n"
                      "        except Exception:\n            pass\n", "missing4"),
    ]
    for label, src, want in probes:
        problems = _probe_run(src)
        assert any(want in p for p in problems), (
            f"探针在「{label}」上没报出 {want!r} —— 探针可能已失灵：{problems}"
        )


def test_match_subject_guard_and_mapping_captures():
    """⚠ 第十三轮复审：match 的 4 类残留。

      · subject 被产出两次 + 没有类作用域 marker ⇒ `v` 被报两次未绑定
      · guard 表达式在 case 体之前求值，但捕获名**已**绑定
        （`case [a, b] if a > 0:` 的 a 在 guard 里可用）
      · `case {'k': val, **rest}` 的 rest 是捕获名
      · 函数/模块级 match 也要建模（此前只在类体路径）
    """
    must_not = {
        "subject 已定义": "class C:\n    v = [1]\n    match v:\n        case [a]:\n            B = a\n",
        "guard 用捕获名": ("class C:\n    v = [1]\n    match v:\n"
                           "        case [a, b] if a > 0:\n            B = b\n"),
        "mapping rest": ("class C:\n    v = {}\n    match v:\n"
                         "        case {'k': val, **rest}:\n            B = rest\n"),
        "函数级捕获名": ("def f():\n    v = [1]\n    match v:\n"
                         "        case [a]:\n            return a\n"),
    }
    for label, src in must_not.items():
        problems = _probe_run(src)
        assert not problems, f"{label} 被误报：{src!r} ⇒ {problems}"

    # ⚠ 已知的**不可观测变异**（写下来免得后人重复踩）：
    # 「把捕获名并回 `_scope_local_names_of_body` 的外层 names」测不出来 ——
    # `_match_scope_nodes` 内部自己算 `body_names = outer | case_names`，
    # 不依赖调用方传入的 seen ⇒ 两处是**双重防线**，输出完全相同。
    # 「`_match_pattern_loads` 里加回那句 `[kw for kw in node.kwd_attrs if False]`」
    # 同样测不出来 —— `kwd_attrs` 是字符串列表，`if False` 恒为空，纯死代码。
    # 这两条都不是空洞测试，而是**变异本身不成立**。


def test_match_inside_class_body_compound_branches():
    """⚠ 第十四轮复审：类体复合分支**内层**的 match 也需要捕获名建模。

    inner 循环原先只特判 FunctionDef/ClassDef，Match 走 `_ordered_subtree`
    铺平下降 ⇒ `class C: if True: match v: case [a]: B=a` 的 a 被误报。
    （函数/模块级 match-in-if 一直正常，因为通用路径已有 Match 分支。）
    """
    must_not = {
        "if 内": "class C:\n    v=[1]\n    if True:\n        match v:\n"
                 "            case [a]:\n                B=a\n",
        "for 内": "class C:\n    v=[1]\n    for _i in (1,):\n        match v:\n"
                  "            case [a]:\n                B=a\n",
        "while 内": "class C:\n    v=[1]\n    while True:\n        match v:\n"
                    "            case [a]:\n                B=a\n",
        "try 内": "class C:\n    v=[1]\n    try:\n        match v:\n"
                  "            case [a]:\n                B=a\n    except Exception:\n        pass\n",
    }
    for label, src in must_not.items():
        problems = _probe_run(src)
        assert not problems, f"复合分支内 match 的捕获名被误报：{label} ⇒ {problems}"


def test_match_mapping_keys_are_expressions_not_captures():
    """⚠ 第十四轮复审：mapping 的**键**是表达式，在外层作用域求值。

    `case {Keys.KEY: val}` 的 `Keys.KEY` 未定义必须报出；
    字面键 `case {'k': val}` 的 'k' 是字符串常量，不产生 Load。
    （与 MatchClass 的 `kwd_attrs` 不同 —— 那是字符串属性名。）
    """
    problems = _probe_run(
        "class C:\n    match v:\n        case {Keys.KEY: val}:\n            B=1\n")
    assert any("'Keys'" in p for p in problems), (
        f"mapping 键未报出：{problems}"
    )

    problems = _probe_run(
        "class C:\n    match v:\n        case {'k': val}:\n            B=1\n")
    assert not any("'k'" in p for p in problems), (
        f"字面键被误报成未绑定名：{problems}"
    )


def test_after_match_capture_name_is_a_known_conservative_gap():
    """⚠ 第十四轮复审：match 之后的捕获名引用是**已知的保守近似**。

    `match v: case [a]: X=1` 之后的 `B=a` 会被报未绑定 ——
    而运行期若该 case 命中，a 是有绑定的。
    这里选择保守（宁可多报），因为「该 case 是否命中」是运行期数据。

    这条测试**锁定当前行为**，让将来若有人改动它会立刻看见，
    而不是当成 bug 顺手"修掉"（那会与跨 case 保守方向冲突）。
    """
    problems = _probe_run(
        "class C:\n    v=[1]\n    match v:\n        case [a]:\n            X=1\n    B=a\n")
    assert any("'a'" in p for p in problems), (
        f"after-match 捕获名的保守行为变了（若是有意修改，请更新本测试"
        f"与 `_match_scope_nodes` 的注释）：{problems}"
    )


def test_compound_sub_bodies_marks_exclusive_branches_correctly():
    """⚠ 第十三轮复审：`_compound_sub_bodies` 的 `exclusive` 标志是**承重点**。

    反转它（if/else 互斥 → 顺序，或 try 顺序 → 互斥）两个方向都会 RED。
    ⚠ 我上一轮说它「不可观测」是错的 —— 那次变异打的是下游 `sub_seen` 表达式，
    而真正承重的是**标志本身**。这条把它显式锁住。
    """
    import ast as _ast

    cases = [
        # (源码, 分支数, exclusive)
        ("if c:\n    A=1\nelse:\n    B=2\n", 2, True),
        ("if c:\n    A=1\n", 1, True),
        ("try:\n    A=1\nexcept Exception:\n    B=2\n", 2, False),
        ("for _i in (1,):\n    A=_i\nelse:\n    B=2\n", 2, False),
        ("with open(f) as fh:\n    A=fh\n", 1, False),
    ]
    for src, n_bodies, expect_excl in cases:
        stmt = _ast.parse(src).body[0]
        bodies, excl = _compound_sub_bodies(stmt)
        assert len(bodies) == n_bodies, (
            f"{src!r}: 分支数 {len(bodies)} != {n_bodies}"
        )
        assert excl is expect_excl, (
            f"{src!r}: exclusive={excl}，期望 {expect_excl}"
        )


def test_probe_accepts_class_scope_as_a_namespace():
    """⚠ 第八轮复审 §3.1：5 类**合法** Python 写法被误报成未绑定。

    类体是一个独立命名空间：类体内的引用能看见类属性，方法签名部件
    （默认值/注解）在类体作用域求值也能看见。探针原先把它们全报红。

    修法不是「放宽断言」，而是引入 `_ClassScopeMarker`：类作用域的绑定
    只对类体内部与方法签名可见，**不进外层** —— 类作用域不是闭包
    （第五轮正因并入外层造成 3 类漏报，那两条负样本就是它的守门）。
    """
    legal = [
        "class C:\n    A = 1\n    B = A\n",
        "class C:\n    helper = 1\n    def m(self, x=helper):\n        return x\n",
        "class C:\n    T = int\n    def m(self, x: T):\n        return x\n",
        "def g(cb=lambda q: q):\n    return cb\n",
        "class C:\n    def m(self, r=lambda s: s):\n        return r\n",
    ]
    for src in legal:
        problems = _probe_run(src)
        assert not problems, f"合法写法被误报：{src!r} ⇒ {problems}"

    # ⚠ 负样本：类体绑定**不得**泄漏到外层，否则这条守卫等于没有
    illegal = [
        ("def outer():\n    class C:\n        attr = 1\n    return attr\n", "attr"),
        ("class C:\n    leak = 1\ndef f():\n    return leak\n", "leak"),
        ("class C:\n    def m(self):\n        return attr\n", "attr"),
    ]
    for src, want in illegal:
        problems = _probe_run(src)
        assert any(want in p for p in problems), (
            f"类体绑定泄漏到外层导致漏报 {want!r}：{src!r} ⇒ {problems}"
        )


def test_probe_scopes_do_not_leak_between_sibling_classes():
    """两个类的作用域互不可见 —— 防止 marker 实现得太宽松。"""
    src = (
        "class A:\n    x = 1\n"
        "class B:\n    def m(self, y=x):\n        return y\n"
    )
    problems = _probe_run(src)
    assert any("'x'" in p for p in problems), (
        f"B 的签名部件不该看见 A 的类属性：{problems}"
    )


def test_scope_local_names_excludes_class_body_stores():
    """⚠ 直接断 `_scope_local_names` 的返回值，而不只是最终 unbound 列表。

    变异教训：把类作用域 marker 当普通分支处理（`visible |= set(node.names)`
    但不 `continue`），最终 unbound 列表**一模一样** —— 差别只在这两个
    辅助函数的返回值上。只测最终输出的话，这类变异 100% 漏过。
    """
    tree = ast.parse("class C:\n    attr = 1\ndef f():\n    return attr\n")
    names = _scope_local_names(tree)
    assert "attr" not in names, f"类体 Store 泄漏进外层可见名：{names}"
    assert {"C", "f"} <= names, f"类名与函数名必须在外层可见：{names}"


def test_scope_local_names_includes_signature_lambda_params():
    """`def g(cb=lambda q: q)` 的 q 绑定在外层作用域（签名在外层求值）。"""
    tree = ast.parse("def g(cb=lambda q: q):\n    return cb\n")
    names = _scope_local_names(tree)
    assert "q" in names, f"签名 lambda 形参未收进外层绑定：{names}"


def test_class_scope_marker_does_not_mask_later_missing_names():
    """⚠ `_ClassScopeMarker` 的真实风险：它 `visible |= names` 之后若**不收回**，
    后续节点就都能看见这些名字 → 真未绑定名被放行（漏报）。

    这比误报危险：误报会吵，漏报不会。第八轮修 marker 时最容易犯这个错。
    """
    src = (
        "def outer():\n"
        "    class C:\n"
        "        defined_in_class = 1\n"
        "    return defined_in_class, totally_unbound\n"
    )
    problems = _probe_run(src)
    assert any("'totally_unbound'" in p for p in problems), (
        f"marker 生效后放行了后续真未绑定名：{problems}"
    )
    assert any("'defined_in_class'" in p for p in problems), (
        f"类属性不该在外层可见：{problems}"
    )


def test_class_scope_marker_resets_between_two_functions():
    """⚠ 关键：`_check` 是**逐作用域**调用的，marker 的名字不得跨函数累积。

    若把 marker 名字写进模块级 `visible`（而非 `_check` 的局部），
    第一个函数里的类属性就会「泄漏」到第二个函数。
    """
    src = (
        "def first():\n"
        "    class A:\n"
        "        only_in_a = 1\n"
        "    return only_in_a\n"
        "def second():\n"
        "    return only_in_a\n"
    )
    problems = _probe_run(src)
    assert any("second" in p and "'only_in_a'" in p for p in problems), (
        f"类作用域的名字泄漏到了另一个函数：{problems}"
    )
