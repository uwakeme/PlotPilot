"""全书终审服务 —— 分幕精审 + 汇总报告。

终审分两阶段：
1. ``run_act_review``：按幕把完整正文（+该范围伏笔上下文）交给审读模型，产出
   结构化 findings 并落库（scope='act'，确定性 id 支持重跑覆盖）。
2. ``synthesize``：汇总全部幕的 findings 与伏笔账本总览，产出带优先级的全书
   报告（scope='final'），top_issues 每条自带 suggested_instruction，供前端
   一键跳转到对应章节发起交互式修改。

服务本身无状态可注入（llm/仓储全部走构造参数），结果持久化由本服务负责
（book_review_reports 表，迁移 add_book_review_reports.sql）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from application.ai.llm_json_extract import parse_llm_json_to_dict
from domain.novel.value_objects.novel_id import NovelId
from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_keys import BOOK_ACT_REVIEW, BOOK_REVIEW_SYNTHESIS
from infrastructure.ai.prompt_utils import render_required_prompt

logger = logging.getLogger(__name__)

# 单幕回退分批大小（结构树缺 chapter_start/chapter_end 时按章数切）
FALLBACK_ACT_SIZE = 5


class BookReviewError(RuntimeError):
    """全书终审执行失败。"""


class BookReviewService:
    """全书终审：逐幕精审 + 汇总报告。"""

    def __init__(
        self,
        llm_service: LLMService,
        *,
        db: Any = None,
        chapter_repository: Any = None,
        story_node_repository: Any = None,
        foreshadow_repository: Any = None,
        model: str = "",
    ) -> None:
        self._llm_service = llm_service
        self._model = model
        self._db = db
        self._chapter_repository = chapter_repository
        self._story_node_repository = story_node_repository
        self._foreshadow_repository = foreshadow_repository

    # ---- 依赖惰性获取 -------------------------------------------------

    def _database(self) -> Any:
        if self._db is None:
            from infrastructure.persistence.database.connection import get_database

            self._db = get_database()
        return self._db

    def _chapters(self) -> Any:
        if self._chapter_repository is None:
            from infrastructure.persistence.database.sqlite_chapter_repository import (
                SqliteChapterRepository,
            )

            self._chapter_repository = SqliteChapterRepository(self._database())
        return self._chapter_repository

    def _story_nodes(self) -> Any:
        if self._story_node_repository is None:
            from infrastructure.persistence.database.story_node_repository import (
                StoryNodeRepository,
            )

            self._story_node_repository = StoryNodeRepository(self._database())
        return self._story_node_repository

    def _foreshadows(self) -> Any:
        if self._foreshadow_repository is None:
            from infrastructure.persistence.database.sqlite_foreshadowing_repository import (
                SqliteForeshadowingRepository,
            )

            self._foreshadow_repository = SqliteForeshadowingRepository(self._database())
        return self._foreshadow_repository

    # ---- 幕结构 --------------------------------------------------------

    def list_acts(self, novel_id: str) -> List[Dict[str, Any]]:
        """返回幕列表 [{act_number, title, chapter_start, chapter_end}]。

        优先用结构树上 act 节点自带的 chapter_start/chapter_end；
        结构缺失时按章号每 FALLBACK_ACT_SIZE 章切一幕兜底。
        """
        chapters = [c for c in (self._chapters().list_by_novel(NovelId(novel_id)) or []) if (c.content or "").strip()]
        if not chapters:
            return []
        chapters.sort(key=lambda c: c.number)

        try:
            nodes = self._story_nodes().get_by_novel_sync(novel_id) or []
        except Exception as exc:
            logger.warning("读取结构树失败，使用固定分批兜底: %s", exc)
            nodes = []

        acts: List[Dict[str, Any]] = []
        for node in nodes:
            if getattr(node.node_type, "value", str(node.node_type)) != "act":
                continue
            start = getattr(node, "chapter_start", None)
            end = getattr(node, "chapter_end", None)
            if start is None or end is None:
                continue
            acts.append(
                {
                    "act_number": int(node.number),
                    "title": node.title or f"第{node.number}幕",
                    "chapter_start": int(start),
                    "chapter_end": int(end),
                }
            )
        acts.sort(key=lambda a: a["act_number"])

        if acts:
            # 结构外的散章（如终局加章）并入最后一幕
            last_end = max(a["chapter_end"] for a in acts)
            extras = [c for c in chapters if c.number > last_end]
            if extras:
                acts[-1]["chapter_end"] = max(acts[-1]["chapter_end"], max(c.number for c in extras))
            return acts

        # 兜底：固定大小分批
        acts = []
        for index, start in enumerate(range(0, len(chapters), FALLBACK_ACT_SIZE), start=1):
            batch = chapters[start : start + FALLBACK_ACT_SIZE]
            acts.append(
                {
                    "act_number": index,
                    "title": f"第{index}批",
                    "chapter_start": batch[0].number,
                    "chapter_end": batch[-1].number,
                }
            )
        return acts

    # ---- 伏笔上下文 ----------------------------------------------------

    def _foreshadow_context(self, novel_id: str, start: int, end: int) -> str:
        try:
            planted = self._foreshadows().get_planted_sql(novel_id) or []
        except Exception as exc:
            logger.warning("读取伏笔账本失败: %s", exc)
            return "（账本不可用）"
        if not planted:
            return "（账本中无未回收伏笔）"
        lines = []
        for row in planted:
            p, d = int(row.get("planted_chapter") or 0), int(row.get("due_chapter") or 0)
            if start <= p <= end or start <= d <= end or d > end:
                state = "应兑章在本幕" if start <= d <= end else ("已逾期未兑" if d < start else "排期在本幕之后")
                lines.append(f"- 埋于ch{p}/应兑ch{d}（{state}）：{str(row.get('description') or '')[:80]}")
        if not lines:
            return "（本幕范围内无登记伏笔）"
        return "\n".join(lines[:40])

    def _foreshadow_ledger_overview(self, novel_id: str) -> str:
        db = self._database()
        counts = {r["status"]: r["c"] for r in db.fetch_all(
            "SELECT status, COUNT(*) c FROM foreshadows WHERE novel_id = ? GROUP BY status", (novel_id,)
        )}
        planted = self._foreshadows().get_planted_sql(novel_id) or []
        lines = [f"状态分布：{json.dumps(counts, ensure_ascii=False)}"]
        if planted:
            lines.append("未回收（planted）清单：")
            for row in planted:
                lines.append(
                    f"- 埋于ch{row.get('planted_chapter')}/应兑ch{row.get('due_chapter')}："
                    f"{str(row.get('description') or '')[:80]}"
                )
        return "\n".join(lines)

    # ---- 单幕精审 ------------------------------------------------------

    async def run_act_review(self, novel_id: str, act_number: int) -> Dict[str, Any]:
        """精审指定幕并落库，返回该幕 findings payload。"""
        acts = self.list_acts(novel_id)
        act = next((a for a in acts if a["act_number"] == act_number), None)
        if act is None:
            raise BookReviewError(f"幕不存在或无正文内容: act={act_number}")

        chapters = [c for c in (self._chapters().list_by_novel(NovelId(novel_id)) or []) if (c.content or "").strip()]
        chapters.sort(key=lambda c: c.number)
        in_act = [
            c for c in chapters if act["chapter_start"] <= c.number <= act["chapter_end"]
        ]
        if not in_act:
            raise BookReviewError(f"幕 {act_number} 范围内没有可用正文")

        act_text = "\n\n".join(
            f"### 第{c.number}章 {c.title or ''}\n{c.content.strip()}" for c in in_act
        )
        prompt = render_required_prompt(
            BOOK_ACT_REVIEW,
            {
                "act_number": str(act_number),
                "act_title": act["title"],
                "chapter_range": f"第{act['chapter_start']}-{act['chapter_end']}章（共{len(in_act)}章）",
                "act_text": act_text,
                "foreshadow_context": self._foreshadow_context(
                    novel_id, act["chapter_start"], act["chapter_end"]
                ),
                "character_digest": self._character_digest(novel_id),
            },
        )
        config = generation_config_from_profile(
            "review_json", model=self._model, response_format={"type": "json_object"}
        )
        response = await self._llm_service.generate(prompt, config)
        data, errors = parse_llm_json_to_dict(response.content)
        if not data:
            raise BookReviewError("单幕精审未返回有效 JSON" + (f"：{'; '.join(errors)}" if errors else ""))

        findings = self._normalize_findings(data.get("findings"))
        payload = {
            "act_number": act_number,
            "act_title": act["title"],
            "chapter_start": act["chapter_start"],
            "chapter_end": act["chapter_end"],
            "chapter_count": len(in_act),
            "summary": str(data.get("summary") or "").strip(),
            "quality_score": self._safe_score(data.get("quality_score")),
            "findings": findings,
        }
        self._save_report(novel_id, scope="act", act_number=act_number, payload=payload)
        return payload

    # ---- 汇总 ----------------------------------------------------------

    async def synthesize(self, novel_id: str) -> Dict[str, Any]:
        """汇总全部幕精审结果，生成并落库全书终审报告。"""
        act_rows = self._database().fetch_all(
            "SELECT act_number, payload FROM book_review_reports "
            "WHERE novel_id = ? AND scope = 'act' AND status = 'completed' ORDER BY act_number",
            (novel_id,),
        )
        if not act_rows:
            raise BookReviewError("尚无任何单幕精审结果，请先完成至少一幕的精审")

        act_findings = []
        for row in act_rows:
            try:
                act_findings.append(json.loads(row["payload"]))
            except Exception:
                continue

        db = self._database()
        novel_row = db.fetch_one(
            "SELECT title, premise, genre FROM novels WHERE id = ?", (novel_id,)
        )
        total_chapters = db.fetch_one(
            "SELECT COUNT(*) c FROM chapters WHERE novel_id = ? AND status = 'completed'",
            (novel_id,),
        )
        book_meta = (
            f"书名：{(novel_row['title'] if novel_row else '') or '未知'}\n"
            f"赛道：{(novel_row['genre'] if novel_row else '') or '未标注'}\n"
            f"梗概：{(novel_row['premise'] if novel_row else '') or '未提供'}\n"
            f"结构：已完成 {int(total_chapters['c']) if total_chapters else 0} 章，共 {len(act_findings)} 幕已精审"
        )

        prompt = render_required_prompt(
            BOOK_REVIEW_SYNTHESIS,
            {
                "book_meta": book_meta,
                "act_findings": json.dumps(act_findings, ensure_ascii=False),
                "foreshadow_ledger": self._foreshadow_ledger_overview(novel_id),
            },
        )
        config = generation_config_from_profile(
            "review_json", model=self._model, response_format={"type": "json_object"}
        )
        response = await self._llm_service.generate(prompt, config)
        data, errors = parse_llm_json_to_dict(response.content)
        if not data:
            raise BookReviewError("终审汇总未返回有效 JSON" + (f"：{'; '.join(errors)}" if errors else ""))

        payload = {
            "headline": str(data.get("headline") or "").strip(),
            "overall_score": self._safe_score(data.get("overall_score")),
            "scores": data.get("scores") if isinstance(data.get("scores"), dict) else {},
            "top_issues": self._normalize_top_issues(data.get("top_issues")),
            "promise_verdicts": data.get("promise_verdicts")
            if isinstance(data.get("promise_verdicts"), list)
            else [],
            "act_summaries": data.get("act_summaries") if isinstance(data.get("act_summaries"), list) else [],
        }
        self._save_report(novel_id, scope="final", act_number=None, payload=payload)
        return payload

    # ---- 查询 ----------------------------------------------------------

    def get_report(self, novel_id: str) -> Dict[str, Any]:
        """最近一次汇总报告 + 各幕精审进度（支持断点续审）。"""
        db = self._database()
        acts = self.list_acts(novel_id)
        reviewed: Dict[int, Dict[str, Any]] = {}
        for row in db.fetch_all(
            "SELECT act_number, payload, finding_count, updated_at FROM book_review_reports "
            "WHERE novel_id = ? AND scope = 'act' AND status = 'completed' ORDER BY act_number",
            (novel_id,),
        ):
            try:
                payload = json.loads(row["payload"])
            except Exception:
                continue
            reviewed[int(row["act_number"] or 0)] = {
                "act_number": int(row["act_number"] or 0),
                "quality_score": payload.get("quality_score"),
                "summary": payload.get("summary"),
                "finding_count": int(row["finding_count"] or 0),
                "updated_at": row["updated_at"],
            }

        final_row = db.fetch_one(
            "SELECT payload, updated_at FROM book_review_reports "
            "WHERE novel_id = ? AND scope = 'final' AND status = 'completed' "
            "ORDER BY updated_at DESC LIMIT 1",
            (novel_id,),
        )
        final_payload: Dict[str, Any] = {}
        final_at: Optional[str] = None
        if final_row:
            try:
                final_payload = json.loads(final_row["payload"])
                final_at = final_row["updated_at"]
            except Exception:
                final_payload = {}

        return {
            "novel_id": novel_id,
            "acts": [
                {
                    **act,
                    "reviewed": act["act_number"] in reviewed,
                    **({"review": reviewed[act["act_number"]]} if act["act_number"] in reviewed else {}),
                }
                for act in acts
            ],
            "reviewed_act_count": len(reviewed),
            "total_act_count": len(acts),
            "final_report": final_payload,
            "final_generated_at": final_at,
        }

    # ---- 内部工具 ------------------------------------------------------

    def _character_digest(self, novel_id: str, limit: int = 8) -> str:
        try:
            from infrastructure.persistence.database.unified_character_repository import (
                SqliteUnifiedCharacterRepository,
            )

            characters = SqliteUnifiedCharacterRepository(self._database()).list_by_novel(novel_id) or []
        except Exception as exc:
            logger.warning("读取人物卡失败: %s", exc)
            return "（人物卡不可用）"
        if not characters:
            return "（无人物卡）"
        lines = []
        for character in characters[:limit]:
            brief = (character.personality or character.description or "").strip()[:120]
            role = character.role or ""
            lines.append(f"- {character.name}({role})：{brief}" if role else f"- {character.name}：{brief}")
        return "\n".join(lines)

    def _save_report(
        self, novel_id: str, *, scope: str, act_number: Optional[int], payload: Dict[str, Any]
    ) -> None:
        finding_count = len(payload.get("findings") or payload.get("top_issues") or [])
        report_id = f"{novel_id}:{scope}:{act_number if act_number is not None else 'final'}"
        db = self._database()
        db.execute(
            """
            INSERT INTO book_review_reports (id, novel_id, scope, act_number, status, payload, finding_count)
            VALUES (?, ?, ?, ?, 'completed', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                payload = excluded.payload,
                finding_count = excluded.finding_count,
                status = 'completed',
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (report_id, novel_id, scope, act_number, json.dumps(payload, ensure_ascii=False), finding_count),
        )
        db.get_connection().commit()

    @staticmethod
    def _normalize_findings(raw: Any) -> List[Dict[str, Any]]:
        items = raw if isinstance(raw, list) else []
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            result.append(
                {
                    "severity": str(item.get("severity") or "minor"),
                    "category": str(item.get("category") or "logic"),
                    "chapter_number": BookReviewService._safe_int(item.get("chapter_number")),
                    "quote": str(item.get("quote") or ""),
                    "problem": str(item.get("problem") or ""),
                    "suggestion": str(item.get("suggestion") or ""),
                    "suggested_instruction": str(item.get("suggested_instruction") or ""),
                }
            )
        return result

    @staticmethod
    def _normalize_top_issues(raw: Any) -> List[Dict[str, Any]]:
        items = raw if isinstance(raw, list) else []
        result = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            chapters = item.get("chapter_numbers")
            result.append(
                {
                    "priority": BookReviewService._safe_int(item.get("priority"), default=index),
                    "severity": str(item.get("severity") or "major"),
                    "category": str(item.get("category") or "logic"),
                    "chapter_numbers": [c for c in (chapters if isinstance(chapters, list) else []) if isinstance(c, int)],
                    "title": str(item.get("title") or ""),
                    "detail": str(item.get("detail") or ""),
                    "quote": str(item.get("quote") or ""),
                    "suggestion": str(item.get("suggestion") or ""),
                    "suggested_instruction": str(item.get("suggested_instruction") or ""),
                }
            )
        return result

    @staticmethod
    def _safe_score(value: Any) -> int:
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
