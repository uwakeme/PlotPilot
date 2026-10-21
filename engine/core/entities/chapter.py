"""Chapter实体 — 章节模型

统一入口：engine.core.entities.chapter
旧入口：engine.domain.entities.chapter（兼容层，re-export）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Any


class ChapterStatus(str, Enum):
    """章节状态"""
    DRAFT = "draft"
    REVIEWING = "reviewing"
    COMPLETED = "completed"


@dataclass
class Paragraph:
    """段落 — 章节的基本组成单元"""
    content: str
    position: int = 0
    paragraph_type: str = "narrative"
    advances_goal: Optional[str] = None

    @property
    def word_count(self) -> int:
        if not self.content:
            return 0
        clean = self.content.replace(" ", "").replace("\n", "").replace("\t", "")
        return len(clean)


@dataclass
class ChapterQualityScore:
    """章节质量评分 — 由QualityGuardrail计算"""
    language_style: float = 0.0
    character_consistency: float = 0.0
    plot_density: float = 0.0
    rhythm: float = 0.0
    overall: float = 0.0
    violations: List[str] = field(default_factory=list)

    @property
    def is_passing(self) -> bool:
        return self.overall >= 0.6 and len(self.violations) == 0


@dataclass
class Chapter:
    """章节实体"""
    chapter_number: int
    title: str = ""
    outline: str = ""
    content: str = ""
    paragraphs: List[Paragraph] = field(default_factory=list)
    status: ChapterStatus = ChapterStatus.DRAFT

    plot_tension: float = 50.0
    emotional_tension: float = 50.0
    pacing_tension: float = 50.0
    tension_score: float = 50.0

    quality_score: Optional[ChapterQualityScore] = None

    chapter_goal: str = ""
    chapter_hook: str = ""

    @property
    def word_count(self) -> int:
        if self.content:
            clean = self.content.replace(" ", "").replace("\n", "").replace("\t", "")
            return len(clean)
        return sum(p.word_count for p in self.paragraphs)

    def add_paragraph(self, paragraph: Paragraph) -> None:
        paragraph.position = len(self.paragraphs)
        self.paragraphs.append(paragraph)

    def update_content_from_paragraphs(self) -> None:
        self.content = "\n\n".join(p.content for p in sorted(self.paragraphs, key=lambda p: p.position))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chapter_number": self.chapter_number,
            "title": self.title,
            "status": self.status.value,
            "word_count": self.word_count,
            "tension_score": self.tension_score,
            "quality_score": self.quality_score.overall if self.quality_score else None,
            "paragraph_count": len(self.paragraphs),
        }
