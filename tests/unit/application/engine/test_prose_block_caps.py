"""第 43 批：抬**角色锚点**与**创作契约**两个槽位的上限（每块 12000 字那条线的下一块拼图）。

为什么是这两个（生产实测，``prodrealtest-001`` 第 5 章）
------------------------------------------------------
正文提示词送 6 个块。实测「谁真被卡住」（``tokens == max_tokens`` 才算被卡）：

* ``fact_lock``：10000 tokens，内容 3204 ⇒ **没被卡**（第 41 批已抬）
* ``character_anchors``：**1500 tokens，内容 2106 ⇒ 被卡** ⇒ 丢 1468 字
  ⇒ 丢掉的正是角色的**信念 / 禁忌 / 创伤触发**（最硬的一致性信息）
* ``narrative_contract``：**1400 tokens，内容 2716 ⇒ 被卡** ⇒ 丢 2399 字
  ⇒ 丢掉的正是**世界观五维 / 物理规律 / 技术接口**
* 其余三块（待回收伏笔 / 已完成节拍 / 已揭露线索）：内容不够 ⇒ **抬了也白抬**

⇒ ⇒ 本批只抬**真被卡住**的两个（1500/1400 → **4000** tokens = 6000 字）。

⚠ 口径纪律：6000 字 < 正文每块上限 12000 字 ⇒ builder 那把刀**不会**先切
  ⇒ 槽位上限是唯一的限流口（口径干净，不制造第二个限流点）。

⚠ 守卫纪律：期望值**写死字面量**；生产实测的原始体量也**写死**
  ⇒ 额度不够装下生产书原始体量时必红。
"""
from __future__ import annotations

from application.engine.services.context_budget_allocator import ContextBudgetAllocator
from application.engine.services.prose_context_settings import (
    get_prose_context_runtime_settings,
)
from engine.core.services.context_budget_limits import DEFAULT_CONTEXT_MAX_TOKENS

# ── 口径值（写死 ⇒ 改常量必红）────────────────────────────────────────
#: 角色锚点槽位上限（第 43 批：1500 → 4000）
CHARACTER_ANCHORS_TOKEN_CAP = 4000
#: 创作契约槽位上限（第 43 批：1400 → 4000）
NARRATIVE_CONTRACT_TOKEN_CAP = 4000
#: 中文 → token 的换算（与分配器 ``chars_per_token_zh`` 同口径）
CHARS_PER_TOKEN = 1.5

#: 生产实测（``prodrealtest-001`` 第 5 章）的**原始**体量（未被上限切之前）
PRODUCTION_CHARACTER_ANCHORS_CHARS = 3691
PRODUCTION_NARRATIVE_CONTRACT_CHARS = 4379


def test_the_two_caps_are_pinned():
    """⚠ 三个数字**写死** ⇒ 改被测模块的常量必红（否则是假绿）。"""
    assert ContextBudgetAllocator.MAX_CHARACTER_ANCHORS_TOKENS == CHARACTER_ANCHORS_TOKEN_CAP
    assert (
        ContextBudgetAllocator.MAX_NARRATIVE_CONTRACT_TOKENS
        == NARRATIVE_CONTRACT_TOKEN_CAP
    )


def test_the_caps_can_hold_the_production_sized_blocks():
    """⚠ **本批的核心断言**：额度装得下生产书的原始体量 ⇒ 不再被切。

    ⚠ 期望值必须**从被测常量推导**（不能用写死的字面量算）：
      若这里写死 4000 再去比生产体量 ⇒ 被测常量被改小时这条**照样绿**
      ⇒ 变异「上限退回 1500」就**抓不到**（守卫变成空的）
      ⇒ ⇒ 用「被测常量 × 1.5 ≥ 生产实测体量」⇒ 改小必红。
      「不许退回旧值」由上面 :func:`test_the_two_caps_are_pinned` 钉住。
    """
    anchors_cap_chars = int(
        ContextBudgetAllocator.MAX_CHARACTER_ANCHORS_TOKENS * CHARS_PER_TOKEN
    )
    contract_cap_chars = int(
        ContextBudgetAllocator.MAX_NARRATIVE_CONTRACT_TOKENS * CHARS_PER_TOKEN
    )

    assert anchors_cap_chars >= PRODUCTION_CHARACTER_ANCHORS_CHARS, (
        f"角色锚点额度 {anchors_cap_chars} 字 < 生产原始体量 "
        f"{PRODUCTION_CHARACTER_ANCHORS_CHARS} 字 ⇒ 仍然被切"
    )
    assert contract_cap_chars >= PRODUCTION_NARRATIVE_CONTRACT_CHARS, (
        f"创作契约额度 {contract_cap_chars} 字 < 生产原始体量 "
        f"{PRODUCTION_NARRATIVE_CONTRACT_CHARS} 字 ⇒ 仍然被切"
    )


def test_the_block_caps_stay_below_the_per_slot_char_cap():
    """⚠ 槽位上限 × 1.5 必须 < 正文每块上限 ⇒ **不制造第二个限流点**。

    ⚠ 否则会出现「槽位没卡、builder 先卡」⇒ 额度抬了也白抬
    ⇒ 而且被切的那一刀**不会**带点名（点名是按 ``section_headers`` 算的）。
    """
    per_slot = get_prose_context_runtime_settings().max_chars_per_slot

    assert int(CHARACTER_ANCHORS_TOKEN_CAP * CHARS_PER_TOKEN) < per_slot
    assert int(NARRATIVE_CONTRACT_TOKEN_CAP * CHARS_PER_TOKEN) < per_slot


def test_the_t0_demand_stays_inside_the_protection_line():
    """⚠ 抬完两个槽位后，T0 的需求仍要低于总预算的 40% 保护线。"""
    t0_line = int(DEFAULT_CONTEXT_MAX_TOKENS * 0.40)
    demand = (
        ContextBudgetAllocator.MAX_FACT_LOCK_TOKENS
        + ContextBudgetAllocator.MAX_CHARACTER_ANCHORS_TOKENS
        + ContextBudgetAllocator.MAX_NARRATIVE_CONTRACT_TOKENS
    )

    assert demand < t0_line, (
        f"T0 三个主槽位合计 {demand} tokens ≥ 保护线 {t0_line} ⇒ 会被 _truncate_t0_slots 砍回去"
    )
