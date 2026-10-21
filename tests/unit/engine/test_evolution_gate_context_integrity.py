"""H-01a：闸门判出 blocking 时，上下文**不许**被静默降级。

## 问题（本批核实新发现）

`_step_prepare_governance` 按「已知的有意偏离」只告警 + 报告随行（**不阻断**）。
但**同一条链路上的第二处闸门判定**——`prepare_chapter_generation` 内部对七段剧本
再判一次——遇到 blocking 违规时会：

```python
raise RuntimeError("evolution_gate_blocked:" + ...)
```

而 `engine/pipeline/base.py` 那一行是：

```python
try:
    bundle = ctx.chapter_workflow.prepare_chapter_generation(...)
except Exception as e:
    logger.warning("chapter_workflow 准备失败，降级到 context_builder: %s", e)
    bundle = None
# ↓ 降级路径
bundle = ctx.context_builder.build_context(..., max_tokens=20000)   # 主路径是 35000
```

⇒ 闸门一判 blocking，**整份上下文 bundle 被丢掉**、预算从 35000 掉到 20000
（上下文被静默削薄 43%），而用户只看到一条 warning。

两次判定的**输入还不同**：第一次（`_step_prepare_governance`）看规划期大纲，
第二次（`prepare_chapter_generation`）看七段剧本 ⇒ 报告里的违规与触发降级的违规
**不是同一份判定**。

## 改法

管线侧调 `prepare_chapter_generation` 时传 `allow_evolution_gate_bypass=True`：

- 闸门**仍然跑**，结论**仍然进 bundle**（`evolution_gate` / `evolution_gate_blocked`）；
- blocking 违规**仍然以「=== EVOLUTION GATE ===」块前置进上下文文本**给模型看
  （`auto_novel_generation_workflow.py:422-434` 原本就是这么设计的）；
- 但**不再中断上下文组装** ⇒ 不阻断、**不降级**。

⇒ 顺带把「有意偏离」的记录改回准确：**闸门不阻断章，也不让上下文降级；
它只负责把违规报出来并告诉模型**。登记表那条不变量
（「死亡角色不得实际登场」）**仍然没有代码保证**——那是本条后续要做的事。

本文件锁住这几条。
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

from application.workflows.auto_novel_generation_workflow import AutoNovelGenerationWorkflow


ROOT = pathlib.Path(__file__).resolve()
for _parent in ROOT.parents:
    if (_parent / ".git").exists() or (_parent / "pytest.ini").exists():
        ROOT = _parent
        break

BASE_REL = "engine/pipeline/base.py"

_BLOCKING_REPORT = {
    "is_pass": False,
    "violations": [
        {
            "level": "blocking",
            "type": "DEAD_CHARACTER_REAPPEARS",
            "message": "已死亡角色「陈小」仍以活跃人物出现",
        }
    ],
    "required_continuations": [],
}


# ── 夹具 ───────────────────────────────────────────────────────────


class _FakeGate:
    def __init__(self, report):
        self._report = report

    def check(self, **kwargs):
        return SimpleNamespace(to_dict=lambda: dict(self._report))


class _FakeBuilder:
    def build_structured_context(self, **kwargs):
        return {
            "layer1_text": "【T0 叙事契约】",
            "layer2_text": "【T1 角色锚点】",
            "layer3_text": "【T2 伏笔槽位】",
            "token_usage": {"total": 123},
        }

    def build_voice_anchor_system_section(self, novel_id):
        return ""


def _workflow(report=None) -> AutoNovelGenerationWorkflow:
    """只装配 `prepare_chapter_generation` 真正用到的那几个协作者。"""
    wf = AutoNovelGenerationWorkflow.__new__(AutoNovelGenerationWorkflow)
    wf.evolution_gate_service = _FakeGate(report if report is not None else _BLOCKING_REPORT)
    wf.context_builder = _FakeBuilder()
    wf._get_storyline_context = lambda *a, **k: ""
    wf._get_plot_tension = lambda *a, **k: {}
    wf._get_style_summary = lambda *a, **k: ""
    return wf


# ── 1. 管线侧：必须显式要求「闸门不中断」 ───────────────────────────


def test_the_pipeline_asks_the_workflow_not_to_abort_on_the_gate():
    """AST 判定：管线调 `prepare_chapter_generation` 时必须传
    `allow_evolution_gate_bypass=True`。

    不传 ⇒ workflow 判出 blocking 就 raise ⇒ `base.py` 的 `except Exception`
    把它当成「上下文准备失败」⇒ **整份 bundle 被丢掉、预算掉到 20000**。
    """
    src = (ROOT / BASE_REL).read_bytes().decode("utf-8-sig")
    tree = ast.parse(src)

    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "prepare_chapter_generation":
            calls.append(node)

    assert calls, "找不到 prepare_chapter_generation 的调用点"

    for call in calls:
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        assert "allow_evolution_gate_bypass" in kwargs, (
            "管线调 prepare_chapter_generation 没传 allow_evolution_gate_bypass "
            "⇒ 闸门一判 blocking 就会把上下文静默降级（预算 35000 → 20000）"
        )
        value = kwargs["allow_evolution_gate_bypass"]
        assert isinstance(value, ast.Constant) and value.value is True, (
            "allow_evolution_gate_bypass 必须是 True —— "
            "闸门按「已知的有意偏离」不阻断，也不许让上下文降级"
        )


def test_the_pipeline_still_reports_the_gate_verdict():
    """闸门跑过 ⇒ 结论必须留在 ctx 上（不能因为不阻断就没人知道）。

    两处留痕：
    - `_step_prepare_governance` 把**规划期大纲**的判定写进 `ctx.evolution_continuity_report`；
    - `_step_build_context` 把**七段剧本**的判定连同整份 bundle 存进 `ctx.bundle`
      （`bundle["evolution_gate"]` / `bundle["evolution_gate_blocked"]`）。
    """
    src = (ROOT / BASE_REL).read_bytes().decode("utf-8-sig")
    tree = ast.parse(src)

    governance = None
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for fn in cls.body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and fn.name == "_step_prepare_governance":
                governance = fn
    assert governance is not None, "找不到 _step_prepare_governance"

    seg = ast.get_source_segment(src, governance) or ""
    assert "evolution_continuity_report" in seg, (
        "治理步没把闸门结论写进 ctx.evolution_continuity_report ⇒ 判了却没人知道"
    )

    # 第二处判定（七段剧本）的结论随 bundle 存进 ctx
    stores_bundle = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Attribute) and tgt.attr == "bundle":
                    stores_bundle = True
    assert stores_bundle, "管线没把 bundle 存进 ctx ⇒ 第二处闸门判定的结论无处可寻"


# ── 2. workflow 侧：bypass=True 时不阻断，但违规必须给模型看见 ──────


def test_the_gate_verdict_reaches_the_model_when_not_blocking():
    """bypass=True ⇒ 正常返回 bundle，且 blocking 违规**前置进上下文文本**。

    这是 workflow 原本的设计（`auto_novel_generation_workflow.py:422-434`）：
    不阻断章，但让模型看见「这一章有连续性违规」。
    """
    wf = _workflow()

    bundle = wf.prepare_chapter_generation(
        "n1", 3, "一、开篇切入点：…", allow_evolution_gate_bypass=True
    )

    assert bundle["evolution_gate_blocked"] is True, "blocking 违规没被标出来"
    assert bundle["evolution_gate"]["violations"][0]["type"] == "DEAD_CHARACTER_REAPPEARS"
    assert "=== EVOLUTION GATE ===" in bundle["context"], (
        "blocking 违规没进上下文文本 ⇒ 模型看不见它"
    )
    assert "已死亡角色" in bundle["context"], "违规消息没进上下文文本"
    assert "【T2 伏笔槽位】" in bundle["context"], "上下文包本身丢了"


def test_the_gate_still_blocks_when_the_caller_wants_it_to():
    """既有行为保留：bypass=False ⇒ 仍 raise（API 路径可以真正阻断）。"""
    wf = _workflow()

    with pytest.raises(RuntimeError) as exc:
        wf.prepare_chapter_generation(
            "n1", 3, "一、开篇切入点：…", allow_evolution_gate_bypass=False
        )

    assert "evolution_gate_blocked:" in str(exc.value)


def test_a_clean_gate_does_not_block():
    """没有 blocking 违规 ⇒ 不 raise（两种模式都一样）。"""
    clean = {
        "is_pass": True,
        "violations": [{"level": "warning", "type": "X", "message": "小问题"}],
        "required_continuations": [],
    }
    wf = _workflow(clean)

    bundle = wf.prepare_chapter_generation(
        "n1", 3, "一、开篇切入点：…", allow_evolution_gate_bypass=False
    )

    assert bundle["evolution_gate_blocked"] is False
    assert "【T2 伏笔槽位】" in bundle["context"], "上下文包本身丢了"


def test_the_gate_block_is_not_prepended_for_warning_only_violations():
    """只有 **blocking** 违规才该进上下文；普通 warning 不该塞进提示词。

    ⚠ 现状：`auto_novel_generation_workflow.py:422-434` 只要有报告就前置
    「=== EVOLUTION GATE ===」块（`len(gate_lines) > 1` 恒真）⇒ 干净闸门也会
    往每章上下文里塞一段噪声。**这是既有行为**（H-01a 之前就这样），本笔不改，
    但在这里钉住现状，避免有人误以为它是有意为之。
    """
    clean = {
        "is_pass": True,
        "violations": [],
        "required_continuations": [],
    }
    wf = _workflow(clean)

    bundle = wf.prepare_chapter_generation(
        "n1", 3, "一、开篇切入点：…", allow_evolution_gate_bypass=True
    )

    assert "=== EVOLUTION GATE ===" in bundle["context"], (
        "现状是「有报告就前置」；若哪天改成「只有 blocking 才前置」，"
        "请把本用例反过来断言（那是 P2 的清理项：干净闸门不该往上下文塞噪声）"
    )
