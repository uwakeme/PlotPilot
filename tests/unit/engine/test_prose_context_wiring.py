"""X1：正文提示词必须看得见「硬事实」，且只能看见该看见的。

## 问题（本批核实发现，主 agent 用运行时探针确认）

`engine/pipeline/prose_composer.py` 的 `_build_variables` 曾经这样写：

```python
continuity_context = (str(metadata.get("continuity_context") or "").strip()
                      or str(request.context_text or "").strip())
```

两个**不同的东西**挤在同一个变量槽里：

- `metadata["continuity_context"]` = 「近3章承接摘要」（continuity ledger
  空时返回「暂无近3章承接摘要。」⇒ **恒非空**）；
- `request.context_text` = T0–T3 上下文包（角色状态锚点 / 叙事契约（死亡锁·
  白名单·身份锁）/ 伏笔槽位 / 幕摘要 / 最近章节 / 向量召回）。

⇒ `or` 的后半支**永不触发** ⇒ 生产正文提示词里**没有硬事实**
⇒ 模型写正文时看不见死亡锁、角色当前状态、文风公约。

## 但「整包倾倒」是错的

正文提示词的**三变量设计是刻意的**（2026-06-08 `444f5e0d`「优化生文流程 & 提示词」：
同一次提交里改了 composer、加了「只吃核心变量」的两个守卫、改了 prompt 模板的
「以【正文细纲】中的七段结构为唯一剧情骨架」硬约束）。
而且整包很贵：实测上下文包 6.9k~19.4k token/章，其中 `fact_lock`
在 529 章那本书里就有 20365 字（≈12.7k token，它自己在无上限地长）。

⇒ 本笔送的是**槽位级的「硬事实」小块**（默认：死亡锁 / 角色锚点 / 创作契约），
每块带上限 ⇒ ~3-4k token/章，且不与「七段结构是唯一剧情骨架」打架：
**硬事实是「不得违反的既成事实」，不是剧情指令。**

## 改法

- `context_builder.build_structured_context` 多返回一个 `hard_facts`
  （按 `config/performance.yaml` 的 `prose_context.slots` 选槽位，每块带上限）；
- 工作流把它放进 bundle ⇒ 管线放进 `ctx.hard_facts` ⇒ composer 送成
  **独立变量** `chapter_context`；
- `continuity_context` 回归本义（只放近3章承接摘要）。

本文件锁住这几条。
"""
from __future__ import annotations

import pathlib

import pytest

from application.engine.services.context_builder import ContextBuilder
from engine.pipeline.prose_composer import (
    ChapterProseInvocationComposer,
    ProseCompositionRequest,
)


ROOT = pathlib.Path(__file__).resolve()
for _parent in ROOT.parents:
    if (_parent / ".git").exists() or (_parent / "pytest.ini").exists():
        ROOT = _parent
        break

NODE_REL = "infrastructure/ai/prompt_packages/nodes/chapter-prose-generation"
LOCK = "=== 绝对事实边界(FACT_LOCK) ===\n陈小已死"
ANCHOR = "=== 角色锚点 ===\n陈小：左臂骨折"


def _request(**overrides) -> ProseCompositionRequest:
    base = dict(
        novel_id="n1",
        chapter_number=3,
        outline="一、开篇切入点：…",
        context_text="整包上下文（不该进正文提示词）",
        hard_facts=f"{LOCK}\n\n{ANCHOR}",
        metadata={"continuity_context": "暂无近3章承接摘要。"},
    )
    base.update(overrides)
    return ProseCompositionRequest(**base)


def _variables(monkeypatch, *, enabled=True, slots=("fact_lock", "character_anchors"),
               max_chars_per_slot=2000, **overrides):
    from application.engine.services.prose_context_settings import (
        ProseContextRuntimeSettings,
    )

    monkeypatch.setattr(
        "engine.pipeline.prose_composer.get_prose_context_runtime_settings",
        lambda: ProseContextRuntimeSettings(
            enabled=enabled, slots=tuple(slots), max_chars_per_slot=max_chars_per_slot
        ),
    )
    return ChapterProseInvocationComposer()._build_variables(_request(**overrides))


# ── 1. 硬事实进自己的变量，不再被 continuity_context 顶掉 ───────────


def test_the_hard_facts_get_their_own_variable(monkeypatch):
    vars_ = _variables(monkeypatch)

    assert "chapter_context" in vars_, "硬事实没有独立变量"
    assert LOCK in vars_["chapter_context"]
    assert vars_["continuity_context"] == "暂无近3章承接摘要。", (
        "continuity_context 必须只放近3章承接摘要"
    )


def test_the_whole_context_bundle_never_reaches_the_prose_prompt(monkeypatch):
    """**核心回归**：整包上下文不得进正文提示词。

    整包会让提示词涨 7k~20k token/章，且与「七段结构是唯一剧情骨架」打架
    （实测 fact_lock 在长篇上能到 20365 字）。

    ⚠ 样本要有鉴别力：**必须用「硬事实为空」的请求**。
    若硬事实非空，整包那条分支根本不会触发 ⇒ 这个用例就抓不到「整包被当兜底」。
    """
    vars_ = _variables(monkeypatch, hard_facts="")

    assert vars_["chapter_context"] == "", "整包上下文被当成了硬事实的兜底"
    assert "整包上下文" not in str(vars_), "整包上下文漏进了正文提示词"


def test_the_whole_context_bundle_never_leaks_when_hard_facts_are_present(monkeypatch):
    """硬事实非空时也一样：整包不许混进来。"""
    vars_ = _variables(monkeypatch)

    assert "整包上下文" not in str(vars_), "整包上下文漏进了正文提示词"


def test_the_hard_facts_are_not_swallowed_by_a_non_empty_continuity_summary(monkeypatch):
    """metadata 那一支恒非空，正是它把硬事实顶掉了。"""
    vars_ = _variables(
        monkeypatch, metadata={"continuity_context": "第2章：陈小左臂骨折。"}
    )

    assert LOCK in vars_["chapter_context"], (
        "continuity_context 非空时硬事实就消失了 —— 这就是原来的断链"
    )
    assert vars_["continuity_context"] == "第2章：陈小左臂骨折。"


def test_an_empty_continuity_summary_does_not_get_the_hard_facts(monkeypatch):
    """两个槽彻底分开：continuity 空了就空着，不拿硬事实顶替。"""
    vars_ = _variables(monkeypatch, metadata={})

    assert vars_["continuity_context"] == ""
    assert LOCK in vars_["chapter_context"]


# ── 2. 开关 ───────────────────────────────────────────────────────


def test_the_switch_can_turn_the_hard_facts_off(monkeypatch):
    vars_ = _variables(monkeypatch, enabled=False)

    assert vars_["chapter_context"] == ""


# ── 3. 槽位级选取 + 每块上限（builder 层） ───────────────────────


class _Slot:
    def __init__(self, name, content):
        self.name = name
        self.content = content
        self.tier = None


class _Allocation:
    def __init__(self, slots):
        self.slots = slots


def test_the_builder_picks_only_the_configured_slots(monkeypatch):
    """只送配置里点名的槽位 ⇒ 不送「最近章节正文」「向量召回」那类大块。"""
    from application.engine.services.prose_context_settings import (
        ProseContextRuntimeSettings,
    )

    monkeypatch.setattr(
        "application.engine.services.prose_context_settings.get_prose_context_runtime_settings",
        lambda: ProseContextRuntimeSettings(
            enabled=True,
            slots=("fact_lock", "character_anchors"),
            max_chars_per_slot=2000,
        ),
    )
    allocation = _Allocation(
        {
            "fact_lock": _Slot("绝对事实边界(FACT_LOCK)", "陈小已死"),
            "character_anchors": _Slot("角色锚点", "陈小：左臂骨折"),
            "recent_chapters": _Slot("最近章节", "很长很长的前文……"),
        }
    )

    text = ContextBuilder._build_hard_facts(allocation)

    assert "陈小已死" in text
    assert "左臂骨折" in text
    assert "很长很长的前文" not in text, "没点名的槽位不该进来"


def test_the_builder_caps_each_slot_and_says_so(monkeypatch):
    """每块带上限；截断**要说明**，不静默。"""
    from application.engine.services.prose_context_settings import (
        ProseContextRuntimeSettings,
    )

    monkeypatch.setattr(
        "application.engine.services.prose_context_settings.get_prose_context_runtime_settings",
        lambda: ProseContextRuntimeSettings(
            enabled=True, slots=("fact_lock",), max_chars_per_slot=20
        ),
    )
    allocation = _Allocation({"fact_lock": _Slot("绝对事实边界(FACT_LOCK)", "陈小已死" * 50)})

    text = ContextBuilder._build_hard_facts(allocation)

    assert len(text) < 200
    assert "截断" in text, "截断了要说明，不能静默"


def test_the_builder_returns_nothing_when_the_switch_is_off(monkeypatch):
    from application.engine.services.prose_context_settings import (
        ProseContextRuntimeSettings,
    )

    monkeypatch.setattr(
        "application.engine.services.prose_context_settings.get_prose_context_runtime_settings",
        lambda: ProseContextRuntimeSettings(enabled=False),
    )
    allocation = _Allocation({"fact_lock": _Slot("绝对事实边界(FACT_LOCK)", "陈小已死")})

    assert ContextBuilder._build_hard_facts(allocation) == ""


# ── 4. 契约与 CPMS 节点：变量得真的被声明并渲染 ───────────────────


def test_the_invocation_contract_declares_the_new_variable():
    from application.ai_invocation.contracts.chapter_prose_generation import (
        chapter_prose_input_bindings,
    )

    aliases = [b.alias for b in chapter_prose_input_bindings()]
    assert "chapter_context" in aliases, (
        "chapter_context 没进 input bindings ⇒ 面板里看不到、也不会被解析"
    )


def test_the_cpms_node_declares_the_variable():
    pkg = (ROOT / NODE_REL / "package.yaml").read_bytes().decode("utf-8")
    assert "name: chapter_context" in pkg, "CPMS 节点没声明 chapter_context 变量"


def test_the_cpms_template_renders_the_variable():
    tpl = (ROOT / NODE_REL / "user.md").read_bytes().decode("utf-8")
    assert "{chapter_context}" in tpl, "模板没渲染 {chapter_context} ⇒ 变量到不了提示词"
    assert "硬事实" in tpl, "模板没说明这块是什么（模型会当成剧情指令）"


def test_the_pipeline_carries_the_hard_facts_from_the_bundle_to_the_composer():
    """**接线守卫**：bundle → ctx.hard_facts → composer 请求，三段都得在。

    漏掉任何一段，硬事实就停在半路（bundle 里有、ctx 上没有、或请求里没传），
    而 composer 侧的用例都是直接构造 request 的 ⇒ 抓不到。
    """
    import ast

    src = (ROOT / "engine/pipeline/base.py").read_bytes().decode("utf-8-sig")
    tree = ast.parse(src)

    assigns_hard_facts = False
    passes_hard_facts = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Attribute) and tgt.attr == "hard_facts":
                    assigns_hard_facts = True
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "hard_facts":
                    passes_hard_facts = True

    assert assigns_hard_facts, "管线没把 bundle 里的 hard_facts 放进 ctx"
    assert passes_hard_facts, "管线没把 hard_facts 传给 composer ⇒ 它停在半路"


def test_the_rendered_prompt_actually_contains_the_hard_facts():
    """端到端：渲染出来的提示词里真的有硬事实。"""
    from infrastructure.ai.prompt_manager import get_prompt_manager
    from infrastructure.ai.prompt_registry import get_prompt_registry

    # 幂等；应用启动时本来就会 seed（提示词包是种子源，改 YAML 必须 bump 版本才生效）
    get_prompt_manager().ensure_seeded()

    prompt = get_prompt_registry().render_to_prompt(
        "chapter-prose-generation",
        {
            "target_words": 2500,
            "chapter_outline": "一、开篇切入点：…",
            "continuity_context": "暂无近3章承接摘要。",
            "chapter_context": LOCK,
        },
    )

    assert prompt is not None, "CPMS 节点渲染失败"
    assert LOCK in prompt.user, "硬事实没进渲染后的提示词"
    assert "【硬事实（不得违反的既成事实）】" in prompt.user, "硬事实段落没渲染"
