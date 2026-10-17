from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


Severity = Literal["info", "low", "medium", "high", "critical"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class NarrativeContract:
    novel_id: str
    title_promise: str = ""
    core_question: str = ""
    theme_anchors: list[str] = field(default_factory=list)
    forbidden_early_payoffs: list[str] = field(default_factory=list)
    reveal_budget: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CanonicalStoryline:
    canonical_id: str
    novel_id: str
    canonical_key: str
    title: str
    aliases: list[str] = field(default_factory=list)
    goal: str = ""
    conflict: str = ""
    span: dict[str, int | None] = field(default_factory=dict)
    promise_tags: list[str] = field(default_factory=list)
    status: str = "active"
    source_storyline_ids: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChapterNarrativeBudget:
    novel_id: str
    chapter_number: int
    max_new_storylines: int
    max_debt_closures: int
    allowed_reveal_level: str
    must_serve_promise_tags: list[str] = field(default_factory=list)
    carry_over_debt_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GovernanceIssue:
    code: str
    severity: Severity
    title: str
    detail: str
    evidence: list[str] = field(default_factory=list)
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GovernanceReport:
    report_id: str
    novel_id: str
    chapter_number: int
    severity: Severity
    # None = 本次没有测出来（无可用锚点 / 正文为空），不是「测了是 0」。
    # 任何 `or 0` 之类的兜底都会把「没测」重新伪装成「零命中」，
    # 而生产库 87.6% 的章节正是这种情况。
    promise_hit_rate: float | None
    issues: list[GovernanceIssue] = field(default_factory=list)
    budget_patch: dict[str, Any] = field(default_factory=dict)
    # 三态：None = 本次未能评估（没有可信证据）/ False = 评估过不建议 / True = 建议暂停。
    # 默认 None 而不是 False —— 未评估时声称「不需要暂停」是在替治理层撒谎。
    should_pause_autopilot: bool | None = None
    created_at: str = field(default_factory=utc_now_iso)
    review_status: str = "open"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["issues"] = [issue.to_dict() for issue in self.issues]
        return data
