"""Budget compression policy for context slots."""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Mapping, Optional

from application.engine.services.context_budget_models import ContextSlot

logger = logging.getLogger(__name__)


#: S-06：截断标记（**会进模型可见内容** ⇒ 写成「给模型看的话」）。
#:
#: 改造前这里是裸的 ``"..."`` ⇒ 模型看到的是「内容被砍掉了」，
#: 但不知道砍的是什么、也不知道该不该担心（而 T0 被砍的通常是
#: ``fact_lock`` —— **不得违反的既成事实**那块）。
#:
#: ⇒ 统一成本仓既有口径（``context_builder.py`` 的「（本块已按上限截断）」、
#: ``prose_composer.py`` 的「（硬事实块已按总长上限截断）」、
#: ``context_budget_allocator.py`` 的「（叙事校准注入已按上限截断）」）。
TRUNCATION_MARKER = "\n（此段已按上下文预算截断，仅为部分内容）"

#: 槽位被**整体舍弃**时的说明（不许静默丢一整个槽）。
#:
#: ⚠ 复审 S1：定义了、写了一段很长的说明，但**零使用** ⇒ 是**死代码**。
#:   留着它 = 留一个「槽位被舍弃时模型能看见说明」的**假信号**
#:   （本项目反复修的那类病）⇒ 已删。
#:   实际做法：整槽被舍弃时**内容留空**（留标记就要为它计 token ⇒ 会破坏
#:   「total ≤ budget」这条不变量），痕迹走 ``compression_log`` + logger。

#: 「被截掉的小节」这段说明的**位置标记**（内容开头）。
#:
#: ⚠ 复审 B-1：这段说明**必须放在开头** —— 放在结尾会被**下游的二次截断**切掉：
#:   分配器给 fact_lock 的上限是 1500 tokens × 1.5 = **2250 字**，
#:   而正文槽的上限是 **2000 字**（``prose_context_settings.DEFAULT_MAX_CHARS_PER_SLOT``）
#:   ⇒ 分配器留在**结尾**的点名正好被 builder 那次独立截断切没
#:   ⇒ 模型在正文提示词里**又**看不见「哪一节没了」（= 上轮 §4 的同一个病灶）。
#: ⇒ 开头是**唯一**能被后续任何截断保住的位置。
_MISSING_SECTIONS_PREFIX = "【被截掉的小节："
#: ⚠ 外层用 **【】**（不是 ``（）``）—— 小节标题本身含「）」（如
#:   「核心事件时间线（不可矛盾）：」）⇒ 用圆括号做外层会让标记**无法被无歧义解析**。
_MISSING_SECTIONS_SUFFIX = "】\n"
#: 开头那个点名块（用于**替换**而不是叠加 ⇒ 幂等）。
_MISSING_SECTIONS_RE = re.compile(r"^【被截掉的小节：(.+?)】\n?", re.S)
#: 点名块的**预留**字符数（上层按剩余额度裁时要给它留位置）。
#:
#: ⚠ 实际长度随「缺了几节」变化 ⇒ 上层还要**实测一次**再收（见 context_builder）。
NAMING_RESERVE_CHARS = 120

#: 截断的**开头留痕**（第 24 批）。
#:
#: ⚠ 为什么必须在**开头**：结尾那个 ``TRUNCATION_MARKER`` 会被**下游的二次截断**切掉
#:   （分配器给 fact_lock 2250 字，正文 builder 再切到 2000 字 ⇒ 结尾标记正好
#:   落在被切掉的那一段里）⇒ **模型在正文提示词里根本看不到「本块被截断了」**。
#:
#: ⚠ 之前只有「点名被截掉的小节」，而**被切在一节中间**的那一节（实测：身份锁死
#:   切在第 5 个角色中间）**标题还在** ⇒ 点名机制认不出来 ⇒ 模型以为
#   「身份锁死这一节就这几个角色」⇒ **半截的硬约束被当成了完整设定**。
#:
#: ⇒ 开头这一句把「本块不完整」这件事**明说**给模型。
TRUNCATION_NOTICE = "【本块已按上限截断，以下内容不完整】\n"
_TRUNCATION_NOTICE_RE = re.compile(r"^【本块已按上限截断，以下内容不完整】\n?", re.S)



def _already_named_sections(content: str) -> list:
    """原文开头**已经**点名过的小节（去掉结尾冒号的形式）。"""
    match = _MISSING_SECTIONS_RE.match(content or "")
    if not match:
        return []
    return [part.strip() for part in match.group(1).split("、") if part.strip()]


def _strip_naming_block(content: str) -> str:
    """去掉开头那个点名块（重新截断时**替换**而不是叠加）。"""
    without_naming = _MISSING_SECTIONS_RE.sub("", content or "", count=1)
    # ⚠ 开头留痕同理（幂等）：重新截断时**替换**而不是叠加
    return _TRUNCATION_NOTICE_RE.sub("", without_naming, count=1)


def _section_body(text: str, header: str, section_headers) -> str:
    """取 ``text`` 里 ``header`` 那一节的**内容**（到下一个标题为止）。

    ⚠ 用途：判断「这一节是不是被切了一半」——
    **标题还在**但内容比原文少 ⇒ 模型会以为「这一节就这么多」
    ⇒ 点名机制必须算上它（否则就是静默失效）。
    """
    start = text.find(header)
    if start < 0:
        return ""
    start += len(header)
    end = len(text)
    for other in section_headers or ():
        if other == header:
            continue
        idx = text.find(other, start)
        if idx >= 0 and idx < end:
            end = idx
    return text[start:end]


def _missing_sections(content: str, kept: str, section_headers) -> list:
    """原文里存在、但被截掉的小节标题。

    ⚠ 只报**确实出现过**（在原文里）的小节 ——
    原文本来就没有的小节不算「被截掉」。

    ⚠ 第 24 批：**「标题还在、内容只剩一半」也算被截掉**。
      此前只报「整节没了」⇒ 刀口切在一节**中间**时（实测：调序后
      「角色设定」这一节被切掉一半）标题还在 ⇒ 点名机制认不出来 ⇒
      模型以为「这一节就这几个角色」⇒ **静默失效**。
    """
    missing = []
    for header in section_headers or ():
        if not header or header not in content:
            continue
        if header not in kept:
            missing.append(header)
            continue
        if _section_body(content, header, section_headers).strip() != _section_body(
            kept, header, section_headers
        ).strip():
            missing.append(header)
    return missing


def _cut_at_line_boundary(text: str, target_chars: int) -> str:
    """切到 ``target_chars``，但**只切在行边界上** ⇒ 不切半行。

    ⚠ 为什么（第 24 批，生产实测出来的）：
      槽位里的行是**一条条约束**（如
      ``   贺九 = 公开面 | [已揭露] …（POV防火墙：…） | 当前心理: …``），
      切在半行 ⇒ 模型看到的是**半条约束**（半句 POV 防火墙）
      ⇒ 它会把半句当成**完整设定** ⇒ **比整条缺失更危险**。
    ⇒ 普通的约束行**整行**丢掉（不送半行）。

    ⚠ 唯一例外（**实测逼出来的**）：刀口所在的那一行**比预算的一半还长**。
      生产实测的样本：白名单一行 400 个角色 = **2290 字** > 预算 2250 字
      ⇒ 这一行**根本装不下** ⇒ 整行丢掉 = 整个白名单都没了
      （实测：2250 字的预算最后只剩 **18 字**内容）
      ⇒ 这种行是**超长清单**（不是一条约束）⇒ 半截仍然可读 ⇒ 只能切在字上。

    ⚠ 行级回退 ⇒ 被丢掉的内容变多 ⇒ 「点名被截掉的小节」必须**跟着算**
      （``mark_truncated`` 里用保留部分重新算一遍 ⇒ 点名不会撒谎）。
    """
    if target_chars <= 0:
        return ""
    if len(text) <= target_chars:
        return text
    cut = text[:target_chars]
    newline = cut.rfind("\n")
    if newline >= 0:
        # 刀口所在的那一行（从上一个换行之后到该行末尾）
        line_end = text.find("\n", newline + 1)
        line_len = (line_end if line_end >= 0 else len(text)) - (newline + 1)
        trimmed = cut[:newline]
        if trimmed.strip() and line_len * 2 <= target_chars:
            # 这一行**装得下**预算的一半 ⇒ 它是一条约束 ⇒ **整行**丢掉
            return trimmed
        # ⚠ 这一行超长（清单）⇒ 丢不得 ⇒ 切在字上（开头留痕会说明不完整）
        return cut.rstrip()
    # ⚠ 整块内容**只有一行** ⇒ 没有行边界可切 ⇒ 按字符切
    return cut.rstrip()


def _drop_trailing_section_header(kept: str, section_headers) -> str:
    """保留部分若**以某个小节标题结尾**（标题在、内容没到）⇒ 把标题也丢掉。

    ⚠ 为什么（第 24 批，生产实测出来的）：
      刀口落在「身份锁死：」标题之后、内容之前时，模型会看到
      「身份锁死：」后面**空着** ⇒ 而点名机制也**认不出**这一节被截断
      （标题还在保留部分里）⇒
      ⇒ 既没有内容、也没有「这一节被截掉了」的提示
      ⇒ 模型只会以为「这一节本来就是空的」⇒ **静默失效**。

    ⇒ 把这个标题也丢掉 ⇒ 点名机制随后就会点名它 ⇒ 模型明确知道「这一节没了」。
    """
    if not kept or not section_headers:
        return kept
    lines = kept.splitlines()
    if not lines:
        return kept
    last_line = lines[-1].strip()
    for header in section_headers:
        if header and last_line == str(header).strip():
            return "\n".join(lines[:-1]).rstrip()
    return kept


def mark_truncated(content: str, target_chars: int, *, section_headers=()) -> str:
    """按上限截断，并**附上说明**（不许裸 ``...``）。

    ⚠ 复审发现（§4）：标记只说「仅是部分内容」⇒ 模型不知道**哪一节**没了。
    ⇒ 槽位声明了 ``section_headers`` 时，额外**点名被截掉的小节**。

    ⚠ 复审 B-1：**点名放在开头**（见 :data:`_MISSING_SECTIONS_PREFIX` 的说明），
    否则会被下游的二次截断切没 ⇒ 模型在正文提示词里又看不见了。

    ⚠ 复审 B-2′：点名块**幂等** —— 重新截断时**替换/合并**已存在的那块，
    不叠加（否则两层截断会留下两个块，而解析约定只认第一个）。

    ⚠ 复审 §2.2：命名与截断**必须在同一层**算 ⇒ 上层（正文 builder）也必须
    复用本函数并把 ``section_headers`` 传进来，否则它那一刀丢掉的小节
    「相对最终内容是陈旧的」。
    """
    if target_chars <= 0:
        return ""
    already = _already_named_sections(content)
    body = _strip_naming_block(content)
    kept = _cut_at_line_boundary(body, target_chars).rstrip()
    # ⚠ 标题在、内容没到 ⇒ 标题也丢（否则点名机制认不出 ⇒ 静默失效）
    kept = _drop_trailing_section_header(kept, section_headers)
    missing = _missing_sections(body, kept, section_headers)
    names = list(
        dict.fromkeys([*already, *(str(h).rstrip("：:") for h in missing)])
    )
    notice = ""
    if names:
        notice = f"{_MISSING_SECTIONS_PREFIX}{'、'.join(names)}{_MISSING_SECTIONS_SUFFIX}"
    # ⚠ 第 24 批：开头**明说**「本块被截断了」——
    #   结尾那个标记会被下游的二次截断切掉 ⇒ 模型看不到
    #   （实测：正文提示词里只有开头的点名块，没有结尾标记）。
    return f"{notice}{TRUNCATION_NOTICE}{kept}{TRUNCATION_MARKER}"


#: 兼容旧名（内部曾用下划线前缀）。
_mark_truncated = mark_truncated


def cap_slots_by_own_limit(
    slots: Dict[str, ContextSlot],
    *,
    chars_per_token_zh: float,
) -> int:
    """把每个槽位裁到**它自己的** ``max_tokens``；返回被裁的槽位数。

    S-06a：T0 路径此前**从不查** ``max_tokens`` ⇒ V9 给每个 T0 槽位写的上限
    （fact_lock 1500 / character_anchors 1500 / context_brief 800 …，合计 8320）
    **形同虚设** ⇒ T0 能涨到 6 万 token。

    ⇒ 这里把那个上限**真正生效**（T1/T2/T3 走 ``allocate_tier`` 时本来就会用
    ``max_tokens``，只有 T0 漏了 ⇒ 两套口径 ⇒ **同一逻辑写两份**）。

    ⚠ 裁剪后**必须带说明**（S-06）—— 这些内容会进模型可见上下文。
    """
    capped = 0
    for slot in slots.values():
        if not slot.max_tokens or slot.max_tokens <= 0:
            continue
        if slot.tokens <= slot.max_tokens:
            continue
        target_chars = int(slot.max_tokens * chars_per_token_zh)
        slot.content = _mark_truncated(
            slot.content, target_chars, section_headers=slot.section_headers
        )
        slot.tokens = slot.max_tokens
        capped += 1
    return capped


def truncate_t0_slots(
    t0_slots: Dict[str, ContextSlot],
    budget: int,
    *,
    chars_per_token_zh: float,
    compression_log: Optional[List[str]] = None,
) -> int:
    """把 T0 槽位裁进预算，**按插入顺序**。

    ⚠ S-06（记账修正）：本函数此前**只**裁剪**第一个**溢出的槽位就 ``break``，
    其后的 T0 槽位**原封不动且不计入 total** ⇒
    ① 账面 ``total`` 严重低估 T0 真实用量；
    ② 最终上下文可大幅超预算（实测：budget=200 / a=100 / b=5000 / c=3000
    ⇒ 返回 200，实际内容 100 + 100 + **3000**）。

    ⇒ 现在分两步：
    1. **每个槽位先按自己的 ``max_tokens`` 裁**（V9 给每个槽位都写了上限：
       lifecycle_directive 600 / story_anchor 300 / narrative_promise 420 /
       narrative_contract 1400 / fact_lock 1500 / character_anchors 1500 /
       context_brief 800 / evolution_presenter 1200 / act_summary 600
       ⇒ T0 合计上限 **8320 tokens**）——
       ⚠ 这些上限此前**从未生效**（T0 路径不查 ``max_tokens``），
       这是「T0 无限膨胀」的根因；
    2. 若裁完仍超 ``budget``，再按插入顺序裁到装得下（**且如实计入 total**）。

    ⇒ 正常情况下第 1 步就够了（T0 上限 8320 < 40% 总预算 14000）
    ⇒ 40% 动态阈值保护退化为**兜底**，而不再是唯一防线。
    """
    total = 0
    dropped: List[str] = []
    # ── 第 1 步：按槽位自己的上限裁（幂等，重复裁无副作用） ──────────
    # ⚠ 放在这里而不是只在调用方做 ⇒ **谁调用本函数都先按各自上限裁**
    #   （否则直接调用它的路径会漏掉这一步）。
    cap_slots_by_own_limit(t0_slots, chars_per_token_zh=chars_per_token_zh)

    for slot in t0_slots.values():
        # ── 第 2 步：按总量预算裁（插入顺序，装得下就装） ───────────
        if total + slot.tokens <= budget:
            total += slot.tokens
            continue

        remaining = budget - total
        if remaining > 0:
            target_chars = int(remaining * chars_per_token_zh)
            slot.content = _mark_truncated(
                slot.content, target_chars, section_headers=slot.section_headers
            )
            slot.tokens = remaining
            total += remaining
        else:
            # 装不下了 ⇒ 舍弃（与 allocate_tier 的口径一致），**并计入 total**。
            # ⚠ 复审 S5：此前是 ``content = ""`` ⇒ **清零且不留任何痕迹**
            #   （模型看不到这一节存在，系统侧也没有记录）⇒ 现在**记进日志**。
            # ⚠ 内容仍留空：留下标记就要为它计 token，而预算已满
            #   ⇒ 会破坏「total ≤ budget」这条不变量。⇒ 痕迹走 compression_log。
            dropped.append(slot.name)
            slot.content = ""
            slot.tokens = 0

    if dropped:
        message = f"T0 槽位因预算不足被整体舍弃：{'、'.join(dropped)}"
        if compression_log is not None:
            compression_log.append(message)
        logger.warning(message)
    return total


def allocate_tier(
    tier_slots: Dict[str, ContextSlot],
    budget: int,
    compression_log: List[str],
    *,
    chars_per_token_zh: float,
    slot_weights: Optional[Mapping[str, float]] = None,
) -> int:
    """Allocate a budget for one tier by priority, mutating overflowing slots.

    Args:
        slot_weights: 槽位权重（0-1），由「本章需要什么记忆」规划调用给出。
            提供时**同时**决定两件事：

            1. **分配顺序** —— 按权重降序，没有权重的槽位排在有权重的之后，
               同组内保持静态 ``priority`` 次序。
            2. **有效硬上限** —— ``effective_max = min(max_tokens, max_tokens * w)``。
               权重只能**压低**上限，绝不能抬高，所以 token 硬上限这个不变量不受影响。

    不提供时行为与改造前完全一致（纯按静态 priority）。
    """
    if slot_weights:
        def _sort_key(item):
            name, slot = item
            raw = slot_weights.get(name)
            if raw is None:
                # 无权重 → 排在有权重之后，保持原有 priority 次序
                return (0, float(slot.priority))
            try:
                return (1, float(raw))
            except (TypeError, ValueError):
                return (0, float(slot.priority))

        sorted_slots = sorted(tier_slots.items(), key=_sort_key, reverse=True)
    else:
        sorted_slots = sorted(
            tier_slots.items(),
            key=lambda item: item[1].priority,
            reverse=True,
        )

    def _effective_max(slot: ContextSlot, slot_key: str) -> Optional[int]:
        """权重只压低硬上限，不抬高。

        返回 ``None`` 表示「这个槽位本来就没有硬上限」；
        返回 ``0`` 表示「有权重且权重为 0」—— 两者含义不同，必须区分，
        否则权重 0 会被当成「无上限」而完全失效。

        ⚠️ 查权重的键是 **dict key**（如 ``vector_recall``），不是 ``slot.name``
        （如 ``向量召回``）。真实槽位里两者**从不相同**，且 name 带括号后缀
        （``绝对事实边界(FACT_LOCK)``），不适合当 JSON 键。排序与封顶必须用同一个键。
        """
        if not slot.max_tokens or slot.max_tokens <= 0:
            return None
        raw = (slot_weights or {}).get(slot_key)
        if raw is None:
            return int(slot.max_tokens)
        try:
            weight = float(raw)
        except (TypeError, ValueError):
            return int(slot.max_tokens)
        weight = max(0.0, min(1.0, weight))
        return int(min(int(slot.max_tokens), int(slot.max_tokens * weight)))

    total_used = 0
    for name, slot in sorted_slots:
        # 权重要在「装得下」判断**之前**生效：否则内容装得下时权重不起作用，
        # 权重就只是个排序键而已，压不出任何东西。
        cap = _effective_max(slot, name)
        if cap is not None and slot.tokens > cap:
            original = slot.tokens
            target_chars = int(cap * chars_per_token_zh)
            # S-06：权重压低上限时也**不许裸 ...** ⇒ 要说明。
            slot.content = _mark_truncated(slot.content, target_chars) if cap > 0 else ""
            slot.tokens = cap
            compression_log.append(
                f"按权重压缩 {name}: {original} → {cap} tokens"
            )

        if total_used + slot.tokens <= budget:
            total_used += slot.tokens
            continue

        remaining = budget - total_used
        original_tokens = slot.tokens
        if slot.max_tokens and slot.max_tokens > 0:
            if remaining > slot.min_tokens:
                target_chars = int(remaining * chars_per_token_zh)
                slot.content = _mark_truncated(slot.content, target_chars)
                slot.tokens = remaining
                total_used += remaining
                compression_log.append(f"压缩 {name}: {original_tokens} → {remaining} tokens")
            else:
                slot.content = ""
                slot.tokens = 0
                compression_log.append(f"舍弃 {name}（预算不足）")
            continue

        if remaining > 0:
            target_chars = int(remaining * chars_per_token_zh)
            slot.content = _mark_truncated(slot.content, target_chars)
            slot.tokens = remaining
            total_used += remaining
            compression_log.append(f"截断 {name}: {original_tokens} → {remaining} tokens")
        else:
            slot.content = ""
            slot.tokens = 0

    return total_used
