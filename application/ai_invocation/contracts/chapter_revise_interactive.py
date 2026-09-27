"""Contracts and continuations for interactive chapter revision (工作台章节优化).

与 chapter_prose_generation 同构：用户给自由优化指令，模型改写整章正文，
采纳后写回章节。差异点：
- 默认策略 REVIEW_AFTER_CALL（先按指令生成，预览确认后才写回）；
- 投影走专用适配器 ``chapters_table_revise``——只更新正文与字数，
  **保持章节 status 不变**（终审优化发生在完稿书上，不能把 completed 章打回 draft）。
"""
from __future__ import annotations

from typing import Any

from application.ai_invocation.continuation import ContinuationContext, register_continuation_handler
from application.ai_invocation.dtos import InvocationPolicy, InvocationSpec, VariableBinding
from infrastructure.ai.prompt_keys import CHAPTER_REVISE
from infrastructure.persistence.database.chapter_draft_repository import snapshot_chapter_before_content_change
from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue


OPERATION = "chapter.revise.interactive"
NODE_KEY = CHAPTER_REVISE
INPUT_BINDING_SET_ID = "chapter-revise:input:v1"
OUTPUT_BINDING_SET_ID = "chapter-revise:output:v1"
CONTINUATION_HANDLER_KEY = "chapter_revise_interactive_commit"
PROJECTION_KEY = "chapter_revise_to_chapters_v1"


def chapter_revise_input_bindings() -> list[VariableBinding]:
    return [
        VariableBinding("chapter_number", "chapter.number", True, scope="chapter", stage="review", value_type="integer", display_name="章节编号"),
        VariableBinding("chapter_title", "chapter.title", False, "", scope="chapter", stage="review", display_name="章节标题"),
        VariableBinding("chapter_content", "chapter.content", True, scope="chapter", stage="review", display_name="章节当前正文"),
        VariableBinding("user_instruction", "chapter.revise.instruction", True, scope="chapter", stage="review", display_name="优化指令"),
        VariableBinding("style_contract", "chapter.style_contract", False, "", scope="chapter", stage="review", display_name="文风契约摘要"),
        VariableBinding("continuity_context", "chapter.continuity_context", False, "", scope="chapter", stage="review", display_name="前后文承接摘要"),
    ]


def chapter_revise_output_bindings() -> list[VariableBinding]:
    return [
        VariableBinding("content", "chapter.revise.proposed", True, scope="chapter", stage="review", display_name="修改稿正文"),
        VariableBinding("accepted_content", "chapter.revise.accepted", True, scope="chapter", stage="review", display_name="采纳正文"),
        VariableBinding("revision_notes", "chapter.revise.notes", False, scope="chapter", stage="review", display_name="修改说明"),
    ]


def ensure_chapter_revise_interactive_contract(db) -> InvocationSpec:
    from infrastructure.ai.prompt_manager import get_prompt_manager
    from infrastructure.ai.prompt_registry import get_prompt_registry
    from infrastructure.persistence.database.sqlite_ai_invocation_repository import (
        SqliteInvocationSpecRepository,
        SqliteVariableHubRepository,
    )

    get_prompt_manager().ensure_seeded()
    node = get_prompt_registry().get_node(NODE_KEY)
    if node is None:
        raise RuntimeError(f"CPMS node is not published: {NODE_KEY}")
    node_version_id = getattr(node, "active_version_id", None) or ""
    if not node_version_id:
        raise RuntimeError(f"CPMS node has no active version: {NODE_KEY}")

    spec = InvocationSpec(
        operation=OPERATION,
        node_key=NODE_KEY,
        prompt_node_version_id=node_version_id,
        input_binding_set_id=INPUT_BINDING_SET_ID,
        output_binding_set_id=OUTPUT_BINDING_SET_ID,
        default_policy=InvocationPolicy.REVIEW_AFTER_CALL,
        risk_level="medium",
        supports_stream=False,
        continuation_handler_key=CONTINUATION_HANDLER_KEY,
        commit_policy_key=f"projection:{PROJECTION_KEY}",
        metadata={
            "projection_key": PROJECTION_KEY,
            "projection": {
                "source": {"variable_key": "chapter.revise.accepted"},
                "target": {"adapter": "chapters_table_revise", "fields": {"content": "$.value", "word_count": "$.computed.length"}},
                "context": {"novel_id": "required", "chapter_number": "required"},
            },
        },
    )
    with sqlite_writes_bypass_queue():
        variable_repo = SqliteVariableHubRepository(db)
        variable_repo.set_bindings(INPUT_BINDING_SET_ID, NODE_KEY, chapter_revise_input_bindings(), direction="input")
        variable_repo.set_bindings(OUTPUT_BINDING_SET_ID, NODE_KEY, chapter_revise_output_bindings(), direction="output")
        SqliteInvocationSpecRepository(db).upsert(
            spec,
            spec_id=f"spec:{NODE_KEY}:v1",
            spec_version=1,
            status="published",
        )
    register_chapter_revise_interactive_continuation()
    return spec


def register_chapter_revise_interactive_continuation() -> None:
    register_continuation_handler(CONTINUATION_HANDLER_KEY, _chapter_revise_commit)


def _chapter_revise_commit(context: ContinuationContext) -> dict[str, Any]:
    content = (context.decision.accepted_content or "").strip()
    if not content:
        raise ValueError("accepted revised content is empty")

    return {
        "content": content,
        "accepted_content": content,
        "revision_notes": {
            "source": OPERATION,
            "session_id": context.session.id,
            "attempt_id": context.decision.attempt_id,
        },
        "_projection": {
            "projection_key": PROJECTION_KEY,
            "adapter": "chapters_table_revise",
            "novel_id": str(context.session.context.get("novel_id") or ""),
            "chapter_number": context.session.context.get("chapter_number"),
            "content": content,
            "word_count": len(content.replace(" ", "")),
            "idempotency_key": f"{context.session.id}:{context.decision.id}:{PROJECTION_KEY}",
        },
    }


def project_chapter_revise_to_chapters(db, projection: dict[str, Any]) -> dict[str, Any]:
    """把采纳的修改稿写回章节——只更新正文与字数，status 保持原值。"""
    novel_id = str(projection.get("novel_id") or "").strip()
    chapter_number = int(projection.get("chapter_number") or 0)
    content = str(projection.get("content") or "")
    if not novel_id or chapter_number <= 0:
        return {"blocked": True, "reason": "missing_projection_context"}
    if not content.strip():
        return {"blocked": True, "reason": "empty_content_refuses_overwrite"}

    # AI 优化覆写前留痕
    snapshot_chapter_before_content_change(db, novel_id, chapter_number, "pre_revise")
    existing = db.fetch_one(
        "SELECT id, status FROM chapters WHERE novel_id = ? AND number = ?",
        (novel_id, chapter_number),
    )
    if existing is None:
        return {"blocked": True, "reason": "chapter_not_found"}
    word_count = int(projection.get("word_count") or len(content.replace(" ", "")))
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE chapters
            SET content = ?, word_count = ?, updated_at = CURRENT_TIMESTAMP
            WHERE novel_id = ? AND number = ?
            """,
            (content, word_count, novel_id, chapter_number),
        )
    return {
        "skipped": False,
        "projection_key": projection.get("projection_key") or PROJECTION_KEY,
        "adapter": "chapters_table_revise",
        "action": "revised",
        "chapter_id": existing["id"],
        "novel_id": novel_id,
        "chapter_number": chapter_number,
        "word_count": word_count,
        "status_preserved": existing["status"],
    }
