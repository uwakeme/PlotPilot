"""D-09：删掉「阶段→phase」这条**死映射**，但**活的那份 phase 机制必须还在**。

核实：登记表 D-09 写的是
「`runner.py:153-167`：writing/auditing 恒映射 "development"、规划恒 "opening"
—— 管线对 phase 当前无消费，死映射易误导致信」。

实测：
- `runner._get_novel_phase()` 把写作阶段硬编码映射成 opening/development/finale，
  喂给 `ctx.phase`；
- `ctx.phase` 在 `engine/pipeline/` 里**只有声明、没有任何消费**（AST 扫过）；
- 真正的 phase 机制在**别处**且**真的被消费**：
  `context_budget_allocator` 按**进度**判 `StoryPhase`（阈值可由 CPMS
  `lifecycle-phase-directives` 覆盖）⇒ 产出沙漏阶段阈值与生命周期指令。

⇒ 那条硬编码映射是**第二份** phase 口径（而且比真实那份粗），
留着只会让人误以为「写作阶段决定 phase」⇒ **删**（登记表的「删或实现」选删：
再接一份 stage 驱动的 phase 只会制造**第三个**真相源）。

本文件钉住：
1. 死映射真的删干净了（方法、ctx 字段、传参三处）；
2. **活的那份 phase 机制还在**（分类器 + CPMS 指令 + 分配器真的在用）。
"""
from __future__ import annotations

import ast
from pathlib import Path

from engine.pipeline.context import PipelineContext

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/unit/engine/ → 仓库根


def _module_source(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_pipeline_context_no_longer_carries_a_dead_phase_field():
    """ctx.phase 字段删掉了（它全仓无人消费）。"""
    assert "phase" not in PipelineContext.__dataclass_fields__, (
        "PipelineContext 又带上了 phase 字段 —— 那条口径无人消费，是死字段"
    )


def test_runner_no_longer_has_the_stage_to_phase_mapping():
    """runner 的「阶段→phase」硬编码映射删掉了。

    ⚠ 用 **AST** 判（不是源码 `contains`）：文件里的说明注释会提到这个方法名，
    文本匹配会**命中自己的注释** ⇒ 守卫永远是红的/永远不红，两头都错。
    """
    tree = ast.parse(_module_source("engine/runtime/runner.py"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name != "_get_novel_phase", (
                "runner 还留着死的阶段→phase 映射"
            )

    # 那份硬编码字典也不许再出现
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            ]
            assert "macro_planning" not in keys or "act_planning" not in keys, (
                "runner 里还有那份硬编码 phase_map"
            )


def test_writing_delegate_no_longer_feeds_the_dead_phase():
    """写作委托不再把死 phase 喂进上下文（AST 判：无该调用、无 phase= 实参）。"""
    tree = ast.parse(_module_source("engine/runtime/writing_delegate.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "_get_novel_phase":
                raise AssertionError("写作委托还在调 _get_novel_phase")
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "_make_context":
                for kw in node.keywords:
                    assert kw.arg != "phase", "_make_context 不该再收 phase 实参"


def test_live_phase_mechanism_still_exists_and_is_consumed():
    """反向守卫：**活的那份** phase 机制（按进度判 StoryPhase + CPMS 指令）必须还在。

    删死映射是为了消除「两个真相源」，**不是**把 phase 机制整个删掉。
    """
    allocator = _module_source("application/engine/services/context_budget_allocator.py")

    # ① 按进度判阶段的分类器
    assert "def _classify_phase" in allocator, "按进度判 StoryPhase 的分类器不见了"
    assert "classify_phase(" in allocator
    # ② CPMS 生命周期指令
    assert "LIFECYCLE_PHASE_DIRECTIVES" in allocator, (
        "CPMS lifecycle-phase-directives 的接线不见了"
    )
    assert "def _build_lifecycle_directive" in allocator
    assert "def _get_phase_directives" in allocator


def test_live_phase_mechanism_is_actually_called_in_the_allocation_path():
    """更进一步：分配器**真的调用**了阶段分类与指令构建（不是只定义了方法）。"""
    tree = ast.parse(
        _module_source("application/engine/services/context_budget_allocator.py")
    )
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name:
            called.add(name)
    assert "_classify_phase" in called, "阶段分类器没被调用（phase 机制其实没接上）"
    assert "_build_lifecycle_directive" in called, "生命周期指令没被构建（指令其实没进上下文）"
