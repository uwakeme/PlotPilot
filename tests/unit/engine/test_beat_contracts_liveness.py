"""E-08：死代码探测器 —— `beat_contracts` 的公开函数不许只被自己的测试调用。

背景：`merge_beats_by_target`（按章字数档位决定「一章拆几次 LLM 调用」）
**零生产调用方** —— 探针扫全仓（`application` / `engine` / `interfaces` /
`infrastructure` / `domain`，**含模块内部调用**）确认它只被自己的测试调用。
它自己的测试把它守成「活的」⇒ 看起来有覆盖，实际上一行生产代码都不走。

⇒ 本文件把这个洞**变成可见的**：
- `merge_beats_by_target` 已不存在（删除是真的）；
- `beat_contracts` 的每个**公开**函数都必须有**生产**调用方
  （下一个死函数长出来时，这里会红）。
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "engine" / "pipeline" / "beat_contracts.py"
SCAN_DIRS = ("application", "engine", "interfaces", "infrastructure", "domain")


def _module_functions() -> list[str]:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ]


def _production_callers(func_name: str) -> set[str]:
    """函数名在**生产代码**里的调用点（AST；测试目录不算）。"""
    found: set[str] = set()
    for dirname in SCAN_DIRS:
        for path in (REPO_ROOT / dirname).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            rel = str(path.relative_to(REPO_ROOT))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = (
                        getattr(node.func, "id", None)
                        or getattr(node.func, "attr", None)
                    )
                    if name == func_name:
                        found.add(rel)
                elif isinstance(node, ast.Attribute) and node.attr == func_name:
                    found.add(rel)
    return found


def test_dead_beat_merger_is_gone():
    """E-08：那个零调用方的档位合并器必须**真的**删掉了。"""
    import engine.pipeline.beat_contracts as mod

    assert not hasattr(mod, "merge_beats_by_target"), (
        "merge_beats_by_target 又回来了 —— 它零生产调用方，"
        "留着只会让人以为「一章拆几次由代码档位定」还在生效"
    )
    assert "merge_beats_by_target" not in MODULE_PATH.read_text(encoding="utf-8")


def test_every_public_beat_helper_has_a_production_caller():
    """每个**公开**辅助函数都必须有生产调用方（否则它就是下一个死代码）。"""
    dead = []
    for name in _module_functions():
        callers = _production_callers(name)
        if not callers:
            dead.append(name)
    assert not dead, (
        f"这些公开函数零生产调用方（只剩测试在用）⇒ 死代码：{dead}。"
        "要么删掉，要么接线 —— 别让测试把它守成『活的』"
    )


def test_live_beat_helpers_keep_their_production_callers():
    """反向守卫：活着的两个函数**必须**仍在生产里被调用（别误删）。

    ⚠ 只查「名字在生产代码里出现过」是不够的 ——
    **定义被改名、调用方没改**时，那个旧名字仍然出现在生产代码里（调用点），
    守卫会放行，但运行时其实是 ImportError。
    ⇒ 这里同时断言「模块仍然**暴露**这个函数」。
    """
    import engine.pipeline.beat_contracts as mod

    for name in ("serialize_beats_for_shared_state", "merge_two_beats"):
        assert hasattr(mod, name), f"{name} 不再是模块的公开 API（定义被改名/删除？）"
        assert _production_callers(name), f"{name} 的生产调用方不见了"
