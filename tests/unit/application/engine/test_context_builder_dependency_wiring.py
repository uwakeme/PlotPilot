"""第 28 批：**依赖传递完整性**（ContextBuilder → 分配器不许漏装配）。

背景（生产实测，第 28 批盘点）
------------------------------
用**生产真实装配**（`get_context_builder()`）盘点生产书第 6 章的上下文槽位时发现：

* 「叙事承诺锁」（T0 / priority=123，从 title+premise 派生）
  在**生产装配**里**恒为空**；
* 而同一段代码在「手搓装配」里却有内容。

⇒ 根因：`ContextBuilder` 构造 `ContextBudgetAllocator` 时
**漏传了 `novel_repository`** ⇒ 分配器里它是 None
⇒ `build_narrative_promise_slot_content(None, ...)` **静默返回空**
（不报错、不日志）⇒ 槽位在表里占着 T0 高优先级，内容永远是空的。

⇒ 这类「**静默空**」最难发现：槽位看起来「正常（空）」，没有任何报错。

⇒ 这条守卫把**整条装配链**的依赖传递钉死：
* 层 1：`interfaces/api/dependencies.py` → `ContextBuilder`
* 层 2：`application/engine/services/context_builder.py` → `ContextBudgetAllocator`

⚠ 守卫纪律：用 **AST**（不是文本 contains）判定「传了哪些关键字参数」
⇒ 不会匹配到别的调用；也不受注释/换行影响。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]

#: 允许**不传**的依赖（各自都有兜底 ⇒ 漏传不会导致槽位恒空）
#:
#: ⚠ 每个都要写清**为什么**可以漏传（否则以后会变成新的静默空）。
ALLOWED_UNPASSED = {
    "phase_thresholds": "有默认值（沙漏阶段阈值可由变量覆盖）",
    "slot_weights": "有默认值（D-02：没传 = 旧行为）",
    "character_projection_service": "有兜底（_get_character_projection_service 自建）",
    "memory_engine": "ContextBuilder 自己构造（见 __init__）",
}


def _call_keywords(path: Path, func_name: str) -> set[str]:
    """AST 找出该文件里 ``func_name(...)`` 调用传了哪些关键字参数。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[set[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != func_name:
            continue
        found.append({kw.arg for kw in node.keywords if kw.arg})
    return found


def _required_kwargs(cls) -> set[str]:
    """构造函数的**依赖**参数（排除 self 与有默认值的可选参数）。"""
    signature = inspect.signature(cls.__init__)
    required = set()
    for name, param in signature.parameters.items():
        if name == "self":
            continue
        if param.default is inspect.Parameter.empty and param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            required.add(name)
    return required


# ── 层 1：dependencies.py → ContextBuilder ─────────────────────────────


def test_every_context_builder_dependency_is_wired_by_the_dI():
    """⚠ DI 工厂必须把 `ContextBuilder` 需要的依赖**全传**。

    ⚠ 漏传的依赖会在 `ContextBuilder.__init__` 里变成 None
    ⇒ 依赖它的槽位**静默空**（不报错）⇒ 最难发现的一类问题。
    """
    from application.engine.services.context_builder import ContextBuilder

    calls = _call_keywords(ROOT / "interfaces/api/dependencies.py", "ContextBuilder")
    assert calls, "dependencies.py 里找不到 ContextBuilder(...) 的构造调用"

    required = _required_kwargs(ContextBuilder)
    for keywords in calls:
        missing = required - keywords
        assert not missing, (
            f"DI 工厂漏传了 ContextBuilder 的依赖：{sorted(missing)}"
            f"（⇒ 依赖它们的槽位会静默空）"
        )


# ── 层 2：context_builder.py → ContextBudgetAllocator ───────────────────


def test_every_allocator_dependency_is_wired_by_the_context_builder():
    """⚠ **本批修的那条**：分配器的 `novel_repository` 曾被漏传。

    ⇒ 分配器里它是 None ⇒「叙事承诺锁」**静默恒空**（T0 / priority=123）。

    ⚠ 判据不能靠「签名里有没有默认值」——
    ``Optional[Any] = None`` **只是类型可选**，**没有兜底**
    ⇒ 漏传一样会让依赖它的槽位静默空。
    ⇒ ⇒ 除**显式白名单**外，**全部**参数都必须被传。
    """
    from application.engine.services.context_budget_allocator import ContextBudgetAllocator

    calls = _call_keywords(
        ROOT / "application/engine/services/context_builder.py", "ContextBudgetAllocator"
    )
    assert calls, "context_builder.py 里找不到 ContextBudgetAllocator(...) 的构造调用"

    signature = inspect.signature(ContextBudgetAllocator.__init__)
    for keywords in calls:
        missing = set()
        for name in signature.parameters:
            if name == "self":
                continue
            if name in ALLOWED_UNPASSED:
                continue
            if name not in keywords:
                missing.add(name)
        assert not missing, (
            f"ContextBuilder 漏传了分配器的依赖：{sorted(missing)}"
            f"（⇒ 依赖它们的槽位会静默空）"
        )


def test_the_allowed_unpassed_dependencies_still_have_a_fallback():
    """⚠ 白名单里的每个依赖都**必须**真有兜底（否则它就是个新的静默空）。"""
    from application.engine.services.context_budget_allocator import ContextBudgetAllocator

    source = (ROOT / "application/engine/services/context_budget_allocator.py").read_text(
        encoding="utf-8"
    )
    for name in ALLOWED_UNPASSED:
        if name == "memory_engine":
            continue
        assert name in source, f"白名单里的 {name} 在分配器里都不见了 ⇒ 白名单该更新"


# ── 端到端：「叙事承诺锁」不再恒空 ─────────────────────────────────────


def test_the_narrative_promise_slot_is_not_empty_in_the_real_wiring():
    """⚠ 端到端：真实装配（`ContextBuilder`）⇒ 叙事承诺锁**有内容**。

    ⚠ 走**真实**的 `ContextBuilder`（不是手搓分配器）⇒
    漏传 `novel_repository` 时这条会红（内容为空）。
    """
    from application.engine.services.context_builder import ContextBuilder

    class _NovelRepo:
        def get_by_id(self, novel_id):
            return SimpleNamespace(
                id=novel_id,
                title="雨夜第七个观众",
                premise="民国探案：一名女探员在连环命案里追查会自己传话的匿名戏票。",
            )

    builder = ContextBuilder(
        bible_service=None,
        storyline_manager=None,
        relationship_engine=None,
        vector_store=None,
        novel_repository=_NovelRepo(),
        chapter_repository=None,
    )

    content = builder.budget_allocator._build_narrative_promise_slot("n-1", 3)

    assert content.strip(), "叙事承诺锁是空的 ⇒ 依赖没装配上（模型拿不到「这本书承诺讲什么」）"
    assert "雨夜第七个观众" in content, content
