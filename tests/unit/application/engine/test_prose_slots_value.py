"""第 46 批：把「反 AI 行为协议」与「故事线进度」送进正文提示词。

背景（生产实测 + 读代码）
--------------------------
正文 prompt 的 ``system.md`` 第 10 行写着：

    紧凑行文：……但**长短段交替**——铺陈用有机段落，暴击瞬间才用独段短句
    （**独段比例上限见「节奏与段落法则」**）。

⚠ 而**整份 system.md 里没有「节奏与段落法则」这一节、也没有任何比例数字**
（全文只命中这一处引用）⇒ ⇒ 那是一条**悬空引用** ⇒ 模型被指向一份它**看不到**的规则。

而那份规则**就在上下文包里**（``anti_ai_protocol`` 槽位：P1~P5 核心协议 +
R1~R7 替换策略 + 15% 独段上限）⇒ ⇒ 只是**它没进正文提示词**
（它在 T1 压缩层，而正文 prompt 只收 ``chapter_context`` = ``prose_context.slots``）。

⇒ ⇒ 本批：**把悬空引用补实**（送 ``anti_ai_protocol``），
并顺带送 ``storyline_context``（18 条故事线的当前进度 = 既成事实）。

⚠ 口径（与 ``debt_due`` 的区别，务必分清）：
* **不送**的是**与七段结构抢剧情决定权**的指令（``debt_due`` 是 ``[MUST_RESOLVE]``
  剧情指令 ⇒ 送进去会强迫本章填坑 ⇒ 打架 ⇒ 钉住不送）；
* **写作质量约束**（段落怎么写、禁什么套话）**不指定任何剧情**
  ⇒ 与正文 prompt 里已有的同向要求一致 ⇒ 不打架 ⇒ 可以送。

⚠ 守卫纪律：走**真实链路**；期望值写死；**不依赖 DB**（正文 prompt 与
反 AI 协议块都是纯文本来源 ⇒ 单元测试即可覆盖）。
"""
from __future__ import annotations

import pathlib
import re

from application.engine.services.context_builder import ContextBuilder
from application.engine.services.context_budget_allocator import ContextBudgetAllocator
from application.engine.services.prose_context_settings import (
    get_prose_context_runtime_settings,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PROSE_PROMPT_DIR = (
    REPO_ROOT / "infrastructure/ai/prompt_packages/nodes/chapter-prose-generation"
)

#: 本批新增的两个块
ANTI_AI_SLOT = "anti_ai_protocol"
STORYLINE_SLOT = "storyline_context"

#: 正文 prompt 里「见「X」」的**引用来源登记表**。
#:
#: ⚠ 新增一条引用时**必须**在这里登记它由哪个槽位提供 ⇒
#:   守卫会强制那个槽位**进正文提示词**（否则就是悬空引用）。
REFERENCE_SOURCES = {
    "节奏与段落法则": ANTI_AI_SLOT,
}

#: 重复内容判定用的最小长度（字符）
DUP_MIN_CHARS = 80


class _Slot:
    def __init__(self, name: str, content: str):
        self.name = name
        self.content = content


class _Allocation:
    def __init__(self, slots: dict):
        self.slots = slots


def _prose_prompt_text() -> str:
    parts = []
    for name in ("system.md", "user.md"):
        path = PROSE_PROMPT_DIR / name
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _anti_ai_block() -> str:
    """反 AI 协议块的内容（**不依赖 DB**：读的是规则文件）。"""
    allocator = ContextBudgetAllocator()
    return allocator._build_anti_ai_protocol_block("novel-1", 1)


def test_the_two_new_slots_are_sent_to_the_prose_prompt():
    """⚠ 这两块必须在 ``prose_context.slots`` 里。"""
    slots = set(get_prose_context_runtime_settings().slots)

    assert ANTI_AI_SLOT in slots, "反 AI 行为协议没进正文提示词 ⇒ 悬空引用补不实"
    assert STORYLINE_SLOT in slots, "故事线进度没进正文提示词"


def test_the_prose_prompt_has_no_unregistered_reference():
    """⚠ 正文 prompt 里的每条「见「X」」都必须**登记来源**。

    ⇒ 登记了才有机会把对应块送进来 ⇒ ⇒ 防「又写了个悬空引用」。
    """
    refs = set(re.findall(r"见[「『]([^」』]{1,20})[」』]", _prose_prompt_text()))

    unknown = sorted(refs - set(REFERENCE_SOURCES))
    assert not unknown, (
        f"正文 prompt 里有未登记来源的引用：{unknown} ⇒ "
        "要么把它对应的块送进 prose_context.slots，要么在 REFERENCE_SOURCES 里登记"
    )


def test_every_registered_reference_source_is_actually_sent():
    """⚠ 登记了来源 ⇒ 那个槽位**必须**被送进正文提示词。"""
    slots = set(get_prose_context_runtime_settings().slots)

    missing = sorted(
        f"{ref}←{slot}"
        for ref, slot in REFERENCE_SOURCES.items()
        if slot not in slots
    )
    assert not missing, f"这些引用登记了来源，但来源块没进正文提示词：{missing}"


def test_the_reference_source_really_contains_the_referenced_rule():
    """⚠ 源块里**真的有**那条被引用的法则（否则登记了也是悬空）。"""
    block = _anti_ai_block()
    assert block, "反 AI 协议块是空的 ⇒ 引用无源"

    for ref in REFERENCE_SOURCES:
        assert ref in block, f"反 AI 协议块里没有「{ref}」⇒ 正文 prompt 的引用仍然悬空"


def test_the_anti_ai_protocol_really_reaches_the_hard_facts_block():
    """⚠ 端到端：真实 builder 的硬事实块里**真的有**反 AI 协议。"""
    allocation = _Allocation(
        {
            ANTI_AI_SLOT: _Slot(
                "Anti-AI 行为协议(ANTI_AI_PROTOCOL)",
                "━━━ 核心协议（P1-P5 · 不可违反）━━━\n"
                "P4. 节奏与段落法则\n"
                "━━━ 替换策略（检测到B类模式时执行）━━━",
            )
        }
    )

    hard_facts = ContextBuilder._build_hard_facts(allocation)

    assert "P1-P5" in hard_facts, "反 AI 协议没进硬事实块"
    assert "替换策略" in hard_facts, "替换策略没进硬事实块"


def test_the_storyline_progress_really_reaches_the_hard_facts_block():
    """⚠ 端到端：故事线进度进硬事实块。"""
    allocation = _Allocation(
        {
            STORYLINE_SLOT: _Slot(
                "故事线上下文",
                "━━━ 故事线上下文（本章活跃）━━━\n● [主线] 「白票拒写」\n  当前进度：……",
            )
        }
    )

    hard_facts = ContextBuilder._build_hard_facts(allocation)

    assert "故事线上下文" in hard_facts, "故事线进度没进硬事实块"
    assert "白票拒写" in hard_facts, "故事线的具体条目没进硬事实块"


def test_a_block_that_duplicates_another_one_must_not_be_sent():
    """⚠ **防重复**：``worldbuilding_core`` 的三个维度**逐字**都在
    ``narrative_contract`` 里 ⇒ 送它是白送（同两段文字进 prompt 两次）。

    ⇒ ⇒ 钉住它**不许**进 ``prose_context.slots``。
    """
    slots = set(get_prose_context_runtime_settings().slots)

    assert "worldbuilding_core" not in slots, (
        "worldbuilding_core 与 narrative_contract 内容逐字重复 ⇒ 送它是白占额度"
    )


def test_the_hard_facts_block_has_no_duplicated_long_paragraph():
    """⚠ **防重复（通用）**：硬事实块里不许出现**两份完全相同的长段**。

    ⇒ 同一段文字进 prompt 两次 ⇒ 浪费额度 + 稀释重点。
    """
    settings = get_prose_context_runtime_settings()
    allocation = _Allocation(
        {
            "fact_lock": _Slot("绝对事实边界(FACT_LOCK)", "【身份锁死】\n   甲 = 男 / 25"),
            "narrative_contract": _Slot(
                "创作契约(NARRATIVE_CONTRACT)",
                "【世界观五维（作者确认）】\n" + "力量体系的说明。" * 12,
            ),
            "worldbuilding_core": _Slot(
                "世界规则",
                "【力量体系/科技树的描述】\n" + "力量体系的说明。" * 12,
            ),
        }
    )

    hard_facts = ContextBuilder._build_hard_facts(allocation)

    lines = [ln.strip() for ln in hard_facts.splitlines() if len(ln.strip()) >= DUP_MIN_CHARS]
    dup = {ln for ln in lines if lines.count(ln) > 1}
    assert not dup, f"硬事实块里有重复的长段（{len(dup)} 处）⇒ 白占额度并稀释重点"
