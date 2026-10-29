"""第 38 批：把「还有哪些伏笔挂着」送进**正文提示词**（伏笔回收链的前半段）。

⚠ 病灶（生产实测）
------------------
伏笔回收是**两段**：

* **前半段** = 模型**看见**待回收清单（正文提示词里有没有这份清单）
* **后半段** = 章后把回收**落库**（第 37 批修的那一段）

⇒ 第 37 批修的是后半段；前半段此前**整段缺失**：
生产实测 **3 个真实正文 invocation**（prompt 12.2k~12.9k 字符）里
「待回收伏笔」**一次都没出现** ⇒ 模型不知道要回收什么 ⇒ 回收无从发生。

⇒ 而分配器里**本来就有**这个槽位（``pending_foreshadowings``，
T1 / 1000 token / priority 70），且「七段剧本」阶段已经在用**同一份**文本
（``build_foreshadow_context``）⇒ 现在两个阶段看到**同一份**清单。

⚠ 守卫纪律：
* 配置里写的槽位名必须**是分配器真有的**（⇒ 防「配置写了、分配器没有 ⇒ 静默空转」）
  ⇒ 槽位名**从分配器真跑一遍拿**（不扫源码、不写死列表）
* 端到端：走**真实**的 ``ContextBuilder._build_hard_facts``（不重写它的形状）
"""
from __future__ import annotations

from application.engine.services.context_builder import ContextBuilder
from application.engine.services.prose_context_settings import (
    get_prose_context_runtime_settings,
)

PENDING_SLOT = "pending_foreshadowings"


class _Slot:
    def __init__(self, name: str, content: str):
        self.name = name
        self.content = content


class _Allocation:
    def __init__(self, slots: dict):
        self.slots = slots


def test_the_prose_context_must_carry_the_pending_foreshadows():
    """⚠ 正文提示词的硬事实块**必须**包含「待回收伏笔」（前半段）。"""
    settings = get_prose_context_runtime_settings()

    assert PENDING_SLOT in settings.slots, (
        f"prose_context.slots 里没有 {PENDING_SLOT} ⇒ 模型看不见还挂着哪些线"
    )


def test_every_configured_slot_must_exist_in_the_allocator():
    """⚠ 配置里写的槽位名必须是**分配器真有的**。

    ⚠ 否则那一块**静默为空**（builder 拿不到 slot 就跳过）⇒ 配置看着加了、其实没加。
    ⚠ 槽位名从**分配器真跑一遍**拿 ⇒ 不写死、也不扫源码。
    """
    from application.engine.services.context_budget_allocator import ContextBudgetAllocator

    allocator = ContextBudgetAllocator()
    real_slots = set(
        allocator._collect_all_slots("novel-1", 1, "章纲").keys()
    )

    configured = set(get_prose_context_runtime_settings().slots)

    missing = configured - real_slots
    assert not missing, f"prose_context.slots 里这些槽位**分配器没有**（会静默为空）：{missing}"
    assert PENDING_SLOT in real_slots, "分配器里没有待回收伏笔槽位了（槽位被改名/删除？）"


def test_the_hard_facts_block_really_carries_the_pending_foreshadows():
    """⚠ 端到端：真实 builder 的硬事实块里**真的有**那份清单。"""
    settings = get_prose_context_runtime_settings()
    pending_text = "待回收伏笔：\n   1. 封蜡离案即融，销票只剩一夜"

    allocation = _Allocation(
        {
            "fact_lock": _Slot("绝对事实边界(FACT_LOCK)", "角色白名单：沈砚青"),
            PENDING_SLOT: _Slot("待回收伏笔", pending_text),
        }
    )

    hard_facts = ContextBuilder._build_hard_facts(allocation)

    assert PENDING_SLOT in settings.slots
    assert "待回收伏笔" in hard_facts, "硬事实块里没有待回收伏笔"
    assert "封蜡离案即融" in hard_facts, "硬事实块里没有待回收伏笔的内容"


def test_the_pending_slot_does_not_squeeze_out_the_fact_lock():
    """⚠ 加了一块**不许**把原有的硬事实挤掉（每块 5000 字 ⇒ 装得下）。"""
    settings = get_prose_context_runtime_settings()

    assert settings.max_chars_per_slot >= 5000, (
        "每块上限被调小了 ⇒ 事实锁会被挤（生产书的事实锁要 4040 字）"
    )
    assert len(settings.slots) >= 4, "硬事实块少于 4 块 ⇒ 有一块被挤掉了"
