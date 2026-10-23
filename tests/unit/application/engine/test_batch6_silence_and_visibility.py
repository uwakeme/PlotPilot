"""第 6 批守卫：S-06 / S-07 / S-08 / G-04-b。

四条都是**独立**的，每条各有自己的「专属牙齿」（上一批的教训：
守卫引用实现常量、载荷与变异不独立、判据落在注释上 ⇒ 都会假绿）。

S-06 · 截断必须**说明**
----------------------
改造前：``context_budget_policy.py`` 4 处 + ``context_assembler.py`` 1 处
用裸的 ``"..."`` ⇒ 模型看到「内容被砍掉了」，但不知道砍的是什么。
被砍的通常是 ``fact_lock`` —— **不得违反的既成事实**那块。

S-07 · 降级路径也要拿到硬事实，取不到时**明说**
------------------------------------------------
改造前：降级路径只调 ``build_context()``（返回字符串、分配结果被丢掉）
⇒ ``ctx.hard_facts`` 保持空串 ⇒ 正文提示词里的「【硬事实（不得违反的既成事实）】」
是一节**空的**，而同模板硬约束第 7 条还在要求「必须与之一致」
⇒ 模型看到一节空的「必须遵守」。

S-08 · ``chapter.context`` 的 display_name 与实际内容相符
----------------------------------------------------------
标签原写「章节上下文包（T0–T3）」，但该槽位装的是**硬事实三块**。

G-04-b · ``assemble_context`` 死代码已删
-----------------------------------------
「两套别都留着」：读回装配那条路零生产调用方 ⇒ 删掉；
但 ``update_emotion_ledger`` 是**活的**（写路径）⇒ 不许整类删。
"""
from __future__ import annotations

import ast
from pathlib import Path

from application.engine.services.context_budget_models import ContextSlot
from application.engine.services.context_budget_policy import (
    TRUNCATION_MARKER,
    allocate_tier,
    truncate_t0_slots,
)

ROOT = Path(__file__).resolve().parents[4]


def _slot(name: str, content: str, tokens: int, **kw) -> ContextSlot:
    return ContextSlot(
        name=name,
        content=content,
        tokens=tokens,
        tier=kw.pop("tier", None),
        priority=kw.pop("priority", 50),
        max_tokens=kw.pop("max_tokens", 0),
        min_tokens=kw.pop("min_tokens", 0),
    )


# ── S-06 · 截断要说明 ───────────────────────────────────────────────

def test_truncation_marker_is_shared_and_explicit():
    """截断标记必须是**一句话**（不是裸省略号），且是单一来源。"""
    assert TRUNCATION_MARKER.strip(), "截断标记不能为空"
    assert "..." not in TRUNCATION_MARKER, "截断标记不许用裸省略号"
    # 给模型看的话：得说清「被截断」和「只是部分内容」
    assert "截断" in TRUNCATION_MARKER
    assert "部分" in TRUNCATION_MARKER


def test_truncate_t0_slots_marks_the_truncated_slot():
    """T0 被截断的槽位必须带上说明标记。"""
    slots = {
        "a": _slot("A", "x" * 100, 100),
        "b": _slot("B", "y" * 5000, 5000),
    }
    truncate_t0_slots(slots, 200, chars_per_token_zh=1.0)

    assert TRUNCATION_MARKER in slots["b"].content, "被截断的槽位没有说明标记"
    assert not slots["b"].content.endswith("..."), "还是裸省略号"


def test_allocate_tier_marks_every_truncated_slot():
    """按权重压缩 / 压缩 / 截断 —— 三条路都要带说明。"""
    slots = {
        "a": _slot("A", "x" * 500, 500, max_tokens=100),
        "b": _slot("B", "y" * 5000, 5000, max_tokens=5000),
    }
    log: list[str] = []
    allocate_tier(slots, 200, log, chars_per_token_zh=1.0)

    for name in ("a", "b"):
        content = slots[name].content
        if content:  # 非空即说明被截断过
            assert TRUNCATION_MARKER in content, f"{name} 被截断却没有说明标记"
            assert not content.endswith("..."), f"{name} 还是裸省略号"


def test_no_bare_ellipsis_is_produced_by_the_budget_policy():
    """**源码级**守卫：预算策略里不许再出现 ``+ "..."`` 这种裸截断。

    ⚠ 只查**代码**（剥掉注释与 docstring）——
    注释里可以解释历史（我自己的注释里就写了 ``"..."``），
    但代码里不许再**产生**裸省略号。
    """
    path = ROOT / "application/engine/services/context_budget_policy.py"
    code = _strip_comments(path.read_text(encoding="utf-8"))

    assert '"..."' not in code, "预算策略里还有裸省略号截断"
    assert "'...'" not in code, "预算策略里还有裸省略号截断"


def test_context_assembler_no_longer_produces_a_bare_ellipsis():
    """同源的第二份实现（``context_assembler``）也不许裸省略号。"""
    path = ROOT / "application/engine/services/context_assembler.py"
    code = _strip_comments(path.read_text(encoding="utf-8"))

    assert '+ "..."' not in code, "context_assembler 里还有裸省略号截断"


# ── S-07 · 降级路径的硬事实 ──────────────────────────────────────────

def test_degraded_path_gets_hard_facts_too():
    """降级路径（builder 兜底）必须**也能拿到硬事实**。

    改造前它只调 ``build_context()``（返回字符串、分配结果被丢掉）
    ⇒ ``ctx.hard_facts`` 恒空 ⇒ 正文提示词里的「硬事实」是一节空的。

    ⚠ 判据要盯**赋值语句本身**，不是「文件里出现过 ctx.hard_facts」——
    否则把 ``ctx.hard_facts = degraded["hard_facts"]`` 改成
    ``ctx.hard_facts = ""`` 照样能过（假绿）。
    """
    from application.engine.services.context_builder import ContextBuilder

    assert hasattr(ContextBuilder, "build_context_with_hard_facts"), (
        "没有「一次分配、两个出口」的方法 ⇒ 降级路径拿不到硬事实"
    )

    code = _strip_comments((ROOT / "engine/pipeline/base.py").read_text(encoding="utf-8"))
    assert "build_context_with_hard_facts(" in code, (
        "降级路径没走 build_context_with_hard_facts ⇒ 硬事实仍然留空"
    )
    # ⚠ 必须是**从 degraded 里取**，不是写死空串
    assert 'ctx.hard_facts = degraded["hard_facts"]' in code, (
        "降级路径没有把 degraded 里的 hard_facts 赋给 ctx.hard_facts"
    )


def test_empty_hard_facts_is_stated_explicitly():
    """取不到硬事实时**必须明说**（不留一节空的「必须遵守」）。

    ⚠ 这条断言的是**文案的语义要点**，不是逐字匹配
    （逐字匹配会在措辞微调时假红）。
    """
    from engine.pipeline.prose_composer import (
        ChapterProseInvocationComposer,
        ProseCompositionRequest,
    )
    from application.engine.services.prose_context_settings import (
        ProseContextRuntimeSettings,
    )
    import engine.pipeline.prose_composer as composer_module

    original = composer_module.get_prose_context_runtime_settings
    composer_module.get_prose_context_runtime_settings = lambda: (
        ProseContextRuntimeSettings(
            enabled=True, slots=("fact_lock",), max_chars_per_slot=2000
        )
    )
    try:
        request = ProseCompositionRequest(
            novel_id="n1",
            chapter_number=3,
            outline="一、开篇切入点：…",
            context_text="整包上下文（不该进正文提示词）",
            hard_facts="",
            metadata={"continuity_context": "暂无近3章承接摘要。"},
        )
        variables = ChapterProseInvocationComposer()._build_variables(request)
    finally:
        composer_module.get_prose_context_runtime_settings = original

    context = variables["chapter_context"]
    assert context.strip(), "硬事实为空时必须有说明"
    assert "未取到" in context, f"说明里没说清「没取到」: {context!r}"
    # 整包上下文不许被当兜底
    assert "整包上下文" not in context


# ── S-08 · display_name 与实际相符 ───────────────────────────────────

def test_chapter_context_display_name_matches_the_actual_content():
    """标签必须说清这个槽位装的是什么（硬事实三块，不是整包上下文）。"""
    from application.ai_invocation.contracts.chapter_prose_generation import (
        chapter_prose_input_bindings,
    )

    binding = next(
        b for b in chapter_prose_input_bindings() if b.alias == "chapter_context"
    )
    assert "整包上下文" not in binding.display_name, (
        f"标签还在说「整包上下文」，但这个槽位装的是硬事实三块: {binding.display_name!r}"
    )
    assert "硬事实" in binding.display_name, (
        f"标签没说清是硬事实: {binding.display_name!r}"
    )


# ── G-04-b · 死代码已删 ──────────────────────────────────────────────

def test_assemble_context_is_gone():
    """读回装配那条路已删（零生产调用方 ⇒ 不留两套）。"""
    for rel in (
        "engine/core/services/memory_orchestrator.py",
        "engine/infrastructure/memory/memory_orchestrator_impl.py",
    ):
        code = _strip_comments((ROOT / rel).read_text(encoding="utf-8"))
        assert "def assemble_context" not in code, f"{rel} 里还有 assemble_context"
        assert "TokenBudget" not in code, f"{rel} 里还有 TokenBudget"
        assert "ContextAssembly" not in code, f"{rel} 里还有 ContextAssembly"


def test_the_live_half_of_the_memory_orchestrator_is_kept():
    """⚠ **不许整类删**：写路径（情绪账本抽取）是**活的**。

    这条是 G-04-b 的**反向守卫** ——
    删死代码时很容易连着把活代码删掉。

    ⚠ 判据是**源码里的方法名**，不是 ``hasattr`` ——
    把方法**改名**（``update_emotion_ledger`` → 别的）时 ``hasattr`` 仍为 True
    ⇒ hasattr 版的守卫是**假绿**（踩过一次）。
    """
    code = _strip_comments(
        (ROOT / "engine/infrastructure/memory/memory_orchestrator_impl.py").read_text(
            encoding="utf-8"
        )
    )
    for name in ("update_emotion_ledger", "restore_state", "manage_foreshadow"):
        assert f"def {name}(" in code, f"活方法 {name} 被误删或改名了"
    assert "def _load_emotion_ledger_json(" in code


def test_no_production_caller_references_the_removed_api():
    """全仓（除测试）不许再**调用**已删的 API。

    ⚠ 判据是**代码**（剥掉注释与 docstring）——
    docstring 里提到「原先有 assemble_context」是**历史说明**，不是调用。
    （踩过一次：用 ``git grep`` 查 ⇒ 命中的是我自己写的说明文字。）
    """
    for rel in (
        "engine/core/services/memory_orchestrator.py",
        "engine/infrastructure/memory/memory_orchestrator_impl.py",
        "application/engine/services/context_budget_allocator.py",
        "engine/runtime/runner.py",
        "engine/pipeline/base.py",
    ):
        code = _strip_comments((ROOT / rel).read_text(encoding="utf-8"))
        assert "assemble_context(" not in code, (
            f"{rel} 里还在调用 assemble_context"
        )
        assert "_assemble_t0(" not in code, f"{rel} 里还在调用 _assemble_t0"
        assert "_apply_budget_pressure(" not in code, (
            f"{rel} 里还在调用 _apply_budget_pressure"
        )


# ── 工具 ─────────────────────────────────────────────────────────────

def _strip_comments(source: str) -> str:
    """剥掉**注释**与 docstring，只留代码。

    ⚠ 这是本项目的老坑（第三次踩）：判据落在注释/docstring 上 ⇒ 假红/假绿。
    ⇒ 必须真的把注释剥掉：
    - ``#`` 行注释（含 ``#:`` 这种块注释的续行）；
    - 三引号包裹的 docstring；
    - （JS 的 ``//`` 与 ``/* */`` 也一并剥，跨语言文件用得上。）
    """
    import re

    text = source
    # docstring（先剥，免得里面的 # 被当注释、影响后续判断）
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    # 块注释（JS 风格）
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    # 行注释
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"#[^\n]*", "", text)
    return text


def _tree_is_valid(path: Path) -> bool:
    try:
        ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return False
    return True
