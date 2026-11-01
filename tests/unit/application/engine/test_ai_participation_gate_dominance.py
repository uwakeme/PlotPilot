# -*- coding: utf-8 -*-
"""第 77 批：AI 参与总闸的**支配性**守卫（逐条 25 项）。

⚠ 旧守卫（``test_ai_participation_master_switch.py::test_every_registered_feature_is_actually_wired``）
  只证明「这个 feature id 在某个 ``ai_participation_enabled(...)`` 里**出现过**」
  ⇒ 闸门写在**别的分支**上、或者写在模型调用**之后**，它照样绿
  ⇒ 「登记了却没接闸」与「接了闸但**闸不在路上**」被混为一谈。

本文件证明的是更强的一条：**闸门在模型调用的路径上**。

四种形态（逐条核实 25 项后归纳出来的）
--------------------------------------
* **A 短路形**（多数）：同一个函数里 ``if not GATE: return 未评估``，
  而模型调用（``generate`` / ``stream_generate``，**含跨模块的传递调用**）
  在这个函数里 ⇒ 闸门关闭 ⇒ 模型调用**不可达**；
* **B 正向形**：``if GATE: <模型调用在体内>``；
* **C1 中介型**（``chapter_mode`` / ``context_weights`` / ``continuation_strategy``
  / ``voice_llm_mode``）：服务本身**不**直接调总闸（既有设计：``AI_PARTICIPATION_SETTINGS_MEDIATED``）
  ⇒ 闸门在**持闸方**或**调用方**身上 ⇒ 那就要求：
  **服务的每个调用点，其消费者（或消费者上层的调用者）必须先短路**；
* **C2 读取器型**（``chapter_target``）：闸门折在「设置读取器」的 ``enabled`` 字段里
  ⇒ 那么**每个消费读取器的函数**都必须在用 AI 结果之前短路掉那个设置。

⇒ 四种形态都算「闸门在路径上」；**任何一项落不到任何一种形态 ⇒ 红**。
"""
from __future__ import annotations

import ast
import pathlib

from application.engine.services.ai_participation_settings import (
    AI_PARTICIPATION_FEATURES,
    AI_PARTICIPATION_SETTINGS_MEDIATED,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
SCAN_DIRS = ("application", "engine", "interfaces", "infrastructure", "domain")
#: 模型调用点（``LLMService`` 只有这两个真正打模型的入口）
MODEL_CALLS = {"generate", "stream_generate"}
#: 传递闭包深度（``score_chapter_async → _score_chapter_llm → … → generate``）
_MAX_DEPTH = 4
#: 「消费者往上找短路」的层数上限（实测链路 3 层）
_MAX_UP = 3

#: 中介型 feature（服务自己不接闸 ⇒ 走 C1）
_MEDIATED_FEATURES = {feature for _caller, _holder, feature in AI_PARTICIPATION_SETTINGS_MEDIATED}


def _python_sources() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for dirname in SCAN_DIRS:
        out.extend(sorted((REPO_ROOT / dirname).rglob("*.py")))
    return out


def _parents(tree: ast.AST) -> dict:
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing(node: ast.AST, parents: dict, kinds):
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, kinds):
            return cur
    return None


def _func_of(node: ast.AST, parents: dict):
    return _enclosing(node, parents, (ast.FunctionDef, ast.AsyncFunctionDef))


def _call_name(node: ast.Call) -> str | None:
    return getattr(node.func, "id", None) or getattr(node.func, "attr", None)


def _is_model_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _call_name(node) in MODEL_CALLS


def _short_circuits(body) -> bool:
    for stmt in body:
        if isinstance(stmt, (ast.Return, ast.Raise)):
            return True
    return False


def _call_graph(tree: ast.AST) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                name = _call_name(sub)
                if name:
                    called.add(name)
        graph.setdefault(node.name, set()).update(called)
    return graph


def _reaches_model(
    func_name: str,
    graph: dict[str, set[str]],
    depth: int = 0,
    defs: dict[str, int] | None = None,
) -> bool:
    """这个函数（**传递地**，可跨模块）会不会打到模型。

    ⚠ **只跟随唯一定义的名字**：定义多处 ⇒ 名字有歧义 ⇒ 这条边不可信
      （公共名字会把不相关的函数连成「也会打模型」的链 ⇒ 假红）。
    """
    if depth > _MAX_DEPTH:
        return False
    called = graph.get(func_name)
    if not called:
        return False
    if called & MODEL_CALLS:
        return True
    for name in called:
        if defs is not None and defs.get(name, 0) != 1:
            continue
        if _reaches_model(name, graph, depth + 1, defs):
            return True
    return False


def _is_negated_gate(node: ast.AST, gate_call: ast.Call) -> bool:
    """``not GATE`` 这个形状。"""
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.Not)
        and node.operand is gate_call
    )


def _gate_kind(test_node: ast.AST, gate_call: ast.Call) -> str:
    """闸门调用在布尔表达式里的位置 ⇒ negated / positive / ineffective。

    ⚠ **必须看 AST 的位置**，不能看源码里有没有那个字符串 ——
      ``if False and not GATE:`` 源码里有闸门、if 体也 return，
      但那个闸门**永远不生效**（左边是 False）⇒ 守卫照样绿 ⇒ 假的。
    """
    if test_node is gate_call:
        return "positive"
    if _is_negated_gate(test_node, gate_call):
        return "negated"
    if isinstance(test_node, ast.BoolOp):
        if isinstance(test_node.op, ast.Or):
            # ``X or not GATE`` ⇒ 关闸 ⇒ 短路
            if any(_is_negated_gate(v, gate_call) for v in test_node.values):
                return "negated"
            if any(v is gate_call for v in test_node.values):
                return "positive"
        elif isinstance(test_node.op, ast.And):
            # ``X and GATE`` ⇒ 开闸才走 if 体
            if any(v is gate_call for v in test_node.values):
                return "positive"
    return "ineffective"


def _gate_shape(call: ast.Call, parents: dict) -> tuple[str, ast.AST | None]:
    """闸门调用所在 ``if`` 的形态 ⇒ (shape, if_stmt)。

    shape: short_circuit（negated + if 体 return）/ positive（闸门在 if 体内）
           / ineffective（闸门是摆设）/ other
    """
    gate_if = None
    cur = call
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, ast.If):
            gate_if = cur
            break
    if gate_if is None:
        return "other", None
    kind = _gate_kind(gate_if.test, call)
    if kind == "negated" and _short_circuits(gate_if.body):
        return "short_circuit", gate_if
    if kind == "positive":
        return "positive", gate_if
    if kind == "ineffective":
        return "ineffective", gate_if
    return "other", gate_if


def _collect() -> tuple[dict, list, list, dict, dict]:
    """扫全仓 ⇒ (feature → 闸门调用点, 源码, 跳过列表, 全局调用图, 名字定义数)。"""
    found: dict[str, list] = {}
    sources: list[dict] = []
    skipped: list[str] = []
    graph: dict[str, set[str]] = {}
    defs: dict[str, int] = {}
    for path in _python_sources():
        try:
            # ⚠ `utf-8-sig` ⇒ 带 BOM 的文件也能解析（否则静默跳过 = 没被检查）
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            skipped.append("%s: %s" % (path.relative_to(REPO_ROOT).as_posix(), exc))
            continue
        parents = _parents(tree)
        module_graph = _call_graph(tree)
        for name, called in module_graph.items():
            graph.setdefault(name, set()).update(called)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs[node.name] = defs.get(node.name, 0) + 1
        sources.append({"path": path, "tree": tree, "parents": parents, "graph": module_graph})
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) != "ai_participation_enabled":
                continue
            if not (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                continue
            found.setdefault(node.args[0].value, []).append(
                {"path": path, "call": node, "parents": parents, "graph": module_graph}
            )
    return found, sources, skipped, graph, defs


def test_the_scan_covers_every_backend_file():
    """⚠ 扫描本身不许有盲区（跳过 = 没被检查 ⇒ 断言为 0）。"""
    _found, _sources, skipped, _graph, _defs = _collect()
    assert not skipped, (
        "有 %d 个文件**没被检查**（解析失败 ⇒ 跳过）⇒ 扫描型守卫有盲区：%s"
        % (len(skipped), skipped[:5])
    )


def test_every_registered_feature_has_a_gate_on_the_path_to_the_model_call():
    """⚠ 逐条：每项能力的闸门都必须**在模型调用的路径上**。"""
    found, sources, _skipped, global_graph, defs = _collect()

    # ① 登记表与接线必须一一对应（登记了没人接 / 接了没登记都要红）
    assert set(found) == set(AI_PARTICIPATION_FEATURES), (
        "登记在册的 AI 参与能力与实际接线的不一致："
        f"登记={sorted(AI_PARTICIPATION_FEATURES)} 接线={sorted(found)}"
    )

    problems: list[str] = []
    for feature in AI_PARTICIPATION_FEATURES:
        dominated = False
        reader_names: list[str] = []
        for hit in found[feature]:
            path, call, parents = hit["path"], hit["call"], hit["parents"]
            fn = _func_of(call, parents)
            if fn is None:
                problems.append("%s: 闸门不在函数里" % feature)
                continue
            shape, gate_if = _gate_shape(call, parents)
            if _reaches_model(fn.name, global_graph, 0, defs):
                if shape == "short_circuit":
                    dominated = True
                elif shape == "positive" and gate_if is not None:
                    if _if_body_reaches_model(gate_if, global_graph, defs):
                        dominated = True
                    else:
                        problems.append(
                            "%s: %s:%d 的闸门是**正向**但模型调用不在它的体内"
                            % (feature, path.relative_to(REPO_ROOT).as_posix(), call.lineno)
                        )
                else:
                    problems.append(
                        "%s: %s:%d 的闸门**不在**模型调用的路径上（形态=%s）"
                        % (feature, path.relative_to(REPO_ROOT).as_posix(), call.lineno, shape)
                    )
            elif shape == "ineffective":
                problems.append(
                    "%s: %s:%d 的闸门**是摆设**（布尔位置让它永远不生效）"
                    % (feature, path.relative_to(REPO_ROOT).as_posix(), call.lineno)
                )
            else:
                reader_names.append(fn.name)

        if dominated:
            continue
        if feature in _MEDIATED_FEATURES:
            # C1：服务自己不接闸（既有设计）⇒ 闸门靠**调用方** ⇒ 每个调用点都得先短路
            bad = _mediated_consumers_without_short_circuit(feature, sources, defs)
            if bad:
                problems.append(
                    "%s: 中介型能力，**调用方**没有先短路：%s" % (feature, bad)
                )
            continue
        # C2：闸门折在读取器里 ⇒ 每个消费读取器的函数都要先短路
        if reader_names and _reader_consumers_short_circuit(reader_names, sources):
            continue
        problems.append(
            "%s: 没找到任何「闸门在模型调用路径上」的形态（读取器=%s）" % (feature, reader_names)
        )

    assert not problems, "以下能力的闸门**不在**模型调用的路径上：\n  " + "\n  ".join(problems)


def _if_body_reaches_model(
    gate_if: ast.If, graph: dict[str, set[str]], defs: dict[str, int]
) -> bool:
    for node in ast.walk(gate_if):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if not name:
            continue
        if name in MODEL_CALLS:
            return True
        if _reaches_model(name, graph, 0, defs):
            return True
    return False


def _mediated_consumers_without_short_circuit(
    feature: str, sources: list[dict], defs: dict[str, int]
) -> list[str]:
    """中介型能力：服务的**每个调用点**的消费者（或其上层调用者）必须先短路。"""
    caller_module = None
    for caller, _holder, feat in AI_PARTICIPATION_SETTINGS_MEDIATED:
        if feat == feature:
            caller_module = caller
            break
    if caller_module is None:
        return ["%s: 不在中介表里" % feature]
    caller_path = REPO_ROOT / caller_module
    service_source = None
    for source in sources:
        if source["path"] == caller_path:
            service_source = source
            break
    if service_source is None:
        return ["%s: 解析不了 %s" % (feature, caller_module)]
    service_methods = [
        name
        for name in service_source["graph"]
        if _reaches_model(name, service_source["graph"], 0, defs)
    ]
    if not service_methods:
        return ["%s: %s 里找不到会打模型的方法" % (feature, caller_module)]

    bad: list[str] = []
    for source in sources:
        for node in ast.walk(source["tree"]):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) not in service_methods:
                continue
            consumer = _func_of(node, source["parents"])
            if consumer is None:
                continue
            if _consumer_short_circuits(consumer, source, 0):
                continue
            bad.append(
                "%s:%d 调用 %s() 没先短路"
                % (
                    source["path"].relative_to(REPO_ROOT).as_posix(),
                    node.lineno,
                    _call_name(node),
                )
            )
    return bad


def _reader_consumers_short_circuit(reader_names: list[str], sources: list[dict]) -> bool:
    """读取器型：每个消费读取器的函数都必须在**后面**做「``if not settings.enabled: return``」。"""
    for source in sources:
        for node in ast.walk(source["tree"]):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) not in reader_names:
                continue
            consumer = _func_of(node, source["parents"])
            if consumer is None:
                return False
            if not _short_circuits_on_enabled(consumer):
                return False
    return True


def _consumer_short_circuits(consumer, source, depth: int = 0) -> bool:
    """这个消费者（或它**上层的调用者**）有没有「短路 if」。

    ⚠ 为什么要**递归**往上找：实测链路有三层 ——
      ``score_chapter_async → _score_chapter_llm → _update_baseline → compute_baseline``
      ⇒ 闸门在**分派器**上 ⇒ 只看一层会误报。
    """
    if depth > _MAX_UP:
        return False
    if _has_switch_short_circuit(consumer):
        return True
    name = consumer.name
    for node in ast.walk(source["tree"]):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node) != name:
            continue
        caller = _func_of(node, source["parents"])
        if caller is None or caller is consumer:
            continue
        if _consumer_short_circuits(caller, source, depth + 1):
            return True
    return False


def _has_switch_short_circuit(func) -> bool:
    """这个函数里有没有「按开关短路」的 if（测试提到 enabled 或调了总闸）。"""
    for node in ast.walk(func):
        if not isinstance(node, ast.If):
            continue
        if not _short_circuits(node.body):
            continue
        test_src = ast.unparse(node.test)
        if "enabled" in test_src or "ai_participation_enabled" in test_src:
            return True
    return False


def _short_circuits_on_enabled(func) -> bool:
    """这个函数里有一个「按 ``.enabled`` 短路」的 if，且**后面还有活**（= 被它挡住的 AI 工作）。"""
    check_line = None
    for node in ast.walk(func):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.unparse(node.test)
        if "enabled" not in test_src:
            continue
        if not _short_circuits(node.body):
            continue
        check_line = node.end_lineno
        break
    if check_line is None:
        return False
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and node.end_lineno and node.end_lineno > check_line:
            return True
    return False
