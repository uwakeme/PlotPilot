"""Data models for context budget allocation."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from engine.core.entities.story import StoryPhase
from engine.core.services.context_budget_limits import DEFAULT_CONTEXT_MAX_TOKENS


class PriorityTier(str, Enum):
    """优先级层级（洋葱模型）"""

    T0_CRITICAL = "t0_critical"
    T1_COMPRESSIBLE = "t1_compressible"
    T2_DYNAMIC = "t2_dynamic"
    T3_SACRIFICIAL = "t3_sacrificial"


@dataclass
class ContextSlot:
    """上下文槽位"""

    name: str
    tier: PriorityTier
    content: str = ""
    tokens: int = 0
    max_tokens: Optional[int] = None
    min_tokens: int = 0
    priority: int = 0
    #: 本槽内容的**分节标题**（如 FACT_LOCK 的五节）。
    #:
    #: 用途：槽位被截断时**点名被截掉的小节**。
    #: ⚠ 复审发现（§4）：此前截断标记只说「仅是部分内容」，
    #:   而 FACT_LOCK 整段被截时**模型看不到「身份锁死」这一节存在**
    #:   ⇒ 对「一旦违背即为废稿」的硬约束块，**截掉整节比截断半截更危险**。
    #: ⇒ 声明了小节标题的槽位，截断后会说清「被截掉的小节：X、Y」。
    section_headers: tuple[str, ...] = ()

    @property
    def is_mandatory(self) -> bool:
        """是否强制保留"""
        return self.tier == PriorityTier.T0_CRITICAL


@dataclass
class BudgetAllocation:
    """预算分配结果"""

    slots: Dict[str, ContextSlot] = field(default_factory=dict)
    total_budget: int = DEFAULT_CONTEXT_MAX_TOKENS
    used_tokens: int = 0
    remaining_tokens: int = 0

    t0_reserved: int = 0
    t1_allocated: int = 0
    t2_allocated: int = 0
    t3_allocated: int = 0

    compression_applied: bool = False
    compression_log: List[str] = field(default_factory=list)
    expired_foreshadows: List[str] = field(default_factory=list)

    progress: float = 0.0
    phase: StoryPhase = StoryPhase.OPENING
    total_chapters: int = 0

    def get_final_context(self) -> str:
        """组装最终上下文"""
        parts = []

        for tier in [
            PriorityTier.T0_CRITICAL,
            PriorityTier.T1_COMPRESSIBLE,
            PriorityTier.T2_DYNAMIC,
            PriorityTier.T3_SACRIFICIAL,
        ]:
            tier_slots = [
                (name, slot) for name, slot in self.slots.items() if slot.tier == tier
            ]
            tier_slots.sort(key=lambda item: item[1].priority, reverse=True)

            for _name, slot in tier_slots:
                if slot.content.strip():
                    parts.append(f"\n=== {slot.name.upper()} ===\n{slot.content}")

        if self.expired_foreshadows:
            parts.append(
                "\n=== 强制剧情收束令 ===\n"
                "以下伏笔已超出预期揭晓章节，必须在本章或本节拍的行文中，通过回忆、对话、意外发展或直接揭露等方式去解答或明显推进悬念：\n"
                + "\n".join(f"- {f}" for f in self.expired_foreshadows)
                + "\n【如果你无视此指令，长篇小说的情节网将陷入崩溃】"
            )

        return "\n".join(parts)
