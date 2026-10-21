"""D-03 P1：伏笔「按章取材」的值对象、口径与渲染。

改造前是**代码按固定条数切**（``registry.get_t0_eligible_foreshadowings``：
窗口 + 重要度 + 核心优先），而且取材发生在**上下文组装阶段** ——
七段执行剧本在那之前就定稿了 ⇒ **伏笔永远影响不了剧本**。

现在分两半：

- **B（时机）**：取材提前到七段剧本定稿**之前**，结果挂 ``ctx.foreshadow_context``，
  剧本与正文上下文**用同一份**（一份选择两处使用，不各算一次）。
- **A（谁来选）**：候选清单由代码给出，**AI 决定这一章推哪几条、怎么推**。

代码保证的不变量（AI 动不了）：

- **核心伏笔不得遗忘** —— AI 漏选核心伏笔 ⇒ 代码补回并**标记**
  （``restored_core_ids``），不静默改写。
- **AI 不得凭空造伏笔** —— 候选 id 白名单，非法 id 丢弃。
- **三态**：``ai``（AI 选过）/ ``fallback``（AI 不可用 ⇒ 用领域层合格集）/
  ``""``（未评估）。兜底来源必须可分辨，不能读成「AI 选过」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

#: 来源标记
SOURCE_AI = "ai"
SOURCE_FALLBACK = "fallback"
SOURCE_NONE = ""

#: 兜底时取的候选条数（与上下文槽位的硬上限同口径）
DEFAULT_MAX_ITEMS = 8

#: 伏笔状态标记的窗口（与上下文槽位一致：+3 为「即将到期」）
_DUE_WINDOW = 3


class ForeshadowSelectionLlmPayload(BaseModel):
    """LLM 返回的伏笔选择结果。"""

    selected: List[Dict[str, Any]] = Field(default_factory=list)


@dataclass(frozen=True)
class ForeshadowCandidate:
    """一条可被挑选的伏笔候选（稳定 id = 伏笔自身 id）。"""

    id: str
    description: str
    planted_in_chapter: int
    importance_value: int = 0
    suggested_resolve_chapter: Optional[int] = None
    is_core: bool = False


@dataclass(frozen=True)
class ForeshadowSelection:
    """本章应推进的伏笔。"""

    candidates: Tuple[ForeshadowCandidate, ...] = ()
    notes: Mapping[str, str] = field(default_factory=dict)
    source: str = SOURCE_NONE
    restored_core_ids: Tuple[str, ...] = ()

    @property
    def is_usable(self) -> bool:
        """是否由 AI 选过（``fallback`` 不算）。"""
        return self.source == SOURCE_AI


def collect_candidates(
    registry: Any,
    chapter_number: int,
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> List[ForeshadowCandidate]:
    """领域层合格集 → 候选清单（稳定 id、稳定顺序）。"""
    if registry is None:
        return []
    try:
        items = registry.get_t0_eligible_foreshadowings(
            current_chapter=chapter_number, max_items=max_items
        )
    except Exception:
        return []

    out: List[ForeshadowCandidate] = []
    for f in items or []:
        try:
            importance = getattr(f, "importance", None)
            out.append(
                ForeshadowCandidate(
                    id=str(getattr(f, "id", "") or ""),
                    description=str(getattr(f, "description", "") or ""),
                    planted_in_chapter=int(getattr(f, "planted_in_chapter", 0) or 0),
                    importance_value=int(getattr(importance, "value", 0) or 0),
                    suggested_resolve_chapter=getattr(f, "suggested_resolve_chapter", None),
                    is_core=bool(f.is_core()) if hasattr(f, "is_core") else False,
                )
            )
        except Exception:
            continue
    return [c for c in out if c.id]


def fallback_selection(candidates: Sequence[ForeshadowCandidate]) -> ForeshadowSelection:
    """兜底：AI 不可用时用领域层合格集（**来源可分辨**，不是「AI 选过」）。"""
    return ForeshadowSelection(tuple(candidates), {}, SOURCE_FALLBACK, ())


def normalize_selection(
    data: Optional[Dict[str, Any]],
    candidates: Sequence[ForeshadowCandidate],
    *,
    source: str = SOURCE_AI,
) -> ForeshadowSelection:
    """把 LLM 输出收成伏笔选择。

    - 候选 id 不在清单里 ⇒ 丢弃（**AI 不得凭空造伏笔**）
    - **核心伏笔不得遗忘** —— AI 漏选的核心伏笔由代码补回，并记进
      ``restored_core_ids``（不静默改写）。
    """
    if not isinstance(data, dict):
        return ForeshadowSelection((), {}, SOURCE_NONE, ())
    raw = data.get("selected")
    if not isinstance(raw, list):
        return ForeshadowSelection((), {}, SOURCE_NONE, ())

    by_id = {c.id: c for c in candidates}
    chosen: List[ForeshadowCandidate] = []
    notes: Dict[str, str] = {}
    seen: set = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("id") or "").strip()
        if not fid or fid not in by_id or fid in seen:
            continue
        seen.add(fid)
        chosen.append(by_id[fid])
        note = str(item.get("note") or "").strip().splitlines()[0][:120] if item.get("note") else ""
        if note:
            notes[fid] = note

    restored: List[str] = []
    for c in candidates:
        if c.is_core and c.id not in seen:
            chosen.append(c)
            restored.append(c.id)

    return ForeshadowSelection(tuple(chosen), notes, source, tuple(restored))


def _status_mark(suggested: Optional[int], chapter_number: int) -> str:
    if not suggested:
        return ""
    if suggested <= chapter_number:
        return "已过期"
    if suggested <= chapter_number + _DUE_WINDOW:
        return "即将到期"
    return f"预期Ch{suggested}"


def build_foreshadow_context(
    registry: Any,
    selection: ForeshadowSelection,
    chapter_number: int,
    *,
    include_subtext: bool = True,
) -> str:
    """把选择渲染成上下文文本 —— 剧本与正文**用同一份**。

    标题与条目格式沿用改造前的写法 ⇒ **兜底路径逐字不变**；
    区别只在于「哪几条」由谁选（AI 还是领域层合格集）。
    """
    lines: List[str] = []
    items = list(selection.candidates)
    if items:
        lines.append("【待回收伏笔（爽文GC精选）】")
        for c in items:
            importance_mark = "重要" if c.importance_value >= 3 else ""
            status_mark = _status_mark(c.suggested_resolve_chapter, chapter_number)
            note = selection.notes.get(c.id, "")
            suffix = f"（{note}）" if note else ""
            lines.append(
                f"- Ch{c.planted_in_chapter} {importance_mark} {status_mark}: "
                f"{c.description}{suffix}"
            )

    if include_subtext and registry is not None:
        lines.extend(_build_subtext_lines(registry, chapter_number))
    return "\n".join(lines)


def _build_subtext_lines(registry: Any, chapter_number: int) -> List[str]:
    """潜台词手账本（按星标 > 预期回收章排序）—— 代码排序，**不参与 AI 选择**。"""
    try:
        pending = registry.get_pending_subtext_entries() or []
    except Exception:
        return []

    def sort_key(e):
        suggested = getattr(e, "suggested_resolve_chapter", None)
        importance = getattr(e, "importance", "medium")
        importance_val = {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(importance, 2)
        # 用户标记「本章重点」→ 最高优先级
        is_priority = getattr(e, "is_priority_for_chapter", False)
        priority_tier = 0 if is_priority else 1
        if suggested:
            if suggested <= chapter_number:
                return (priority_tier, 0, -importance_val, suggested)
            else:
                return (priority_tier, 1, -importance_val, suggested)
        return (priority_tier, 2, -importance_val, 9999)

    try:
        ordered = sorted(pending, key=sort_key)
    except Exception:
        return []

    out: List[str] = []
    for entry in ordered[:5]:
        try:
            importance = getattr(entry, "importance", "medium")
            suggested = getattr(entry, "suggested_resolve_chapter", None)
            status_mark = _status_mark(suggested, chapter_number)
            out.append(
                f"- Ch{entry.chapter} [{entry.character_id}] {status_mark}: {entry.question}"
            )
        except Exception:
            continue
    if not out:
        return []
    return ["", "【伏笔手账本·待兑现疑问】"] + out
