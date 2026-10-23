"""G-04-a：情绪账本注入要有**独立口径**，且裁剪不许静默。

背景
----
第 4 批把情绪账本接进了生产上下文（T1 槽），但留下两个缺口：

1. **注入长度无界**：``to_t0_section()`` 里 wounds/boons/power_shifts 各取最近
   3/3/2 条，而 **open_loops 全部打印**
   （``add_open_loop`` 的 docstring 明说「未回收的悬念不能因为上限而消失」）
   ⇒ 注入长度随未回收悬念**线性增长**；
2. **唯一的兜底是槽位的 ``max_tokens=400``** ⇒ 到点被砍一刀，
   而砍掉的正是「已埋但还没回收的线」⇒ **静默丢失**。

⇒ 第 7 批 S-06 之后，裸 ``...`` 换成了带说明的标记
（但那只说明「这段被截了」，**不说明丢了多少条悬念**）。

本笔的处置
----------
**账本自己定口径**（这才是「独立预算口径」的落点）：

- 新增 ``INJECT_ITEMS_PER_CATEGORY``（**注入侧**每类最多几条）；
- ``to_t0_section()`` 按它裁剪，**保留最新**；
- 裁剪时**明说**「共 N 条，此处只列最近 M 条，另有 K 条未列」；
- 槽位 ``max_tokens`` 400 → 1000（口径才是约束，槽位上限只作兜底）。

⚠ 与写侧上限 ``MAX_ITEMS_PER_CATEGORY``（=20）的区别：
那是**账本里最多存多少条**；这个是**一次给模型看多少条**。
⇒ 写得进 ≠ 一次全给。
"""
from __future__ import annotations

from pathlib import Path

from engine.core.value_objects.emotion_ledger import (
    INJECT_ITEMS_PER_CATEGORY,
    MAX_ITEMS_PER_CATEGORY,
    EmotionalBoon,
    EmotionalWound,
    EmotionLedger,
    OpenLoop,
    PowerShift,
)

ROOT = Path(__file__).resolve().parents[3]
ALLOCATOR = ROOT / "application/engine/services/context_budget_allocator.py"


def _ledger(
    *,
    wounds: int = 0,
    boons: int = 0,
    shifts: int = 0,
    loops: int = 0,
) -> EmotionLedger:
    return EmotionLedger(
        wounds=tuple(
            EmotionalWound(description=f"创伤{i}", impact="影响", chapter_number=i)
            for i in range(wounds)
        ),
        boons=tuple(
            EmotionalBoon(description=f"获得{i}", value="价值", chapter_number=i)
            for i in range(boons)
        ),
        power_shifts=tuple(
            PowerShift(from_state="前", to_state="后", trigger="因") for _ in range(shifts)
        ),
        open_loops=tuple(
            OpenLoop(description=f"悬念{i}", hint="暗示", planted_chapter=i)
            for i in range(loops)
        ),
    )


# ── 1) 注入长度有界 ────────────────────────────────────────────────

def test_injection_length_is_bounded_no_matter_how_many_loops():
    """核心守卫：悬念再多，注入长度也**有界**。

    此前 open_loops 全部打印 ⇒ 长度随未回收悬念**线性增长**。
    """
    small = _ledger(loops=3).to_t0_section()
    huge = _ledger(loops=200).to_t0_section()

    assert len(huge) < len(small) * 4, (
        f"注入长度仍在随悬念数增长：3 条 {len(small)} 字符 vs 200 条 {len(huge)} 字符"
    )


def test_each_category_keeps_only_the_latest_ones():
    """每类只保留**最新**的 N 条（口径 = INJECT_ITEMS_PER_CATEGORY）。"""
    section = _ledger(
        wounds=20, boons=20, shifts=20, loops=20
    ).to_t0_section()

    # 20 条每类只注入 5 条
    # ⚠ 前缀用的是各值对象 ``to_summary_line()`` 的真实标签，
    #   不是「创伤/获得」这类简写 —— 断言必须落在**真实前缀**上，
    #   否则「改了标签但条数没变」的变异照样绿。
    assert section.count("【核心损失】") == INJECT_ITEMS_PER_CATEGORY
    assert section.count("【核心获得】") == INJECT_ITEMS_PER_CATEGORY
    assert section.count("【势能转化】") == INJECT_ITEMS_PER_CATEGORY
    assert section.count("【未解悬念】") == INJECT_ITEMS_PER_CATEGORY
    # 保留的是**最新**的
    assert "创伤19" in section
    assert "创伤0" not in section
    assert "悬念19" in section
    assert "悬念0" not in section


def test_no_truncation_note_when_nothing_is_dropped():
    """没裁就不该出现裁剪说明（不许制造噪音）。"""
    section = _ledger(wounds=1, boons=1, shifts=1, loops=1).to_t0_section()

    assert "未列" not in section
    assert "只列最近" not in section


# ── 2) 裁剪必须明说 ────────────────────────────────────────────────

def test_dropped_items_are_stated_explicitly():
    """裁掉的条数必须**说出来**（不许静默丢悬念）。"""
    section = _ledger(loops=20).to_t0_section()

    assert "只列最近" in section
    assert "未列" in section
    assert str(20 - INJECT_ITEMS_PER_CATEGORY) in section, (
        "没说出到底漏了几条悬念"
    )


def test_the_note_says_how_many_were_left_out():
    """说明里要同时有「共几条」和「漏了几条」。"""
    section = _ledger(loops=12).to_t0_section()

    assert "共 12 条" in section
    assert f"另有 {12 - INJECT_ITEMS_PER_CATEGORY} 条未列" in section


# ── 3) 写侧上限与注入口径是两回事 ─────────────────────────────────

def test_write_side_cap_and_injection_caliber_are_independent():
    """写得进 ≠ 一次全给。

    ⚠ 这条把两个常量的**分工**钉住：
    - ``MAX_ITEMS_PER_CATEGORY`` = 账本里最多**存**多少条（写侧）；
    - ``INJECT_ITEMS_PER_CATEGORY`` = 一次给模型**看**多少条（注入侧）。
    """
    assert MAX_ITEMS_PER_CATEGORY == 20
    assert INJECT_ITEMS_PER_CATEGORY == 5
    assert INJECT_ITEMS_PER_CATEGORY < MAX_ITEMS_PER_CATEGORY, (
        "注入口径必须小于写侧上限 ⇒ 否则「一次全给」就成立，口径形同虚设"
    )


def test_stored_loops_are_not_dropped_by_the_write_side():
    """写侧**不裁剪**未回收的悬念（那是数据完整性，不是展示问题）。

    ⚠ 必须走**真正的写入路径** ``add_open_loop`` ——
    早先这条守卫直接构造 ``EmotionLedger(open_loops=...)``，
    而 dataclass 构造器本来就不裁 ⇒ 守卫钉的是「构造器」，
    变异打的却是 ``add_open_loop`` ⇒ 两者毫无关系 ⇒ **假绿**。
    （第 9 批变异验证 M4 实测：这条守卫当时抓不到任何变异。）
    """
    ledger = EmotionLedger.create_empty()
    for i in range(30):
        ledger = ledger.add_open_loop(
            OpenLoop(description=f"悬念{i}", hint="暗示", planted_chapter=i)
        )

    assert len(ledger.open_loops) == 30, "写侧把未回收的悬念丢了"


# ── 4) 槽位上限只作兜底 ────────────────────────────────────────────

def test_the_slot_limit_is_not_the_binding_constraint():
    """槽位 ``max_tokens`` 要装得下**满口径**（否则口径设了也被砍一刀）。

    满口径 = 4 类 × INJECT_ITEMS_PER_CATEGORY 条。
    """
    source = ALLOCATOR.read_text(encoding="utf-8")
    idx = source.find('slots["emotion_ledger"]')
    assert idx > 0
    open_at = source.find("ContextSlot(", idx)
    depth = 0
    end = -1
    for pos in range(open_at, len(source)):
        if source[pos] == "(":
            depth += 1
        elif source[pos] == ")":
            depth -= 1
            if depth == 0:
                end = pos
                break
    block = source[idx : end + 1]

    assert "max_tokens=1000" in block, (
        "槽位上限又退回 400 ⇒ 满口径（约 700 token）会被二次砍掉，口径形同虚设"
    )
