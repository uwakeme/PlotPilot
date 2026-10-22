from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from application.evolution.services.dead_character_check_schema import (
    VERDICT_ACTIVE,
    VERDICT_MEMORY,
    VERDICT_UNCLEAR,
)
from domain.evolution.models import EvolutionState

# 大纲中的引号实体（《》「」『』）：角色/事件名的显式提及信号
_QUOTED_ENTITY_RE = re.compile(r"[「『《]([^」』》]{1,12})[」』》]")


def _outline_mentions(outline: str, labels) -> str:
    """判断角色 labels 是否被大纲提及，返回命中的 label。

    旧实现是裸子串匹配——单字角色名（如「林」）在中文大纲里几乎必然命中，
    造成 DEAD_CHARACTER_REAPPEARS 大量误报。规则收紧为：
    1. 引号实体精确匹配（任何长度都可信）；
    2. 多字名允许裸匹配（≥2 字误报率低）；
    3. 单字名只认引号实体。
    """
    if not outline:
        return ""
    quoted = {q.strip() for q in _QUOTED_ENTITY_RE.findall(outline)}
    for label in labels:
        if not label:
            continue
        if label in quoted:
            return label
        if len(str(label)) >= 2 and label in outline:
            return label
    return ""


@dataclass
class EvolutionGateViolation:
    level: str
    type: str
    message: str
    suggestion: str = ""
    #: H-01：这条结论是**谁**给的（``ai`` / ``rule`` / 空=未评估）
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class EvolutionGateReport:
    is_pass: bool
    violations: List[EvolutionGateViolation] = field(default_factory=list)
    required_continuations: List[str] = field(default_factory=list)
    repair_plan: List[str] = field(default_factory=list)
    #: H-01：判不出来（未评估）的条数 —— **显式可数**，
    #: 不许让「没判」看起来像「没违规」。
    unassessed_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_pass": self.is_pass,
            "violations": [v.to_dict() for v in self.violations],
            "required_continuations": list(self.required_continuations),
            "repair_plan": list(self.repair_plan),
            "unassessed_count": self.unassessed_count,
        }


class EvolutionGateBlockedError(RuntimeError):
    def __init__(self, report: EvolutionGateReport):
        super().__init__("evolution_gate_blocked")
        self.report = report


class EvolutionGateService:
    def __init__(
        self,
        snapshot_repository: Any = None,
        character_repository: Any = None,
        llm_service: Any = None,
    ):
        self.snapshot_repository = snapshot_repository
        self.character_repository = character_repository
        # H-01：AI 判「死亡角色是活跃登场还是回忆/遗物」。
        # None ⇒ 回落规则判定（命中即拦，与改造前逐位一致）。
        self.llm_service = llm_service

    def check(
        self,
        novel_id: str,
        chapter_number: int,
        outline_content: str,
        branch_id: str = "main",
        pov_character_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        state: Optional[EvolutionState] = None,
        dead_character_verdicts: Optional[Dict[str, str]] = None,
    ) -> EvolutionGateReport:
        """写前连续性闸门（**同步**）。

        ``dead_character_verdicts``：``{character_id: verdict}`` ——
        由调用方（async 处）用 AI 判好传进来。
        没传 ⇒ **规则兜底**：名字命中即拦（与改造前逐位一致，不放宽闸门）。

        ⚠ 这里**不能**改成 async：三个调用点里有一个在纯同步方法里
        （``auto_novel_generation_workflow.prepare_chapter_generation``）。
        AI 判定走 :meth:`check_with_ai`。
        """
        tags = tags or []
        outline = outline_content or ""
        state = self._resolve_state(
            novel_id, chapter_number, branch_id, state
        )

        violations: List[EvolutionGateViolation] = []
        required: List[str] = []
        bypass = self._has_bypass(tags, outline)
        character_labels = self._character_labels(novel_id, state)

        # H-01：死亡角色的出场方式由 **AI** 判（活跃登场 vs 回忆/遗物/传闻）。
        # 候选发现仍由 ``_outline_mentions`` 做（它是粗的，但只当**发现器**用）。
        for candidate in self._dead_character_candidates(novel_id, outline, state):
            char_id = candidate["character_id"]
            hit = candidate["hit"]
            verdict = (dead_character_verdicts or {}).get(char_id)
            if verdict is None:
                # 没判（AI 不可用 / 调用方没跑）⇒ **规则兜底**：命中即拦。
                # 与改造前逐位一致 —— **不放宽**闸门。
                violations.append(
                    EvolutionGateViolation(
                        level="blocking",
                        type="DEAD_CHARACTER_REAPPEARS",
                        message=f"角色 {hit} 已死亡，但当前大纲仍让其以活跃人物出现。",
                        suggestion="改为回忆/遗物/传闻，或由作者 override 明确复活规则。",
                        source="rule",
                    )
                )
                continue
            if verdict == VERDICT_MEMORY:
                # AI 判过：只是回忆/遗物/传闻 ⇒ **不是违规**
                continue
            if verdict == VERDICT_UNCLEAR:
                # 判不出来 ⇒ 显式「未评估」：
                # 既不 blocking，也**不**悄悄当成「没违规」。
                violations.append(
                    EvolutionGateViolation(
                        level="unassessed",
                        type="DEAD_CHARACTER_REAPPEARS",
                        message=(
                            f"角色 {hit} 已死亡，本章提到了他，"
                            "但判不出是回忆/遗物还是活跃登场（未评估）。"
                        ),
                        suggestion=(
                            "确认他是回忆/遗物还是确实登场；"
                            "必要时由作者 override 明确复活规则。"
                        ),
                        source="ai",
                    )
                )
                continue
            # VERDICT_ACTIVE ⇒ 真违规
            violations.append(
                EvolutionGateViolation(
                    level="blocking",
                    type="DEAD_CHARACTER_REAPPEARS",
                    message=f"角色 {hit} 已死亡，但当前大纲仍让其以活跃人物出现。",
                    suggestion="改为回忆/遗物/传闻，或由作者 override 明确复活规则。",
                    source="ai",
                )
            )

        unresolved = state.scene.get("unresolved_actions") or []
        for action in unresolved:
            required.append(f"未完成动作：{str(action)[:160]}")
            violations.append(
                EvolutionGateViolation(
                    level="info" if bypass else "blocking",
                    type="UNRESOLVED_ACTION",
                    message=(
                        "上一章存在未完成动作，当前大纲已声明艺术性跳跃豁免。"
                        if bypass
                        else "上一章存在未完成动作，当前大纲未声明 TimeSkip/HardCut 豁免。"
                    ),
                    suggestion="开篇承接该动作，或添加 [TimeSkip] / [HardCut]。",
                )
            )

        completed = set(state.completed_events or [])
        for event_id in completed:
            token = event_id.split(":")[-1]
            if token and len(token) > 6 and token in outline:
                violations.append(
                    EvolutionGateViolation(
                        level="blocking",
                        type="DUPLICATE_EVENT",
                        message=f"大纲疑似重复已完成事件 {event_id}。",
                        suggestion="将其写成回忆/后果，避免当作新事件再次发生。",
                    )
                )

        if pov_character_id:
            known = set((state.facts.get("character_known") or {}).get(pov_character_id, []))
            reader_known = set(state.facts.get("reader_known") or [])
            leaked = [fact for fact in reader_known if fact in outline and fact not in known]
            for fact in leaked[:5]:
                violations.append(
                    EvolutionGateViolation(
                        level="warning",
                        type="POV_LEAK",
                        message=f"当前 POV {pov_character_id} 尚未知晓读者事实：{fact}",
                        suggestion="让角色通过线索推断，或切换 POV / 添加 [POVSwitch]。",
                    )
                )

        repair = [v.suggestion for v in violations if v.level == "blocking" and v.suggestion]
        return EvolutionGateReport(
            is_pass=not any(v.level == "blocking" for v in violations),
            violations=violations,
            required_continuations=required,
            repair_plan=repair,
            unassessed_count=sum(1 for v in violations if v.level == "unassessed"),
        )

    async def check_with_ai(
        self,
        novel_id: str,
        chapter_number: int,
        outline_content: str,
        branch_id: str = "main",
        pov_character_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        state: Optional[EvolutionState] = None,
    ) -> EvolutionGateReport:
        """闸门 + **AI 语义判定**（H-01）。

        AI 判「这个已死亡的角色在本章是活跃登场，还是只是被想起」。
        AI 不可用 / 输出不合契约 ⇒ **未评估** ⇒ 回落规则判定
        （命中即拦，与改造前逐位一致），来源可分辨（``rule``）。
        """
        resolved = self._resolve_state(
            novel_id, chapter_number, branch_id, state
        )
        verdicts = await self.classify_dead_characters(
            novel_id=novel_id,
            chapter_number=chapter_number,
            outline_content=outline_content,
            branch_id=branch_id,
            state=resolved,
        )
        return self.check(
            novel_id=novel_id,
            chapter_number=chapter_number,
            outline_content=outline_content,
            branch_id=branch_id,
            pov_character_id=pov_character_id,
            tags=tags,
            state=resolved,
            dead_character_verdicts=verdicts,
        )

    async def classify_dead_characters(
        self,
        novel_id: str,
        chapter_number: int,
        outline_content: str,
        branch_id: str = "main",
        state: Optional[EvolutionState] = None,
    ) -> Dict[str, str]:
        """AI 判「死亡角色是活跃登场还是回忆/遗物」⇒ ``{character_id: verdict}``。

        判不了（无 LLM / 调用失败 / 输出不合契约）⇒ 返回**空 dict**
        ⇒ 调用方落**规则兜底**（命中即拦，与改造前逐位一致）。

        公开出来是为了让 async 调用方（workflow / 管线）也能用同一份实现，
        而不是各写一份候选发现逻辑。
        """
        if self.llm_service is None:
            return {}
        resolved = self._resolve_state(
            novel_id, chapter_number, branch_id, state
        )
        candidates = self._dead_character_candidates(
            novel_id, outline_content or "", resolved
        )
        if not candidates:
            return {}
        from application.evolution.services.dead_character_checker import (
            DeadCharacterChecker,
        )

        result = await DeadCharacterChecker(self.llm_service).check(
            outline=outline_content or "",
            candidates=candidates,
        )
        if not result.is_usable:
            return {}
        return {cid: v.verdict for cid, v in result.verdicts.items()}

    def _resolve_state(
        self,
        novel_id: str,
        chapter_number: int,
        branch_id: str,
        state: Optional[EvolutionState],
    ) -> EvolutionState:
        """取上一章的结束状态（``check`` 与 ``check_with_ai`` 共用一份）。"""
        if state is None and self.snapshot_repository:
            prev = self.snapshot_repository.get_latest_active_before(
                novel_id, branch_id, chapter_number
            )
            state = prev.ending_state if prev else EvolutionState.empty()
        return state or EvolutionState.empty()

    def _dead_character_candidates(
        self, novel_id: str, outline: str, state: EvolutionState
    ) -> List[Dict[str, Any]]:
        """已死亡 **且** 大纲提到了的角色（候选发现器）。

        H-01：``_outline_mentions`` **保留**当候选发现器（别删 ——
        ``DUPLICATE_EVENT`` / ``POV_LEAK`` 不依赖它，但**它本身是粗的**：
        「回忆 / 遗物」也会命中 ⇒ 真正的判断交给 AI。
        """
        labels = self._character_labels(novel_id, state)
        candidates: List[Dict[str, Any]] = []
        for char_id, char in (state.characters or {}).items():
            if not isinstance(char, dict) or char.get("status") != "dead":
                continue
            names = labels.get(char_id) or {char_id}
            hit = _outline_mentions(outline, names)
            if not hit:
                continue
            candidates.append(
                {
                    "character_id": char_id,
                    "names": sorted(str(n) for n in names),
                    "hit": hit,
                }
            )
        return candidates

    @staticmethod
    def _has_bypass(tags: List[str], outline: str) -> bool:
        text = " ".join(tags) + " " + outline
        return any(token in text for token in ["[TimeSkip", "[HardCut", "[POVSwitch", "[AmbiguousFate"])

    def _character_labels(self, novel_id: str, state: EvolutionState) -> Dict[str, set[str]]:
        labels: Dict[str, set[str]] = {}
        for char_id, char in (state.characters or {}).items():
            labels.setdefault(char_id, set()).add(char_id)
            if isinstance(char, dict) and char.get("name"):
                labels[char_id].add(str(char["name"]))
        if not self.character_repository:
            return labels
        try:
            for character in self.character_repository.list_by_novel(novel_id):
                cid = getattr(getattr(character, "id", None), "value", None) or str(getattr(character, "id", ""))
                if not cid:
                    continue
                labels.setdefault(cid, set()).add(cid)
                name = getattr(character, "name", "")
                if name:
                    labels[cid].add(str(name))
        except Exception:
            return labels
        return labels
