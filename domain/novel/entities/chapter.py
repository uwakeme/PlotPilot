# domain/novel/entities/chapter.py
from enum import Enum
from datetime import datetime, timezone
from typing import Optional
from domain.shared.base_entity import BaseEntity
from domain.novel.value_objects.novel_id import NovelId
from domain.novel.value_objects.chapter_content import ChapterContent
from domain.novel.value_objects.word_count import WordCount


class ChapterStatus(str, Enum):
    """章节状态"""
    DRAFT = "draft"
    REVIEWING = "reviewing"
    COMPLETED = "completed"


class Chapter(BaseEntity):
    """章节实体"""

    def __init__(
        self,
        id: str,
        novel_id: NovelId,
        number: int,
        title: str,
        content: str = "",
        outline: str = "",
        status: ChapterStatus = ChapterStatus.DRAFT,
        tension_score: float = 50.0,
        plot_tension: float = 50.0,
        emotional_tension: float = 50.0,
        pacing_tension: float = 50.0,
        generation_hint: str = "",
        tension_evaluated: bool = False,
    ):
        super().__init__(id)
        self.novel_id = novel_id
        self.number = number
        self.title = title
        self._content_text = content  # 直接存储文本，允许空内容
        self.outline = outline  # 章节大纲
        self.status = status
        self.tension_score = tension_score  # 章节综合张力值 0-100
        self.plot_tension = plot_tension  # 情节张力 0-100
        self.emotional_tension = emotional_tension  # 情绪张力 0-100
        self.pacing_tension = pacing_tension  # 节奏张力 0-100
        self.generation_hint = generation_hint  # 用户手写的本章生成约束（直注 AI 上下文）
        # ⚠ 第 82 批（N-01）：**张力有没有真的评过**。
        #   四列的 50.0 是建表默认值 ⇒ 「没评过」与「恰好 50 分」在值上同形，
        #   所以「评没评过」必须由这枚**独立标记**承载，不能从值反推。
        #   ⚠ 默认 False（未评估）是安全的一侧：宁可漏一个已评估，
        #   也不把没评过的当成评过的。
        self.tension_evaluated = tension_evaluated

    @property
    def content(self) -> str:
        return self._content_text

    @property
    def word_count(self) -> WordCount:
        if not self._content_text:
            return WordCount(0)
        content_obj = ChapterContent(self._content_text)
        return WordCount(content_obj.word_count())

    def update_content(self, content: str) -> None:
        """更新内容（允许空内容用于草稿）"""
        self._content_text = content
        self.updated_at = datetime.now(timezone.utc)

    @property
    def evaluated_tension_score(self) -> Optional[float]:
        """**真的评过**时的综合张力分；没评过 ⇒ ``None``。

        ⚠ 第 84 批：「没评过」时 ``tension_score`` 里躺的是建表默认值 ``50.0``
          ⇒ 直接把它当「张力 50」喂给提示词 ⇒ **模型会以为前章张力正好是 50**
          （提示词里还写着「若前章≤30 需要拉升 / 若前章≥70 可以回落」）
          ⇒ 假值污染了判断依据。
        ⇒ 这里给出**唯一的合法出口**：没评过就是 ``None``（未评估）。
        """
        if not self.tension_evaluated:
            return None
        return float(self.tension_score)

    def update_generation_hint(self, hint: str) -> None:
        """更新本章生成约束文本（直接注入 AI 上下文的用户手写约束）"""
        self.generation_hint = hint.strip()
        self.updated_at = datetime.now(timezone.utc)

    def update_tension_score(self, score: float) -> None:
        """更新张力分数（0-100）"""
        if not 0 <= score <= 100:
            raise ValueError(f"Tension score must be between 0 and 100, got {score}")
        self.tension_score = score
        # ⚠ 第 82 批（N-01）：写进来的是**真实评分** ⇒ 本章从此「评过」。
        self.tension_evaluated = True
        self.updated_at = datetime.now(timezone.utc)

    def update_tension_dimensions(self, dimensions: "TensionDimensions") -> None:
        """从 TensionDimensions 值对象更新全部张力字段。"""
        self.plot_tension = dimensions.plot_tension
        self.emotional_tension = dimensions.emotional_tension
        self.pacing_tension = dimensions.pacing_tension
        self.tension_score = dimensions.composite_score
        # ⚠ 第 82 批（N-01）：**只有真评出来的**才算「评过」——
        #   未评估哨兵（-1）不是评分 ⇒ 不许把标记点亮。
        if getattr(dimensions, "is_evaluated", False):
            self.tension_evaluated = True
        self.updated_at = datetime.now(timezone.utc)
