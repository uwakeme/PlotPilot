"""E-08：节拍拆法**由 AI 提案**（销案守卫 —— 钉住「销案的前提」）。

核实结论（第 51 批，读码 + 探针，不是推断）
--------------------------------------------
登记表 E-08 行写的是「节拍拆分由字数档代码定 ⇒ 改造为 AI 提案拆分方案」。
**这条描述已经过期**，三件事让「AI 提案拆分方案」**事实上已经达成**：

1. 登记表点名的那个函数（``merge_beats_by_target``：≤2200 字合并 1 拍、
   ≤3200 合并 2 拍、>3200 时 <350 字的拍并入邻拍）**已经不存在** ——
   它零生产调用方，早一批作为死代码删除。
2. **新生产管线不做按拍拆分**：`engine/pipeline/base.py` 的
   ``_step_prepare_chapter_plan`` 把 ``ctx.beats`` 置空 ⇒ **整章一次写完**
   ⇒ 「一章拆几次 LLM 调用」根本不由代码档位决定。
3. 章内结构由 **AI 的章前执行剧本**给出
   （``ChapterPreplanningService.ensure_execution_plan``，一次 LLM 调用）
   ⇒ 拆法**本来就是 AI 提的**。

代码侧保留的只有**上限与下限**（``context_builder._cap_and_merge_beats``：
``MAX_BEATS = 12`` / ``MIN_BEAT_WORDS = 350``）—— 而「每拍下限防碎片」正是
登记表 E-08 自己写明**要保留的不变量** ⇒ 不是「代答」，是护栏。

⚠ 本文件的作用：**让这条销案的前提保持为真**。
⚠ 如果将来恢复「按拍写作」，G3/G4 会红 ⇒ 提醒重开 E-08（那时才需要真的做
「AI 提案拆分方案」）。
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BEAT_CONTRACTS = REPO_ROOT / "engine" / "pipeline" / "beat_contracts.py"
PIPELINE_BASE = REPO_ROOT / "engine" / "pipeline" / "base.py"
CONTEXT_BUILDER = REPO_ROOT / "application" / "engine" / "services" / "context_builder.py"


def _module_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _method_source(path: Path, class_name: str, method_name: str) -> str:
    """取某个类方法的方法体源码（形状守卫用）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = path.read_text(encoding="utf-8").splitlines()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name:
                    start = item.lineno - 1
                    end = item.end_lineno or item.lineno
                    return "\n".join(lines[start:end])
    raise AssertionError(f"{path.name} 里找不到 {class_name}.{method_name}")


# ─────────────────────── G1 · G2 ───────────────────────


def test_the_word_tier_beat_splitter_is_gone():
    """按字数档决定拍数的那个函数**必须**不存在（它是 E-08 的原始病灶）。"""
    funcs = _module_functions(BEAT_CONTRACTS)
    assert "merge_beats_by_target" not in funcs, (
        "按字数档决定拆几拍的函数又回来了 ⇒ 它零生产调用方（死代码）"
    )


def test_the_anti_fragment_floor_is_still_in_code():
    """「每拍下限防碎片」这条**不变量**必须还在代码里。

    ⚠ 这是登记表 E-08 自己写明**要保留**的东西 ⇒ 销案不等于把护栏拆了。
    """
    tree = ast.parse(CONTEXT_BUILDER.read_text(encoding="utf-8"))
    consts: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name) and isinstance(item.value, ast.Constant):
                            if isinstance(item.value.value, (int, float)):
                                consts[target.id] = item.value.value
    assert consts.get("MAX_BEATS", 0) >= 1, "节拍上限没了"
    assert consts.get("MIN_BEAT_WORDS", 0) >= 1, "每拍下限（防碎片）没了"


# ─────────────────────── G3 · G4 ───────────────────────


def test_the_new_pipeline_does_not_split_by_beats():
    """新生产管线必须**不做按拍拆分**（``ctx.beats`` 置空 ⇒ 整章一次写完）。

    ⚠ 这条是 E-08 销案的**前提** —— 一旦恢复按拍写作 ⇒ 前提没了 ⇒ 本守卫红
    ⇒ 提醒重开 E-08。
    """
    source = _method_source(PIPELINE_BASE, "BaseStoryPipeline", "_step_prepare_chapter_plan")
    assert "ctx.beats = []" in source, (
        "新生产管线又开始按拍拆分了 ⇒ E-08 的前提变了 ⇒ 重开这一条"
    )


def test_the_chapter_structure_comes_from_the_llm():
    """章内结构必须来自 **LLM 的章前执行剧本**（拆法是 AI 提的）。

    ⚠ 形状守卫：只看「有没有真的 await 那个 LLM 服务」——
    若哪天换回同步的确定性拆法，本守卫会红 ⇒ 提醒重开 E-08。
    """
    source = _method_source(PIPELINE_BASE, "BaseStoryPipeline", "_step_prepare_chapter_plan")
    assert "ensure_execution_plan(" in source, "章前执行剧本的调用不见了"
    assert "await" in source, "章前执行剧本不是 await 出来的（⇒ 不是 LLM 那条路）"
