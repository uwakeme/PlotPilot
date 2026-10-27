"""Autopilot continuation handlers."""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from application.ai.llm_json_extract import extract_outer_json_value, repair_json, strip_json_fences
from application.ai_invocation.continuation import ContinuationContext, register_continuation_handler
from application.ai_invocation.dtos import InvocationPolicy
from application.ai_invocation.autopilot.shared_state import write_autopilot_shared_state
from application.audit.services.character_whitelist_check import (  # noqa: E402
    CONFIRMED,
    REJECTED,
    REJECTED_CATEGORY,
    classify_rogue_character_hit,
    is_rogue_category,
)

logger = logging.getLogger(__name__)


def _position_bucket(start: int, total: int) -> str:
    """命中位置的大致段落（与 LLM 通道的措辞对齐）。"""
    if total <= 0:
        return "前段"
    ratio = start / total
    if ratio < 1 / 3:
        return "前段"
    if ratio < 2 / 3:
        return "中段"
    return "后段"


def _code_channel_risk_flags(novel_id: str, chapter_number: int) -> list[dict[str, Any]]:
    """**代码通道**（可机检）的命中 —— 与 LLM 通道的结论**汇总到一处**。

    ⚠ 第 16 批：``risk_flags`` 之前**恒为空** ——
    ``continuations._audit`` 读 ``payload.get("risk_flags", [])``，
    而 LLM 的 JSON 契约里**从来没有这个键** ⇒ 面板上的「风险标记」永远是 0。
    而代码通道算出来的分数/命中走的是**另一条路**（``anti_ai_audits`` 表 + 共享状态）
    ⇒ 两条通道的结论**没有汇总到一处**。

    ⇒ 现在：代码通道的命中与 LLM 通道的命中合并进同一个 ``risk_flags``。
    """
    if not novel_id or chapter_number <= 0:
        return []
    try:
        from infrastructure.persistence.database.connection import get_database

        row = get_database().fetch_one(
            "SELECT content FROM chapters WHERE novel_id = ? AND number = ?",
            (novel_id, chapter_number),
        )
        content = str((row or {}).get("content") or "")
        if not content.strip():
            return []

        from application.audit.services.cliche_scanner import ClicheScanner

        hits = ClicheScanner().scan_cliches(content)
        return [
            {
                "source": "code",
                "category": hit.category,
                "pattern": hit.pattern,
                "text": hit.text,
                "position": _position_bucket(hit.start, len(content)),
                "severity": hit.severity,
                "replacement": hit.replacement_hint,
            }
            for hit in hits
        ]
    except Exception as exc:  # noqa: BLE001 —— 取不到就当没有，不许把审计搞崩
        logger.debug("代码通道审计命中读取失败 novel=%s ch=%s: %s", novel_id, chapter_number, exc)
        return []


def _whitelist_variants(novel_id: str) -> list[str]:
    """取该书的**角色白名单变体**（K 类核对的依据）。

    ⚠ 与生成侧 / 提示词侧**同源**：白名单来自 ``memory_engine.character_names``
    （Bible 的角色名单）⇒ 不会「审计用的白名单」与「生成时锁的白名单」分叉。
    ⚠ 取不到 ⇒ 返回空 ⇒ 核对**不下结论**（保守：宁可留着让人看）。
    """
    if not novel_id:
        return []
    try:
        from domain.novel.value_objects.novel_id import NovelId
        from infrastructure.persistence.database.connection import get_database
        from infrastructure.persistence.database.sqlite_bible_repository import (
            SqliteBibleRepository,
        )

        from application.audit.services.character_whitelist_check import expand_whitelist
        from application.engine.services.memory_engine import (
            FactLockBuilder,
            character_names,
        )

        builder = FactLockBuilder(SqliteBibleRepository(get_database()))
        bible = builder.bible_repository.get_by_novel_id(NovelId(novel_id))
        if not bible:
            return []
        return sorted(expand_whitelist(character_names(bible)))
    except Exception as exc:  # noqa: BLE001 —— 取不到就当没有白名单，不许臆测
        logger.debug("取角色白名单变体失败（K 类核对将不下结论）novel=%s: %s", novel_id, exc)
        return []


def _merge_risk_flags(
    payload: Mapping[str, Any],
    code_flags: list[dict[str, Any]],
    *,
    whitelist_variants: list[str] | tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """把两条通道的命中合并成一份（LLM 通道优先，去重）。

    ⚠ 第 22 批：**K 类（越界角色）会被确定性核对**。
    LLM 报的角色名若落在白名单变体集合里 ⇒ **驳回**（误报）
    ⇒ 以 ``越界角色·机检驳回`` 单独一条留下（**不许静默丢**），
    并与真越界**可分辨**（类别不同、severity=info）。
    白名单取不到 / 名字判不了 ⇒ **不下结论**（原样保留）。
    """
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _add(item: Mapping[str, Any], source: str) -> None:
        category = str(item.get("category") or "")
        text = str(item.get("text") or item.get("pattern") or "")
        key = (category, text)
        if not text or key in seen:
            return
        seen.add(key)
        entry = dict(item)
        entry.setdefault("source", source)
        merged.append(entry)

    if isinstance(payload, Mapping):
        for item in payload.get("hits") or []:
            if not isinstance(item, Mapping):
                continue
            entry = {
                "category": item.get("category", ""),
                "pattern": item.get("pattern", ""),
                "text": item.get("pattern", ""),
                "position": item.get("position", ""),
                "severity": item.get("severity", ""),
                "replacement": item.get("replacement", ""),
            }
            if is_rogue_category(entry.get("category", "")):
                verdict, name = classify_rogue_character_hit(
                    entry.get("pattern", ""), whitelist_variants
                )
                if verdict == REJECTED:
                    entry = {
                        "category": REJECTED_CATEGORY,
                        "pattern": f"机检驳回：白名单内角色「{name}」",
                        "text": name,
                        "position": entry.get("position", ""),
                        "severity": "info",
                        "replacement": "该角色在白名单内，不算越界角色",
                        "verified": "rejected_by_whitelist_check",
                    }
                elif verdict == CONFIRMED:
                    entry["verified"] = "confirmed_out_of_whitelist"
            _add(entry, "llm")
    for item in code_flags:
        _add(item, "code")
    return merged


def _load_json_value(content: str, error_code: str) -> Any:
    """Parse an accepted LLM JSON payload after normal LLM cleanup/repair."""
    raw = str(content or "")
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        try:
            cleaned = extract_outer_json_value(strip_json_fences(raw))
            payload = json.loads(repair_json(cleaned))
        except Exception as exc:
            raise ValueError(error_code) from exc
    return payload


def _load_json_object(content: str, error_code: str) -> dict[str, Any]:
    payload = _load_json_value(content, error_code)
    if not isinstance(payload, dict):
        raise ValueError(error_code)
    return payload


def _sectioned_markdown(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = ""
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("##"):
            current = line.lstrip("#").strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections.setdefault(current, []).append(raw_line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def _list_lines(text: str) -> list[str]:
    items: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = line.lstrip("-* ").strip()
        line = line.split(".", 1)[1].strip() if line[:1].isdigit() and "." in line[:4] else line
        if line:
            items.append(line)
    return items


def _parse_tuple_line(text: str) -> list[str]:
    line = str(text or "").strip().strip("()")
    if not line:
        return []
    return [part.strip() for part in line.split(",") if part.strip()]


def _parse_aftermath_markdown(content: str) -> dict[str, Any]:
    sections = _sectioned_markdown(content)
    if not sections:
        return {}
    summary = sections.get("摘要") or sections.get("章节摘要") or ""
    event_text = sections.get("事件") or sections.get("关键事件") or ""
    triples_text = sections.get("实体关系三元组") or sections.get("三元组") or ""
    causal_text = sections.get("因果边") or ""
    triples = []
    for item in _list_lines(triples_text):
        parts = _parse_tuple_line(item)
        if len(parts) >= 3:
            triples.append({"subject": parts[0], "predicate": parts[1], "object": ",".join(parts[2:]).strip()})
    causal_edges = []
    for item in _list_lines(causal_text):
        edge = item.strip().strip("()")
        if "→" in edge:
            cause, effect = edge.split("→", 1)
            causal_edges.append({"cause": cause.strip(), "effect": effect.strip()})
        elif "->" in edge:
            cause, effect = edge.split("->", 1)
            causal_edges.append({"cause": cause.strip(), "effect": effect.strip()})
    return {
        "summary": summary.strip(),
        "events": _list_lines(event_text),
        "state_delta": {
            "relation_triples": triples,
            "causal_edges": causal_edges,
        },
        "foreshadow_updates": [],
    }


def _load_aftermath_payload(content: str) -> dict[str, Any]:
    try:
        return _load_json_object(content, "autopilot_aftermath_requires_json_object")
    except ValueError as exc:
        payload = _parse_aftermath_markdown(content)
        if payload.get("summary"):
            return payload
        raise exc


def _publish_shared_state(novel_id: str, **fields: Any) -> None:
    if not write_autopilot_shared_state(novel_id, **fields):
        logger.debug("autopilot continuation shared state publish skipped: novel=%s", novel_id)


def _clear_invocation_state(novel_id: str, **extra: Any) -> None:
    _publish_shared_state(
        novel_id,
        active_invocation_session_id="",
        active_invocation_operation="",
        active_invocation_node_key="",
        active_invocation_status="completed",
        active_invocation_policy="",
        has_active_invocation=False,
        requires_ai_review=False,
        autopilot_pause_reason="",
        **extra,
    )


def _resume_autopilot_stage(novel_id: str, stage: str, **fields: Any) -> None:
    try:
        from application.paths import get_db_path
        from infrastructure.persistence.database.connection import get_database
        from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue

        db = get_database(get_db_path())
        set_parts = [
            "autopilot_status = 'running'",
            "current_stage = ?",
            "updated_at = CURRENT_TIMESTAMP",
        ]
        params: list[Any] = [stage]
        for key, value in fields.items():
            set_parts.append(f"{key} = ?")
            params.append(value)
        params.append(novel_id)
        with sqlite_writes_bypass_queue():
            db.execute(
                f"UPDATE novels SET {', '.join(set_parts)} WHERE id = ?",
                tuple(params),
            )
            db.commit()
    except Exception as exc:
        logger.warning(
            "autopilot continuation failed to resume stage in DB: novel=%s stage=%s error=%s",
            novel_id,
            stage,
            exc,
        )
    try:
        from application.engine.services.novel_stop_signal import publish_start_signal

        publish_start_signal(novel_id)
    except Exception as exc:
        logger.debug("autopilot continuation start signal skipped: %s", exc)


def _chapter_content_already_contains(prior: str, addition: str) -> bool:
    prior = str(prior or "").strip()
    addition = str(addition or "").strip()
    if not prior or not addition:
        return False
    return prior == addition or prior.endswith(f"\n\n{addition}") or prior.endswith(addition)


def _write_chapter_draft(
    novel_id: str,
    chapter_number: int,
    content: str,
    *,
    append: bool,
    status: str,
) -> dict[str, Any]:
    try:
        from application.paths import get_db_path
        from infrastructure.persistence.database.connection import get_database
        from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue

        db = get_database(get_db_path())
        with sqlite_writes_bypass_queue():
            row = db.fetch_one(
                "SELECT id, title, outline, content, status FROM chapters WHERE novel_id = ? AND number = ?",
                (novel_id, chapter_number),
            )
            prior = str((row or {}).get("content") or "").strip()
            addition = str(content or "").strip()
            prior_status = str((row or {}).get("status") or "") if row else ""
            duplicate_content = False
            if append:
                duplicate_content = _chapter_content_already_contains(prior, addition)
                merged = prior
                if addition and not duplicate_content:
                    merged = f"{prior}\n\n{addition}".strip() if prior else addition
            else:
                merged = addition
                duplicate_content = bool(addition) and prior == addition

            if not append and not addition:
                return {
                    "word_count": len(prior),
                    "skipped": True,
                    "reason": "empty_content_refuses_overwrite",
                    "duplicate_content": False,
                    "completed_transition": False,
                }
            if append and not addition:
                return {
                    "word_count": len(prior),
                    "skipped": True,
                    "reason": "empty_content_refuses_append",
                    "duplicate_content": False,
                    "completed_transition": False,
                }
            word_count = len(merged)
            if duplicate_content and row is not None and (append or prior_status == status):
                return {
                    "word_count": word_count,
                    "skipped": True,
                    "reason": "duplicate_content",
                    "duplicate_content": True,
                    "completed_transition": False,
                    "status": prior_status,
                }
            if row is None:
                db.execute(
                    """
                    INSERT INTO chapters (
                        id, novel_id, number, title, content, outline, status, word_count,
                        tension_score, plot_tension, emotional_tension, pacing_tension,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (
                        f"autopilot:{novel_id}:{chapter_number}",
                        novel_id,
                        chapter_number,
                        f"第{chapter_number}章",
                        merged,
                        "",
                        status,
                        word_count,
                    ),
                )
                action = "inserted"
            else:
                db.execute(
                    """
                    UPDATE chapters
                    SET content = ?, status = ?, word_count = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE novel_id = ? AND number = ?
                    """,
                    (merged, status, word_count, novel_id, chapter_number),
                )
                action = "updated"
            db.commit()
            return {
                "word_count": word_count,
                "skipped": False,
                "action": action,
                "duplicate_content": duplicate_content,
                "completed_transition": status == "completed" and prior_status != "completed",
                "status": status,
            }
    except Exception as exc:
        logger.warning(
            "autopilot continuation failed to write chapter draft: novel=%s chapter=%s error=%s",
            novel_id,
            chapter_number,
            exc,
        )
        return {"word_count": 0, "skipped": True, "reason": str(exc), "completed_transition": False}


def _append_chapter_draft(novel_id: str, chapter_number: int, content: str) -> int:
    result = _write_chapter_draft(
        novel_id,
        chapter_number,
        content,
        append=True,
        status="draft",
    )
    return int(result.get("word_count") or 0)


def _resume_after_full_chapter_accept(
    novel_id: str,
    chapter_number: int,
    *,
    completed_transition: bool,
) -> None:
    try:
        from application.paths import get_db_path
        from infrastructure.persistence.database.connection import get_database
        from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue

        db = get_database(get_db_path())
        set_parts = [
            "autopilot_status = 'running'",
            "current_stage = 'auditing'",
            "current_beat_index = 0",
            "beats_completed = 0",
            "updated_at = CURRENT_TIMESTAMP",
        ]
        if completed_transition:
            set_parts.extend(
                [
                    "current_auto_chapters = COALESCE(current_auto_chapters, 0) + 1",
                    "current_chapter_in_act = COALESCE(current_chapter_in_act, 0) + 1",
                ]
            )
        with sqlite_writes_bypass_queue():
            db.execute(
                f"UPDATE novels SET {', '.join(set_parts)} WHERE id = ?",
                (novel_id,),
            )
            db.commit()
    except Exception as exc:
        logger.warning(
            "autopilot continuation failed to advance full chapter: novel=%s chapter=%s error=%s",
            novel_id,
            chapter_number,
            exc,
        )
    try:
        from application.engine.services.novel_stop_signal import publish_start_signal

        publish_start_signal(novel_id)
    except Exception as exc:
        logger.debug("autopilot continuation start signal skipped: %s", exc)


def register_autopilot_continuations() -> None:
    def _outline_partition(ctx: ContinuationContext) -> Mapping[str, Any]:
        payload = _load_json_object(
            ctx.decision.accepted_content,
            "autopilot_outline_partition_requires_json_object",
        )
        novel_id = str(ctx.session.context.get("novel_id") or "")
        chapter_number = ctx.session.context.get("chapter_number")
        micro_beats = payload.get("atoms") or payload.get("micro_beats") or []
        if not isinstance(micro_beats, list) or not micro_beats:
            raise ValueError("autopilot_outline_partition_requires_non_empty_atoms")
        if ctx.session.policy == InvocationPolicy.DIRECT:
            return {
                "atoms": micro_beats,
                "chapter_plan": payload,
                "chapter_number": chapter_number,
                "planned_micro_beats": micro_beats,
                "outline_plan_mode": payload.get("mode") or "autopilot_outline_partition",
            }
        if novel_id:
            _clear_invocation_state(
                novel_id,
                autopilot_status="running",
                current_stage="writing",
                autopilot_pending_chapter_number=chapter_number,
                autopilot_pending_chapter_plan=payload,
                planned_micro_beats=micro_beats,
                outline_plan_mode=payload.get("mode") or "autopilot_outline_partition",
            )
            _resume_autopilot_stage(novel_id, "writing")
        return {
            "atoms": micro_beats,
            "chapter_plan": payload,
            "chapter_number": chapter_number,
            "planned_micro_beats": micro_beats,
            "outline_plan_mode": payload.get("mode") or "autopilot_outline_partition",
        }

    register_continuation_handler("autopilot_outline_partition", _outline_partition)

    def _macro_plan(ctx: ContinuationContext) -> Mapping[str, Any]:
        payload = _load_json_value(
            ctx.decision.accepted_content,
            "autopilot_macro_plan_requires_json_object",
        )
        if isinstance(payload, list):
            payload = {"parts": payload}
        if not isinstance(payload, dict):
            raise ValueError("autopilot_macro_plan_requires_json_object")
        parts = payload.get("parts") or payload.get("structure") or []
        if not isinstance(parts, list) or not parts:
            raise ValueError("autopilot_macro_plan_requires_non_empty_parts")

        novel_id = str(ctx.session.context.get("novel_id") or "")
        target_chapters = int(ctx.session.context.get("target_chapters") or 0)
        result = {
            "success": True,
            "structure": parts,
            "quality_metrics": {},
            "generation_time": 0,
        }
        if novel_id and ctx.session.policy != InvocationPolicy.DIRECT:
            _clear_invocation_state(
                novel_id,
                autopilot_status="running",
                current_stage="macro_planning",
                autopilot_pending_macro_plan=result,
                autopilot_pending_macro_target_chapters=target_chapters,
                macro_structure_ready=False,
                writing_substep="macro_planning",
                writing_substep_label="宏观规划 · 等待结构落库",
            )
            _resume_autopilot_stage(novel_id, "macro_planning")
        return {
            "parts": parts,
            "macro_plan": result,
            "target_chapters": target_chapters,
        }

    def _act_plan(ctx: ContinuationContext) -> Mapping[str, Any]:
        from application.blueprint.services.chapter_planning_policy import validate_lightweight_act_plan

        payload = _load_json_object(
            ctx.decision.accepted_content,
            "autopilot_act_plan_requires_json_object",
        )
        chapters = payload.get("chapters") or []
        if not isinstance(chapters, list) or not chapters:
            raise ValueError("autopilot_act_plan_requires_non_empty_chapters")
        expected_count = int(ctx.session.context.get("chapter_count") or 0)
        errors = validate_lightweight_act_plan(chapters, expected_count=expected_count)
        if errors:
            raise ValueError("autopilot_act_plan_incomplete_or_truncated: " + "; ".join(errors))

        novel_id = str(ctx.session.context.get("novel_id") or "")
        act_id = str(ctx.session.context.get("act_id") or "")
        if novel_id and act_id and ctx.session.policy != InvocationPolicy.DIRECT:
            _clear_invocation_state(
                novel_id,
                autopilot_status="running",
                current_stage="act_planning",
                autopilot_pending_act_plan_id=act_id,
                autopilot_pending_act_chapters=chapters,
            )
            _resume_autopilot_stage(novel_id, "act_planning")
        return {
            "chapters": chapters,
            "act_id": act_id,
            "act_plan": {
                "success": True,
                "act_id": act_id,
                "chapters": chapters,
            },
        }

    def _prose_generation(ctx: ContinuationContext) -> Mapping[str, Any]:
        content = ctx.decision.accepted_content or ""
        novel_id = str(ctx.session.context.get("novel_id") or "")
        chapter_number = int(ctx.session.context.get("chapter_number") or 0)
        beat_index = int(ctx.session.context.get("beat_index") or 0)
        session_meta = dict(ctx.session.metadata or {})
        if novel_id and ctx.session.policy != InvocationPolicy.DIRECT and chapter_number > 0:
            is_full_chapter = ctx.session.operation == "autopilot.chapter.prose"
            story_pipeline_commit_owner = (
                is_full_chapter
                and session_meta.get("commit_owner") == "story_pipeline_save_step"
            )
            if story_pipeline_commit_owner:
                _clear_invocation_state(
                    novel_id,
                    autopilot_status="running",
                    current_stage="writing",
                    current_beat_index=0,
                    current_chapter_number=chapter_number,
                    accumulated_words=len(content),
                    writing_substep="chapter_generated",
                    writing_substep_label="正文生成完成，等待管线提交",
                )
                _resume_autopilot_stage(novel_id, "writing", current_beat_index=0)
                return {
                    "content": content,
                    "beat_content": content,
                    "chapter_number": chapter_number,
                    "beat_index": beat_index,
                    "commit_owner": "story_pipeline_save_step",
                }
            write_result = _write_chapter_draft(
                novel_id,
                chapter_number,
                content,
                append=not is_full_chapter,
                status="completed" if is_full_chapter else "draft",
            )
            total_words = int(write_result.get("word_count") or 0)
            if is_full_chapter:
                _clear_invocation_state(
                    novel_id,
                    autopilot_status="running",
                    current_stage="auditing",
                    current_beat_index=0,
                    current_chapter_number=chapter_number,
                    accumulated_words=total_words,
                    writing_substep="audit_pending",
                    writing_substep_label="等待章节审计",
                )
                _resume_after_full_chapter_accept(
                    novel_id,
                    chapter_number,
                    completed_transition=bool(write_result.get("completed_transition")),
                )
            elif write_result.get("duplicate_content"):
                _clear_invocation_state(
                    novel_id,
                    autopilot_status="running",
                    current_stage="writing",
                    current_chapter_number=chapter_number,
                    accumulated_words=total_words,
                )
                _resume_autopilot_stage(novel_id, "writing")
            else:
                next_beat_index = beat_index + 1
                _clear_invocation_state(
                    novel_id,
                    autopilot_status="running",
                    current_stage="writing",
                    current_beat_index=next_beat_index,
                    current_chapter_number=chapter_number,
                    accumulated_words=total_words,
                )
                _resume_autopilot_stage(
                    novel_id,
                    "writing",
                    current_beat_index=next_beat_index,
                )
        return {
            "content": content,
            "beat_content": content,
            "chapter_number": chapter_number,
            "beat_index": beat_index,
        }

    def _audit(ctx: ContinuationContext) -> Mapping[str, Any]:
        payload = _load_json_object(
            ctx.decision.accepted_content,
            "autopilot_audit_requires_json_object",
        )
        novel_id = str(ctx.session.context.get("novel_id") or "")
        chapter_number = int(ctx.session.context.get("chapter_number") or 0)
        # ⚠ 第 16 批：``risk_flags`` 之前恒为空（读的键 LLM 从来不输出）。
        # ⇒ 现在由**两条通道**派生：LLM 的 hits + 代码通道（可机检）的命中。
        # ⚠ 第 22 批：K 类（越界角色）额外做**确定性核对**（白名单变体比对）
        #   ⇒ LLM 把白名单内角色报成越界时会被驳回（并留痕）。
        risk_flags = _merge_risk_flags(
            payload if isinstance(payload, Mapping) else {},
            _code_channel_risk_flags(novel_id, chapter_number),
            whitelist_variants=_whitelist_variants(novel_id),
        )
        if isinstance(payload, dict):
            payload = {**payload, "risk_flags": risk_flags}
        if novel_id and ctx.session.policy != InvocationPolicy.DIRECT and chapter_number > 0:
            _clear_invocation_state(
                novel_id,
                autopilot_status="running",
                current_stage="auditing",
                autopilot_pending_audit_chapter_number=chapter_number,
                autopilot_pending_audit_report=payload,
            )
            _resume_autopilot_stage(novel_id, "auditing")
        return {
            "chapter_audit_report": payload,
            "chapter.audit.report": payload,
            "chapter.audit.risk_flags": risk_flags,
        }

    def _aftermath(ctx: ContinuationContext) -> Mapping[str, Any]:
        payload = _load_aftermath_payload(ctx.decision.accepted_content)
        novel_id = str(ctx.session.context.get("novel_id") or "")
        chapter_number = int(ctx.session.context.get("chapter_number") or 0)
        if novel_id and ctx.session.policy != InvocationPolicy.DIRECT and chapter_number > 0:
            _clear_invocation_state(
                novel_id,
                autopilot_status="running",
                current_stage="auditing",
                autopilot_pending_aftermath_chapter_number=chapter_number,
                autopilot_pending_aftermath_payload=payload,
            )
            _resume_autopilot_stage(novel_id, "auditing")
        return {
            "chapter_aftermath": payload,
            "chapter.summary": payload.get("summary", "") if isinstance(payload, dict) else "",
            "chapter.state_delta": payload.get("state_delta", {}) if isinstance(payload, dict) else {},
            "chapter.foreshadow_updates": payload.get("foreshadow_updates", []) if isinstance(payload, dict) else [],
        }

    register_continuation_handler("autopilot_prose_generation", _prose_generation)
    register_continuation_handler("autopilot_macro_plan", _macro_plan)
    register_continuation_handler("autopilot_act_plan", _act_plan)
    register_continuation_handler("autopilot_audit", _audit)
    register_continuation_handler("autopilot_after_chapter_extract", _aftermath)
