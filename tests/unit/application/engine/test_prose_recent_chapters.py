"""第 44 批：把「**近章原文**」送进正文提示词（模型要看得见上一章是怎么写的）。

背景（生产实测）
----------------
模型写下一章时，此前正文提示词里只有**摘要**（已完成节拍 629 字 / 已揭露线索 565 字）
⇒ **看不到上一章的原文** ⇒ 不知道上一章：

* 最后一句是什么、场景停在**哪个动作**上；
* 谁还在场、谁刚离开；
* 上一章的**语气与节奏**（写下一章要接得住）。

⇒ ⇒ 而「近章原文」这个块**早就在上下文包里**（T2 的 ``recent_chapters`` 槽位：
N-1 章首略览 + 章末节选「供本章开头承接」、N-2 章末节选、更早的章首预览）
⇒ ⇒ **不是新造数据源**，只是**没被选中**送进正文提示词
⇒ ⇒ 与第 29 批的 ``MemoryEngine`` 同族：**能力已实现，但从没被调用**。

⚠ 它是**已经写出来的正文**（既成事实）⇒ 不是剧情指令
  ⇒ 与「七段结构是唯一剧情骨架」不打架（与 ``debt_due`` 不同 ⇒ 后者仍钉住「有意不送」）。

⚠ 守卫纪律：走**真实链路**（真实分配器 → 真实 builder）；期望值写死字面量。
"""
from __future__ import annotations

from application.engine.services.context_builder import ContextBuilder
from application.engine.services.prose_context_settings import (
    get_prose_context_runtime_settings,
    prose_context_total_cap,
)

#: 第 44 批新增的块（必须送进正文提示词）
RECENT_CHAPTERS_SLOT = "recent_chapters"

#: 近章原文块里的**承接标记**（生成侧那个函数写的原文，不是我们自己编的）
BRIDGE_MARK = "【章末节选，供本章开头承接】"


class _Slot:
    def __init__(self, name: str, content: str):
        self.name = name
        self.content = content


class _Allocation:
    def __init__(self, slots: dict):
        self.slots = slots


def test_the_recent_chapters_slot_is_configured_for_the_prose_prompt():
    """⚠ 它**必须**在 ``prose_context.slots`` 里（否则它继续躺在包里没人看）。"""
    settings = get_prose_context_runtime_settings()

    assert RECENT_CHAPTERS_SLOT in settings.slots, (
        "近章原文没进正文提示词 ⇒ 模型写下一章看不到上一章原文"
    )


def test_the_previous_chapter_excerpt_reaches_the_prose_prompt():
    """⚠ 端到端：真实 builder 的硬事实块里**真的有**上一章的承接节选。"""
    allocation = _Allocation(
        {
            RECENT_CHAPTERS_SLOT: _Slot(
                "最近章节",
                "【最近章节】\n\n第 4 章：显字\n【章首略览】\n雨落了三夜。\n"
                f"{BRIDGE_MARK}\n她在纸背上按下最后一道墨痕。",
            )
        }
    )

    hard_facts = ContextBuilder._build_hard_facts(allocation)

    assert BRIDGE_MARK in hard_facts, "近章原文的承接节选没进硬事实块"
    assert "她在纸背上按下最后一道墨痕" in hard_facts, "上一章的收尾没进硬事实块"


def test_the_recent_chapters_slot_exists_in_the_real_allocator():
    """⚠ 槽位名必须是**分配器真有的**（不写死、不扫源码 ⇒ 真跑一遍拿）。"""
    from application.engine.services.context_budget_allocator import ContextBudgetAllocator

    real_slots = set(ContextBudgetAllocator()._collect_all_slots("novel-1", 1, "章纲").keys())

    assert RECENT_CHAPTERS_SLOT in real_slots, (
        "分配器里没有 recent_chapters 槽位了（被改名/删除？）"
    )


def test_adding_the_block_still_fits_the_total_cap():
    """⚠ 加了一块 ⇒ 整包总长仍要落在**总上限**内（否则 composer 会被迫腰斩）。"""
    settings = get_prose_context_runtime_settings()

    assert len(settings.slots) >= 7, "送进正文提示词的块少于 7 个"
    assert prose_context_total_cap(settings) >= 7 * 12000, (
        "整包总上限装不下 7 块 × 每块 12000 字"
    )
