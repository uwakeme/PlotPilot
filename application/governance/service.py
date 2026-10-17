from __future__ import annotations

import re
import uuid
from dataclasses import replace
from typing import Any

from application.governance.models import (
    CanonicalStoryline,
    ChapterNarrativeBudget,
    GovernanceIssue,
    GovernanceReport,
    NarrativeContract,
    utc_now_iso,
)
from application.governance.storyline_registry import (
    canonical_id_for,
    merge_aliases,
    normalize_storyline_key,
)
from application.world.services.narrative_promise import extract_narrative_promise


_EARLY_PAYOFF_TERMS = (
    "真相大白",
    "彻底平反",
    "彻底解决",
    "身份揭晓",
    "幕后黑手现身",
    "大仇得报",
    "飞升成功",
    "终局",
)

# 揭示档位的收紧顺序：数字越小 = 透露得越少。用于把 budget_patch 的
# allowed_reveal_level 变成「只收紧、不放松」的约束，而不是直接覆盖。
_REVEAL_TIGHTNESS = {"hint": 0, "partial": 1, "major": 2, "payoff": 3}


class NarrativeGovernanceService:
    """Book-level narrative coordinator.

    This first production slice is deliberately deterministic. It creates a
    stable contract, canonical storyline surface, chapter budget and report
    records without taking over generation internals in one risky move.
    """

    def __init__(
        self,
        repository: Any,
        novel_repository: Any | None = None,
        legacy_storyline_repository: Any | None = None,
        db: Any | None = None,
    ) -> None:
        self.repository = repository
        self.novel_repository = novel_repository
        self.legacy_storyline_repository = legacy_storyline_repository
        self.db = db

    def get_state(self, novel_id: str) -> dict[str, Any]:
        contract = self.get_or_create_contract(novel_id)
        self.backfill_canonical_storylines(novel_id)
        latest_report = self.repository.latest_report(novel_id)
        return {
            "contract": contract.to_dict(),
            "canonical_storylines": [s.to_dict() for s in self.repository.list_storylines(novel_id)],
            "open_debts": self.repository.list_open_debts(novel_id),
            "latest_report": latest_report.to_dict() if latest_report else None,
            "chapter_budget_preview": self.preview_chapter_budget(novel_id).to_dict(),
        }

    def read_state(self, novel_id: str) -> dict[str, Any]:
        """纯读版治理状态：不创建契约、不回填 storyline、不 emit 事件。

        GET /state 这类读接口必须零写副作用——旧实现（get_state）并发首读
        会重复创建契约并重复 emit NarrativeContractCreated 事件。缺契约时
        返回与 get_or_create_contract 相同结构的内存视图（不落库），前端
        拿到的结构不变；真正持久化发生在第一次写操作（update_contract 等）。
        """
        contract = self.repository.get_contract(novel_id)
        contract_view = contract.to_dict() if contract else self._build_default_contract(novel_id).to_dict()
        storylines = list(self.repository.list_storylines(novel_id))
        if not storylines:
            # 内存视图合并 legacy storyline（不 upsert；写路径仍由 backfill 负责）
            storylines = self._legacy_storyline_views(novel_id)
        latest_report = self.repository.latest_report(novel_id)
        return {
            "contract": contract_view,
            "canonical_storylines": [s.to_dict() for s in storylines],
            "open_debts": self.repository.list_open_debts(novel_id),
            "latest_report": latest_report.to_dict() if latest_report else None,
            "chapter_budget_preview": self.preview_chapter_budget(novel_id).to_dict(),
        }

    def _legacy_storyline_views(self, novel_id: str) -> list[CanonicalStoryline]:
        """从 legacy storyline 仓储构造只读视图（不落库）。"""
        if not self.legacy_storyline_repository:
            return []
        try:
            from domain.novel.value_objects.novel_id import NovelId

            legacy = self.legacy_storyline_repository.get_by_novel_id(NovelId(novel_id))
        except Exception:
            return []
        views: list[CanonicalStoryline] = []
        for item in legacy:
            title = getattr(item, "name", "") or getattr(item, "description", "")[:40] or getattr(item, "id", "")
            views.append(
                CanonicalStoryline(
                    canonical_id=canonical_id_for(novel_id, title, [getattr(item, "id", "")]),
                    novel_id=novel_id,
                    canonical_key=normalize_storyline_key(title),
                    title=title,
                    aliases=merge_aliases([getattr(item, "description", "")]),
                    goal=getattr(item, "progress_summary", "") or "",
                    span={
                        "start": getattr(item, "estimated_chapter_start", None),
                        "end": getattr(item, "estimated_chapter_end", None),
                        "last_active": getattr(item, "last_active_chapter", None),
                    },
                    source_storyline_ids=[getattr(item, "id", "")],
                )
            )
        return views

    def _build_default_contract(self, novel_id: str) -> NarrativeContract:
        """按小说标题/梗概构造默认契约（仅内存视图；持久化由 get_or_create_contract 决定）。"""
        novel = self._load_novel(novel_id)
        title = getattr(novel, "title", "") if novel is not None else ""
        premise = getattr(novel, "premise", "") if novel is not None else ""
        promise = extract_narrative_promise(title, premise)
        anchors = list(promise.promise_keywords)
        if promise.genre_signal:
            anchors.insert(0, promise.genre_signal)

        return NarrativeContract(
            novel_id=novel_id,
            title_promise=promise.title or title or "未命名叙事承诺",
            core_question=promise.core_conflict or promise.opening_hook or premise[:180],
            theme_anchors=anchors[:8],
            forbidden_early_payoffs=[
                "前12章彻底解决书名反差",
                "前12章让核心敌人无代价退场",
                "前12章一次性公开身份/冤案/传承全部真相",
            ],
            reveal_budget={
                "opening": "hint",
                "development": "partial",
                "convergence": "major",
                "finale": "payoff",
            },
        )

    def get_or_create_contract(self, novel_id: str) -> NarrativeContract:
        existing = self.repository.get_contract(novel_id)
        if existing:
            return existing

        contract = self._build_default_contract(novel_id)
        # 并发首读防重：持久化前二次检查，避免多个读请求各自 emit
        # NarrativeContractCreated（upsert 幂等但事件会重复）
        raced = self.repository.get_contract(novel_id)
        if raced is not None:
            return raced
        self.repository.save_contract(contract)
        self._emit(novel_id, "NarrativeContractCreated", None, contract.to_dict())
        return contract

    def update_contract(self, novel_id: str, payload: dict[str, Any]) -> NarrativeContract:
        current = self.get_or_create_contract(novel_id)
        contract = replace(
            current,
            title_promise=str(payload.get("title_promise", current.title_promise) or ""),
            core_question=str(payload.get("core_question", current.core_question) or ""),
            theme_anchors=list(payload.get("theme_anchors", current.theme_anchors) or []),
            forbidden_early_payoffs=list(
                payload.get("forbidden_early_payoffs", current.forbidden_early_payoffs) or []
            ),
            reveal_budget=dict(payload.get("reveal_budget", current.reveal_budget) or {}),
            updated_at=utc_now_iso(),
        )
        self.repository.save_contract(contract)
        self._emit(novel_id, "NarrativeContractUpdated", None, contract.to_dict())
        return contract

    def sync_title_promise(self, novel_id: str, title: str) -> NarrativeContract | None:
        """书名变更后让书名承诺跟随新书名；契约未建或已是新书名时静默跳过。"""
        clean = (title or "").strip()
        if not clean:
            return None
        contract = self.repository.get_contract(novel_id)
        if not contract or contract.title_promise == clean:
            return contract
        updated = replace(contract, title_promise=clean, updated_at=utc_now_iso())
        self.repository.save_contract(updated)
        self._emit(novel_id, "NarrativeContractTitleSynced", None, updated.to_dict())
        return updated

    def prepare_chapter(self, novel_id: str, chapter_number: int | None = None) -> dict[str, Any]:
        budget = self.preview_chapter_budget(novel_id, chapter_number)
        self._emit(novel_id, "ChapterPreparedEvent", budget.chapter_number, budget.to_dict())
        return {
            "budget": budget.to_dict(),
            "context_request": {
                "novel_id": novel_id,
                "chapter_number": budget.chapter_number,
                "promise_tags": budget.must_serve_promise_tags,
                "debt_ids": budget.carry_over_debt_ids,
            },
        }

    def preview_chapter_budget(
        self,
        novel_id: str,
        chapter_number: int | None = None,
    ) -> ChapterNarrativeBudget:
        if chapter_number is None:
            chapter_number = self._next_chapter_number(novel_id)
        contract = self.get_or_create_contract(novel_id)
        latest_report = self.repository.latest_report(novel_id)
        debts = self.repository.list_open_debts(novel_id, limit=8)
        stage = self._stage_for(novel_id, chapter_number)

        if stage == "opening":
            max_new = 0
            max_close = 1
            reveal = "hint"
        elif stage == "development":
            max_new = 1
            max_close = 2
            reveal = "partial"
        else:
            max_new = 0
            max_close = 3
            reveal = "major" if stage == "convergence" else "payoff"

        notes: list[str] = []
        # 消费上一章治理报告的 budget_patch —— 治理层已经把「该修什么」算成
        # 具体字段了，这里按字段执行，不再用 severity 猜（severity 是给排序看的
        # 粗粒度标签，拿它推演预算本身就是二次猜测）。patch 形状见
        # _budget_patch_from_issues：must_retouch_promise / must_reduce_new_storylines
        # / max_new_storylines_delta / allowed_reveal_level 均为可选请求，
        # 缺席 = 治理层没提这项要求，不等于默认要满足。
        patch = latest_report.budget_patch if latest_report else None
        if patch:
            if patch.get("must_retouch_promise"):
                notes.append("上一章治理报告判定承诺漂移：本章必须让行动后果直接命中书名承诺或核心冲突。")
            if patch.get("must_reduce_new_storylines"):
                delta = patch.get("max_new_storylines_delta")
                # 只在治理层给出可量化档位时才动 max_new_storylines；
                # 缺 delta 不编造减多少，保持原值（宁可少收紧，不假装知道）。
                if isinstance(delta, int) and not isinstance(delta, bool):
                    max_new = max(0, max_new + delta)
                    notes.append(f"上一章治理报告要求压缩新增故事线：{max_new} 条。")
            cap = patch.get("allowed_reveal_level")
            cap_rank = _REVEAL_TIGHTNESS.get(cap) if isinstance(cap, str) else None
            cur_rank = _REVEAL_TIGHTNESS.get(reveal)
            # 只收紧不放宽；cap 档位无法排序（未知值）时保持不变。
            if cap_rank is not None and cur_rank is not None and cap_rank < cur_rank:
                reveal = cap
                notes.append("上一章治理报告要求把揭示档位收紧到仅给线索。")
        if stage in ("opening", "convergence", "finale"):
            notes.append("默认克制新增故事线；优先让现有线发生因果变化、交汇或回收。")

        return ChapterNarrativeBudget(
            novel_id=novel_id,
            chapter_number=int(chapter_number),
            max_new_storylines=max_new,
            max_debt_closures=max_close,
            allowed_reveal_level=reveal,
            must_serve_promise_tags=contract.theme_anchors[:4],
            carry_over_debt_ids=[str(d.get("debt_id") or d.get("id") or "") for d in debts[:5]],
            notes=notes,
        )

    def commit_chapter(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> GovernanceReport:
        self._emit(
            novel_id,
            "ChapterCommittedEvent",
            chapter_number,
            {"content_length": len(content or ""), "metadata": metadata or {}},
        )
        return self.evaluate_after_chapter(novel_id, chapter_number, content)

    def evaluate_after_chapter(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        sync_flags: dict[str, Any] | None = None,
    ) -> GovernanceReport:
        contract = self.get_or_create_contract(novel_id)
        issues: list[GovernanceIssue] = []
        hit_rate = self._promise_hit_rate(contract, content)

        if hit_rate is None:
            # 没测出来就没有资格说漂移：既不产出 promise_drift，也不暂停。
            # 产出一条 info 让「本次未测量」在报告里可见，而不是消失在 0 里。
            issues.append(
                GovernanceIssue(
                    code="promise_measurement_unavailable",
                    severity="info",
                    title="承诺命中率未测出",
                    detail="本章未能测出承诺命中率（正文为空，或叙事契约没有可用锚点），本次不对承诺漂移下结论。",
                    suggestion="为叙事契约补充主题锚点，并确认本章正文已写入。",
                )
            )
        elif hit_rate <= 0:
            issues.append(
                GovernanceIssue(
                    code="promise_drift",
                    severity="high" if chapter_number <= 12 else "medium",
                    title="承诺漂移",
                    detail="本章正文没有命中书名承诺、核心问题或主题锚点。",
                    suggestion="下一章预算必须服务至少一个承诺标签，并减少新增支线。",
                )
            )
        elif hit_rate < 0.25:
            issues.append(
                GovernanceIssue(
                    code="promise_drift",
                    severity="medium",
                    title="承诺命中不足",
                    detail="本章只弱命中核心承诺，容易让读者感到主线退场。",
                    suggestion="补一处与书名反差或核心冲突直接相关的行动后果。",
                )
            )

        payoff_terms = [term for term in _EARLY_PAYOFF_TERMS if term in (content or "")]
        if chapter_number <= 12 and payoff_terms:
            issues.append(
                GovernanceIssue(
                    code="premature_payoff",
                    severity="high",
                    title="过早结清",
                    detail="开篇阶段出现疑似终局式兑现词。",
                    evidence=payoff_terms[:4],
                    suggestion="改为局部证据、误导性线索或带代价的小胜。",
                )
            )

        storylines = self.repository.list_storylines(novel_id)
        active_count = len([s for s in storylines if s.status == "active"])
        if chapter_number <= 20 and active_count > max(4, chapter_number // 3 + 2):
            issues.append(
                GovernanceIssue(
                    code="storyline_inflation",
                    severity="medium",
                    title="故事线膨胀",
                    detail=f"当前活跃 canonical storylines={active_count}，超过开篇承载能力。",
                    suggestion="合并同义线，下一章不要新增支线。",
                )
            )

        if sync_flags and sync_flags.get("causal_edges_stored") is False and chapter_number > 1:
            issues.append(
                GovernanceIssue(
                    code="causal_gap",
                    severity="low",
                    title="因果链偏弱",
                    detail="章后抽取未写入因果边，可能存在事件推进但缺少明确因果。",
                    suggestion="下一章强化上一章行动导致的具体后果。",
                )
            )

        severity = self._max_severity(issues)
        # 三态暂停判定（I-03）：
        # - None：本次未测量，无可信证据，治理层不给建议
        # - True/False：评估过了，结论如下
        # 章号闸已删除：原先的 `chapter_number <= 12` 会让任何将来新增的严重度
        # 检测器在第 12 章后静默失效。过早结清与 high 档承诺漂移各自仍只在前
        # 12 章产出（开篇问题），所以删闸不会让后段章节突然开始被暂停。
        if hit_rate is None:
            should_pause: bool | None = None
        else:
            should_pause = severity == "critical" or (
                any(i.code == "promise_drift" and i.severity == "high" for i in issues)
                and any(i.code == "premature_payoff" for i in issues)
            )
            if should_pause and severity != "critical":
                severity = "critical"

        report = GovernanceReport(
            report_id=f"gov_{uuid.uuid4().hex}",
            novel_id=novel_id,
            chapter_number=chapter_number,
            severity=severity,
            promise_hit_rate=hit_rate,
            issues=issues,
            budget_patch=self._budget_patch_from_issues(issues),
            should_pause_autopilot=should_pause,
        )
        self.repository.save_report(report)
        self._emit(novel_id, "GovernanceEvaluatedEvent", chapter_number, report.to_dict())
        if should_pause:
            # 只出建议、不停书（I-03 改判）：治理指标不是可信证据，自动停书
            # 且不可恢复。真正的暂停挂在人工 accept 上（见 review_action）。
            self._emit(novel_id, "GovernancePauseRecommendedEvent", chapter_number, report.to_dict())
        return report

    def merge_storylines(self, novel_id: str, payload: dict[str, Any]) -> CanonicalStoryline:
        source_ids = [str(x) for x in payload.get("source_ids", []) if str(x).strip()]
        target_id = str(payload.get("target_id") or "").strip()
        title = str(payload.get("title") or "").strip()
        target = self.repository.get_storyline(target_id) if target_id else None
        sources = [self.repository.get_storyline(sid) for sid in source_ids]
        sources = [s for s in sources if s is not None]

        if not target:
            title = title or (sources[0].title if sources else "未命名故事线")
            target = CanonicalStoryline(
                canonical_id=canonical_id_for(novel_id, title),
                novel_id=novel_id,
                canonical_key=normalize_storyline_key(title),
                title=title,
            )

        target.aliases = merge_aliases(
            target.aliases,
            [s.title for s in sources],
            *(s.aliases for s in sources),
            payload.get("aliases", []),
        )
        target.source_storyline_ids = merge_aliases(
            target.source_storyline_ids,
            *(s.source_storyline_ids for s in sources),
            source_ids,
        )
        target.promise_tags = merge_aliases(target.promise_tags, payload.get("promise_tags", []))
        target.updated_at = utc_now_iso()
        self.repository.upsert_storyline(target)
        for source in sources:
            if source.canonical_id != target.canonical_id:
                self.repository.delete_storyline(source.canonical_id)
        self._emit(novel_id, "CanonicalStorylinesMerged", None, target.to_dict())
        return target

    def review_action(self, novel_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """人工复核通道。治理层只给建议，停书这一动作由人显式 accept 触发。

        暂停建议过去是自动执行的（_pause_autopilot），而支撑它的承诺命中率指标
        在生产库上有 87.6% 的章节为「零命中」——自动停书等于让一个不可信指标
        夺走用户的书写权且无法恢复。现在把它交回给人：accept 一条
        should_pause_autopilot is True 的报告，才真的执行暂停。
        """
        report_id = str(payload.get("report_id") or "").strip()
        action = str(payload.get("action") or "accepted").strip()
        status = {
            "accept": "accepted",
            "accepted": "accepted",
            "ignore": "ignored",
            "ignored": "ignored",
            "modify": "modified",
            "modified": "modified",
        }.get(action, action or "accepted")
        paused = False
        if report_id:
            self.repository.update_report_status(novel_id, report_id, status)
            report = self.repository.get_report(novel_id, report_id)
            if status == "accepted" and report is not None and report.should_pause_autopilot is True:
                self._pause_autopilot(novel_id)
                paused = True
                self._emit(
                    novel_id,
                    "GovernancePauseAcceptedEvent",
                    report.chapter_number,
                    {"report_id": report_id, "status": status, "autopilot_paused": True},
                )
        self._emit(novel_id, "GovernanceReviewAction", None, {"report_id": report_id, "status": status})
        return {"report_id": report_id, "status": status, "autopilot_paused": paused}

    def backfill_canonical_storylines(self, novel_id: str) -> None:
        if not self.legacy_storyline_repository:
            return
        try:
            from domain.novel.value_objects.novel_id import NovelId

            legacy = self.legacy_storyline_repository.get_by_novel_id(NovelId(novel_id))
        except Exception:
            return
        existing_keys = {s.canonical_key for s in self.repository.list_storylines(novel_id)}
        for item in legacy:
            title = getattr(item, "name", "") or getattr(item, "description", "")[:40] or getattr(item, "id", "")
            key = normalize_storyline_key(title)
            if key in existing_keys:
                continue
            canonical = CanonicalStoryline(
                canonical_id=canonical_id_for(novel_id, title, [getattr(item, "id", "")]),
                novel_id=novel_id,
                canonical_key=key,
                title=title,
                aliases=merge_aliases([getattr(item, "description", "")]),
                goal=getattr(item, "progress_summary", "") or "",
                span={
                    "start": getattr(item, "estimated_chapter_start", None),
                    "end": getattr(item, "estimated_chapter_end", None),
                    "last_active": getattr(item, "last_active_chapter", None),
                },
                source_storyline_ids=[getattr(item, "id", "")],
            )
            self.repository.upsert_storyline(canonical)
            existing_keys.add(key)

    def _load_novel(self, novel_id: str) -> Any | None:
        if self.novel_repository:
            try:
                from domain.novel.value_objects.novel_id import NovelId

                return self.novel_repository.get_by_id(NovelId(novel_id))
            except Exception:
                return None
        if self.db:
            row = self.db.fetch_one("SELECT title, premise, target_chapters FROM novels WHERE id = ?", (novel_id,))
            return type("NovelRow", (), dict(row))() if row else None
        return None

    def _next_chapter_number(self, novel_id: str) -> int:
        if self.db:
            try:
                row = self.db.fetch_one("SELECT MAX(number) AS n FROM chapters WHERE novel_id = ?", (novel_id,))
                return int((row or {}).get("n") or 0) + 1
            except Exception:
                return 1
        return 1

    def _stage_for(self, novel_id: str, chapter_number: int) -> str:
        target = 0
        novel = self._load_novel(novel_id)
        if novel is not None:
            target = int(getattr(novel, "target_chapters", 0) or 0)
        if target <= 0:
            target = 80
        progress = max(0.0, min(1.0, chapter_number / target))
        if chapter_number <= 12 or progress <= 0.25:
            return "opening"
        if progress <= 0.75:
            return "development"
        if progress <= 0.9:
            return "convergence"
        return "finale"

    def _promise_hit_rate(self, contract: NarrativeContract, content: str) -> float | None:
        """承诺命中率。返回 None = 本次测不出来（不是 0，也不是 1）。

        - 正文为空 → None：没有正文就没有测量对象，写 0.0 等于谎称「测了，是零命中」。
        - 契约没有任何可用锚点 → None：原先返回 1.0（满分），那是全批最恶劣的
          伪装——生产库 theme_anchors 全空时每章都拿满分。
        """
        text = re.sub(r"\s+", "", content or "")
        if not text:
            return None
        anchors = []
        anchors.extend(contract.theme_anchors)
        anchors.extend([contract.title_promise, contract.core_question])
        anchors = [a for a in anchors if a and len(str(a).strip()) >= 2]
        if not anchors:
            return None
        hits = 0
        for anchor in anchors:
            terms = [anchor] if len(anchor) <= 12 else re.split(r"[，,。；;：:\s]+", anchor)
            if any(term and len(term) >= 2 and term in text for term in terms):
                hits += 1
        return round(hits / max(1, len(anchors)), 3)

    def _max_severity(self, issues: list[GovernanceIssue]) -> str:
        if not issues:
            return "info"
        order = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
        return max((issue.severity for issue in issues), key=lambda x: order.get(x, 0))

    def _budget_patch_from_issues(self, issues: list[GovernanceIssue]) -> dict[str, Any]:
        if not issues:
            return {}
        patch: dict[str, Any] = {"must_reduce_new_storylines": False, "must_retouch_promise": False}
        for issue in issues:
            if issue.code in ("promise_drift", "premature_payoff"):
                patch["must_retouch_promise"] = True
                patch["allowed_reveal_level"] = "hint"
            if issue.code == "storyline_inflation":
                patch["must_reduce_new_storylines"] = True
                patch["max_new_storylines_delta"] = -1
        return patch

    def _pause_autopilot(self, novel_id: str) -> None:
        if not self.db:
            return
        try:
            conn = self.db.get_connection()
            conn.execute(
                """
                UPDATE novels
                SET autopilot_status = 'stopped',
                    current_stage = 'paused_for_review',
                    audit_progress = ?
                WHERE id = ?
                """,
                ("叙事治理发现严重结构风险，已暂停自动驾驶。", novel_id),
            )
            conn.commit()
        except Exception:
            return

    def _emit(
        self,
        novel_id: str,
        event_type: str,
        chapter_number: int | None,
        payload: dict[str, Any],
    ) -> None:
        try:
            self.repository.append_event(
                f"gevt_{uuid.uuid4().hex}",
                novel_id,
                event_type,
                chapter_number,
                payload,
            )
        except Exception:
            return
