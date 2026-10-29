"""正文提示词的**硬事实块清单**（第 38 批建立、第 39 批扩成清单）。

⚠ 为什么要有这份清单
--------------------
「送哪几块进正文提示词」是这个系统的**语义决策**，不是随手加的：
每加一块 ⇒ 模型多看见一份既成事实 ⇒ 但**也可能**变成「剧情指令」而与
「以七段结构为唯一剧情骨架」打架。

⇒ ⇒ ⇒ 这份清单把两类块**分开钉住**：

* **必须送**（既成事实：不得违反 / 不得重复 / 不得推翻）
  = ``fact_lock`` / ``character_anchors`` / ``narrative_contract``
  / ``pending_foreshadowings``（第 38 批）/ ``completed_beats`` + ``revealed_clues``（第 39 批）
* **有意不送**（剧情指令：会与七段结构打架）
  = ``debt_due``（它的构造器写明「放入 **[MUST_RESOLVE]** 块，**强迫大模型在本章填坑**」）

⚠ 守卫纪律：
* 配置里写的槽位名**必须是分配器真有的** ⇒ 槽位名**从分配器真跑一遍拿**
  （不写死、不扫源码）⇒ 防「配置写了、分配器没有 ⇒ 那一块静默为空」
* 端到端：走**真实** ``ContextBuilder._build_hard_facts``（不重写它的形状）
"""
from __future__ import annotations

from application.engine.services.context_builder import ContextBuilder
from application.engine.services.prose_context_settings import (
    get_prose_context_runtime_settings,
)

#: **必须**送进正文提示词的硬事实块（既成事实）
REQUIRED_SLOTS = (
    "fact_lock",
    "character_anchors",
    "narrative_contract",
    "pending_foreshadowings",
    "completed_beats",
    "revealed_clues",
    # ⚠ 第 44 批：**近章原文**（N-1 章末节选「供本章开头承接」）
    #   ⇒ 此前模型只拿到**摘要** ⇒ 不知道上一章最后一句是什么、场景停在哪个动作上
    #   ⇒ 跨章衔接只能靠摘要猜 ⇒ 它是**已写出来的正文**（既成事实，不是剧情指令）
    "recent_chapters",
    # ⚠ 第 46 批：**反 AI 行为协议** —— 正文 prompt 的 system.md 里
    #   「独段比例上限见『节奏与段落法则』」是一条**悬空引用**（那份法则没进正文提示词）
    "anti_ai_protocol",
    # ⚠ 第 46 批：**故事线进度**（18 条主线/支线各自推进到哪）⇒ 既成事实
    "storyline_context",
)

#: **有意不送**的块（剧情指令 ⇒ 会与「七段结构是唯一剧情骨架」打架）
#: ⚠ 送进去之前要先改这块的语义（⇒ 变成既成事实），不能顺手加。
DELIBERATELY_EXCLUDED = {
    "debt_due": "叙事债务到期提醒是 **[MUST_RESOLVE]**（强迫本章填坑）⇒ 剧情指令，不是既成事实",
}


class _Slot:
    def __init__(self, name: str, content: str):
        self.name = name
        self.content = content


class _Allocation:
    def __init__(self, slots: dict):
        self.slots = slots


def _real_allocator_slots() -> set:
    from application.engine.services.context_budget_allocator import ContextBudgetAllocator

    allocator = ContextBudgetAllocator()
    return set(allocator._collect_all_slots("novel-1", 1, "章纲").keys())


def test_every_required_hard_fact_block_is_sent_to_the_prose_prompt():
    """⚠ 清单里的**每一块**都必须在 ``prose_context.slots`` 里。"""
    slots = set(get_prose_context_runtime_settings().slots)

    missing = [s for s in REQUIRED_SLOTS if s not in slots]
    assert not missing, f"这些硬事实块没进正文提示词：{missing}"


def test_every_configured_slot_must_exist_in_the_allocator():
    """⚠ 配置里写的槽位名必须是**分配器真有的**。

    ⚠ 否则那一块**静默为空**（builder 拿不到 slot 就跳过）⇒ 配置看着加了、其实没加。
    ⚠ 槽位名从**分配器真跑一遍**拿 ⇒ 不写死、也不扫源码。
    """
    real_slots = _real_allocator_slots()
    configured = set(get_prose_context_runtime_settings().slots)

    missing = configured - real_slots
    assert not missing, f"prose_context.slots 里这些槽位**分配器没有**（会静默为空）：{missing}"
    for required in REQUIRED_SLOTS:
        assert required in real_slots, f"分配器里没有 {required} 槽位了（被改名/删除？）"


def test_the_deliberately_excluded_blocks_stay_out():
    """⚠ 有意不送的块**不许**被顺手加进来（那是剧情指令，不是既成事实）。"""
    slots = set(get_prose_context_runtime_settings().slots)

    for name, why in DELIBERATELY_EXCLUDED.items():
        assert name not in slots, f"{name} 是有意不送的：{why}"


def test_the_hard_facts_block_really_carries_the_three_memory_locks():
    """⚠ 端到端：真实 builder 的硬事实块里**真的有**那三份记忆锁。"""
    pending_text = "【待回收伏笔（爽文GC精选）】\n- Ch5 重要 即将到期: 封蜡离案即融"
    beats_text = "【已完成节拍（以下事件已经发生过了，禁止在本章重复写一遍）】\n   [第5章] 沈砚青潜入地窖"
    clues_text = "【截至目前已知的线索（读者和主角已经知道的信息）】\n   [真相] [第5章] 销票口诀后半句真义"

    allocation = _Allocation(
        {
            "fact_lock": _Slot("绝对事实边界(FACT_LOCK)", "角色白名单：沈砚青"),
            "pending_foreshadowings": _Slot("待回收伏笔", pending_text),
            "completed_beats": _Slot("已完成节拍", beats_text),
            "revealed_clues": _Slot("已揭露线索(REVEALED_CLUES)", clues_text),
        }
    )

    hard_facts = ContextBuilder._build_hard_facts(allocation)

    assert "封蜡离案即融" in hard_facts, "硬事实块里没有待回收伏笔"
    assert "沈砚青潜入地窖" in hard_facts, "硬事实块里没有已完成节拍"
    assert "销票口诀后半句真义" in hard_facts, "硬事实块里没有已揭露线索"


def test_the_pending_slot_does_not_squeeze_out_the_fact_lock():
    """⚠ 加了块**不许**把原有的硬事实挤掉（每块 5000 字 ⇒ 装得下）。"""
    settings = get_prose_context_runtime_settings()

    assert settings.max_chars_per_slot >= 5000, (
        "每块上限被调小了 ⇒ 事实锁会被挤（生产书的事实锁要 4040 字）"
    )
    assert len(settings.slots) >= len(REQUIRED_SLOTS), "硬事实块少于清单 ⇒ 有一块被挤掉了"
