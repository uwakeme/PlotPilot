"""D-06：上下文预算必须**只有一个**数字（降级不许静默削薄上下文）。

核实：全仓有**两个**硬编码预算 —— 主路径 35000、降级路径 20000
（`engine/pipeline/base.py` 的 builder 兜底、`engine/runtime/legacy_writing_delegate.py`
的两处兜底）⇒ 降级发生时上下文被**静默削薄 43%**，而调用方只看到一条 warning。

本文件钉住：
- 预算是多少**只由一个常量**决定（`DEFAULT_CONTEXT_MAX_TOKENS`）；
- 三个降级调用点都引用那个常量（不许再写回 20000）；
- 已知例外（`suggest_outline` 的 28000 预算）**只允许**出现在那一个函数里
  —— 它的 28000 与该路径自己的 28000 **字符**截断上限是一对，不许扩散。
"""
from __future__ import annotations

import ast
from pathlib import Path

from application.engine.services.context_builder import (
    DEFAULT_CONTEXT_MAX_TOKENS,
    ContextBuilder,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
SCAN_DIRS = ("application", "engine", "interfaces", "infrastructure", "domain")


def _call_max_tokens_arg(path: Path, callee: str) -> list[ast.AST]:
    """找出调用 `callee(...)` 时传的 `max_tokens=` 实参（AST，不做文本匹配）。

    只看**上下文构建**类调用 —— `GenerationConfig(max_tokens=...)` 那是
    **模型输出**上限，与上下文预算是两回事，不能混在一起查。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != callee:
            continue
        for kw in node.keywords:
            if kw.arg == "max_tokens":
                found.append(kw.value)
    return found


def _default_arg_ast(path: Path, func_name: str, arg_name: str) -> ast.AST | None:
    """取某个函数某个参数的**默认值 AST 节点**（不是值！）。

    ⚠ 必须断言「默认值是那个常量」而不是「默认值等于 35000」——
    后者对「写回字面量 35000」的变异**照样通过**（假绿）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != func_name:
            continue
        pos_args = list(node.args.args)
        n_defaults = len(node.args.defaults)
        mapping: dict[str, ast.AST] = {}
        for arg, default in zip(pos_args[-n_defaults:] if n_defaults else [], node.args.defaults):
            mapping[arg.arg] = default
        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
            if default is not None:
                mapping[arg.arg] = default
        return mapping.get(arg_name)
    return None


def test_context_budget_constant_is_the_single_source():
    assert DEFAULT_CONTEXT_MAX_TOKENS == 35000


def test_context_builder_defaults_use_the_constant():
    """两个 build_* 的默认预算都**引用常量**（不是字面量）。"""
    import inspect

    path = REPO_ROOT / "application" / "engine" / "services" / "context_builder.py"
    for func in (ContextBuilder.build_context, ContextBuilder.build_structured_context):
        params = inspect.signature(func).parameters
        default = params["max_tokens"].default
        assert default == DEFAULT_CONTEXT_MAX_TOKENS, (
            f"{func.__name__} 的默认预算不是常量（会是第二份数字）"
        )
        # 关键：断言**来源**是那个常量（值相等不算 —— 写回字面量也相等）
        node = _default_arg_ast(path, func.__name__, "max_tokens")
        assert isinstance(node, ast.Name) and node.id == "DEFAULT_CONTEXT_MAX_TOKENS", (
            f"{func.__name__} 的默认预算必须引用常量，不能写回字面量"
        )


def test_workflow_default_uses_the_constant():
    """workflow 主路径的默认预算也引用同一个常量。"""
    import inspect

    from application.workflows.auto_novel_generation_workflow import (
        AutoNovelGenerationWorkflow,
    )

    default = inspect.signature(
        AutoNovelGenerationWorkflow.prepare_chapter_generation
    ).parameters["max_tokens"].default
    assert default == DEFAULT_CONTEXT_MAX_TOKENS

    path = REPO_ROOT / "application" / "workflows" / "auto_novel_generation_workflow.py"
    node = _default_arg_ast(path, "prepare_chapter_generation", "max_tokens")
    assert isinstance(node, ast.Name) and node.id == "DEFAULT_CONTEXT_MAX_TOKENS", (
        "workflow 的默认预算必须引用常量，不能写回字面量"
    )


def test_pipeline_fallback_uses_the_constant():
    """管线降级路径的 build_context 预算 = 常量（不再是 20000）。"""
    path = REPO_ROOT / "engine" / "pipeline" / "base.py"
    args = _call_max_tokens_arg(path, "build_context")

    assert args, "没找到 build_context 的 max_tokens 实参"
    for arg in args:
        assert isinstance(arg, ast.Name) and arg.id == "DEFAULT_CONTEXT_MAX_TOKENS", (
            "管线里的上下文预算必须引用常量（不许再写回 20000）"
        )


def test_legacy_fallbacks_use_the_constant():
    """legacy 写作的两处降级预算 = 常量（不再是 20000）。"""
    path = REPO_ROOT / "engine" / "runtime" / "legacy_writing_delegate.py"
    args = _call_max_tokens_arg(path, "build_context")
    args += _call_max_tokens_arg(path, "build_fallback_chapter_bundle")

    assert args, "没找到 legacy 的 max_tokens 实参"
    for arg in args:
        assert isinstance(arg, ast.Name) and arg.id == "DEFAULT_CONTEXT_MAX_TOKENS", (
            "legacy 降级路径的上下文预算必须引用常量"
        )


def test_no_degraded_budget_literal_remains():
    """全仓不再有 `max_tokens=20000` 这个**降级**预算字面量。

    ⚠ 第 3 批复审 S2：这条原先**只扫 `ast.Call` 的 keyword**，
    扫不到**函数参数默认值**（`def f(..., max_tokens: int = 20000)`）
    ⇒ 而 D-06 的 bug 本身就是这个形状（`auto_novel_generation_workflow.py`
    有一处 `max_tokens: int = 20000` 的默认值）。
    ⇒ 现在**两种形状都扫**。
    """
    offenders = []
    for dirname in SCAN_DIRS:
        for path in (REPO_ROOT / dirname).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg != "max_tokens":
                            continue
                        if (
                            isinstance(kw.value, ast.Constant)
                            and kw.value.value == 20000
                        ):
                            offenders.append(
                                f"{path.relative_to(REPO_ROOT)}:{node.lineno} (call keyword)"
                            )
                # ⚠ 函数/方法的**参数默认值**（D-06 的 bug 就是这个形状）
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = node.args
                    positional = list(args.posonlyargs) + list(args.args)
                    defaults = list(args.defaults)
                    for arg, default in zip(positional[-len(defaults):], defaults) if defaults else []:
                        if arg.arg != "max_tokens":
                            continue
                        if isinstance(default, ast.Constant) and default.value == 20000:
                            offenders.append(
                                f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                                f"(参数默认值 {arg.arg})"
                            )
    assert not offenders, f"仍有降级预算字面量 20000：{offenders}"


def test_known_smaller_budget_stays_in_its_own_function():
    """已知例外：`suggest_outline` 的 28000 只允许出现在那一个函数里。

    它的 28000 与该路径自己的 28000 **字符**截断上限是一对；
    扩散到别处就又成了「两套数字长期并存」。
    """
    offenders = []
    for dirname in SCAN_DIRS:
        for path in (REPO_ROOT / dirname).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg != "max_tokens":
                        continue
                    if (
                        isinstance(kw.value, ast.Constant)
                        and kw.value.value == 28000
                    ):
                        rel = str(path.relative_to(REPO_ROOT))
                        if not rel.endswith("auto_novel_generation_workflow.py"):
                            offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"28000 这个预算扩散到了别处：{offenders}"
