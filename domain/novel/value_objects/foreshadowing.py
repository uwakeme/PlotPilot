from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ForeshadowingStatus(str, Enum):
    """伏笔状态"""
    PLANTED = "planted"      # 已埋下
    RESOLVED = "resolved"    # 已解决
    ABANDONED = "abandoned"  # 已放弃


class ImportanceLevel(int, Enum):
    """重要性级别"""
    LOW = 1        # 低
    MEDIUM = 2     # 中等
    HIGH = 3       # 高
    CRITICAL = 4   # 关键


# 绑定层级：与 viewpoint_guardrail._check_foreshadow_binding 的判据一致。
# 1=角色 2=势力 3=事件 4=世界；核心伏笔（emotional_weight > 0.8）不应低于 3。
FORESHADOW_BINDING_CHARACTER = 1
FORESHADOW_BINDING_FACTION = 2
FORESHADOW_BINDING_EVENT = 3
FORESHADOW_BINDING_WORLD = 4

DEFAULT_EMOTIONAL_WEIGHT = 0.5
DEFAULT_BINDING_LEVEL = FORESHADOW_BINDING_CHARACTER


@dataclass(frozen=True)
class Foreshadowing:
    """伏笔值对象"""
    id: str
    planted_in_chapter: int
    description: str
    importance: ImportanceLevel
    status: ForeshadowingStatus
    suggested_resolve_chapter: Optional[int] = None
    resolved_in_chapter: Optional[int] = None
    # viewpoint_guardrail._check_foreshadow_binding 的判据字段。
    # 带默认值以兼容既有构造调用与旧 JSON 载荷。
    emotional_weight: float = DEFAULT_EMOTIONAL_WEIGHT
    binding_level: int = DEFAULT_BINDING_LEVEL
    # G-01：``suggested_resolve_chapter`` 这个**取值**的来源。
    # "ai"          = 模型给了偏移且在 [2,30] 内，直接采用模型原值
    # "ai_adjusted" = 模型给了但被代码改写过（钳位 / int() 截断），
    #                 最终取值非模型原值 —— 标 "ai" 就是在伪造 provenance
    # "fallback"    = 模型没给 / 不可解析 / 非有限值，代码兜底
    # 此前这个标记只打进日志，落库后「AI 说的」与「代码编的」完全同形。
    # 空串 = 旧数据/未标注（**不等于** "ai"，读端必须显式判）。
    resolve_offset_source: str = ""
    #: 来源说明（为什么走了兜底 / 模型给的是什么），可读文本。
    resolve_offset_note: str = ""

    def __post_init__(self):
        if self.planted_in_chapter < 1:
            raise ValueError("planted_in_chapter must be >= 1")
        if not self.description or not self.description.strip():
            raise ValueError("description cannot be empty")
        if self.status == ForeshadowingStatus.RESOLVED and self.resolved_in_chapter is None:
            raise ValueError("RESOLVED status requires resolved_in_chapter")

        # Validate optional chapter fields
        if self.suggested_resolve_chapter is not None and self.suggested_resolve_chapter < 1:
            raise ValueError("suggested_resolve_chapter must be >= 1")
        if self.resolved_in_chapter is not None and self.resolved_in_chapter < 1:
            raise ValueError("resolved_in_chapter must be >= 1")

        # Validate business rules
        if self.resolved_in_chapter is not None and self.resolved_in_chapter < self.planted_in_chapter:
            raise ValueError("resolved_in_chapter must be >= planted_in_chapter")
        if self.suggested_resolve_chapter is not None and self.suggested_resolve_chapter < self.planted_in_chapter:
            raise ValueError("suggested_resolve_chapter must be >= planted_in_chapter")

        # Validate binding fields consumed by viewpoint_guardrail
        if not 0.0 <= self.emotional_weight <= 1.0:
            raise ValueError("emotional_weight must be between 0.0 and 1.0")
        if not (
            FORESHADOW_BINDING_CHARACTER
            <= self.binding_level
            <= FORESHADOW_BINDING_WORLD
        ):
            raise ValueError("binding_level must be between 1 and 4")
