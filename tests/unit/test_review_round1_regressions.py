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
        elif isinstance(node, ast.Lambda):
            # lambda 的**形参**绑定在外层作用域（lambda 体也在外层求值），
            # 漏掉这一支会把 `key=lambda item: item[0]` 里的 item 报成未绑定。
            local |= _params_of(node)
        elif isinstance(node, ast.comprehension):
            # 推导式有自己的作用域，但目标变量在本作用域可见
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    local.add(sub.id)
    return local


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
            for expr in (list(node.decorator_list)
                         + [d for d in node.args.defaults if d is not None]
                         + ([node.returns] if node.returns else [])
                         + [a.annotation for a in (
                             list(node.args.args) + list(node.args.kwonlyargs)
                             + ([node.args.vararg] if node.args.vararg else [])
                             + ([node.args.kwarg] if node.args.kwarg else [])
                         ) if getattr(a, "annotation", None)]):
                stack.append(expr)
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
            stack.extend(node.bases)
            stack.extend(node.keywords)
            stack.extend(node.decorator_list)
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # 方法签名部件（装饰器/默认值/注解）在**类体作用域**求值，
                    # 单独扫它们的表达式（第六轮复审 §3.3）。
                    for expr in (list(stmt.decorator_list)
                                 + [d for d in stmt.args.defaults if d is not None]
                                 + ([stmt.returns] if stmt.returns else [])
                                 + [a.annotation for a in (
                                     list(stmt.args.args) + list(stmt.args.kwonlyargs)
                                     + ([stmt.args.vararg] if stmt.args.vararg else [])
                                     + ([stmt.args.kwarg] if stmt.args.kwarg else [])
                                 ) if getattr(a, "annotation", None)]):
                        stack.append(expr)
                    continue  # 方法**体**另有作用域
                stack.append(stmt)
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
        for node in scope_nodes:
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id not in visible):
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
