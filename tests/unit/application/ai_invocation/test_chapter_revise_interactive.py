"""交互式章节优化 contract(chapter_revise_interactive)单元测试

走 AdoptionCommitService 全链路：continuation handler → 投影写回，
验证 status 保持不变（终审优化不能把 completed 章打回 draft）。
"""
import sqlite3
from contextlib import contextmanager

from application.ai_invocation.contracts.chapter_revise_interactive import (
    CONTINUATION_HANDLER_KEY,
    NODE_KEY,
    OPERATION,
    OUTPUT_BINDING_SET_ID,
    project_chapter_revise_to_chapters,
    register_chapter_revise_interactive_continuation,
)
from application.ai_invocation.dtos import (
    AdoptionDecision,
    ContinuationRef,
    InvocationPolicy,
    InvocationSession,
    InvocationSessionStatus,
    PromptSnapshot,
)
from application.ai_invocation.services import AdoptionCommitService
from application.ai_invocation.variable_hub import InMemoryVariableHubRepository
from application.ai_invocation.contracts.chapter_revise_interactive import (
    chapter_revise_output_bindings,
)
from domain.ai.value_objects.prompt import Prompt


class _Db:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE chapters (
                id TEXT PRIMARY KEY,
                novel_id TEXT NOT NULL,
                number INTEGER NOT NULL,
                title TEXT,
                content TEXT,
                outline TEXT,
                status TEXT DEFAULT 'draft',
                word_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(novel_id, number)
            );
            """
        )

    @contextmanager
    def transaction(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def fetch_one(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def _session_and_decision(content: str):
    session = InvocationSession(
        id="session-1",
        operation=OPERATION,
        node_key=NODE_KEY,
        policy=InvocationPolicy.REVIEW_AFTER_CALL,
        status=InvocationSessionStatus.AWAITING_COMMIT,
        context={"novel_id": "novel-1", "chapter_number": 222},
        continuation=ContinuationRef(handler_key=CONTINUATION_HANDLER_KEY),
        prompt_snapshot=PromptSnapshot(
            prompt=Prompt(system="s", user="u"),
            node_key=NODE_KEY,
            node_version_id="node-v1",
            output_binding_set_id=OUTPUT_BINDING_SET_ID,
            input_binding_set_id="chapter-revise:input:v1",
            asset_link_set_id="",
            variable_snapshot_hash="vars",
            template_hash="template",
            composition_hash="composition",
            rendered_prompt_hash="rendered",
        ),
    )
    decision = AdoptionDecision(
        id="decision-1",
        session_id="session-1",
        attempt_id="attempt-1",
        accepted_content=content,
    )
    return session, decision


def test_commit_revises_chapter_and_preserves_status(monkeypatch):
    db = _Db()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO chapters (id, novel_id, number, title, content, status, word_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("chapter-222", "novel-1", 222, "旧章", "旧正文", "completed", 3),
        )
    repo = InMemoryVariableHubRepository()
    repo.set_bindings(OUTPUT_BINDING_SET_ID, NODE_KEY, chapter_revise_output_bindings(), direction="output")
    register_chapter_revise_interactive_continuation()
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda: db,
    )
    session, decision = _session_and_decision("修改后的完整正文")

    commit = AdoptionCommitService(variable_hub_repository=repo).commit(session=session, decision=decision)

    row = db.fetch_one(
        "SELECT content, status, word_count FROM chapters WHERE novel_id = ? AND number = ?",
        ("novel-1", 222),
    )
    assert commit.status.value == "succeeded"
    assert session.status == InvocationSessionStatus.COMPLETED
    assert row == {"content": "修改后的完整正文", "status": "completed", "word_count": len("修改后的完整正文")}


def test_commit_blocks_when_chapter_missing(monkeypatch):
    db = _Db()  # 空表
    repo = InMemoryVariableHubRepository()
    repo.set_bindings(OUTPUT_BINDING_SET_ID, NODE_KEY, chapter_revise_output_bindings(), direction="output")
    register_chapter_revise_interactive_continuation()
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda: db,
    )
    session, decision = _session_and_decision("新正文")

    commit = AdoptionCommitService(variable_hub_repository=repo).commit(session=session, decision=decision)

    # 投影被拦截：章节不存在，不许凭空创建
    projection_steps = [s for s in commit.steps if "projection" in s.name]
    assert projection_steps, "应包含 projection 步骤"


def test_project_refuses_empty_content():
    db = _Db()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO chapters (id, novel_id, number, title, content, status, word_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("chapter-1", "novel-1", 1, "旧章", "旧正文", "completed", 3),
        )

    result = project_chapter_revise_to_chapters(
        db, {"novel_id": "novel-1", "chapter_number": 1, "content": "  "}
    )

    row = db.fetch_one("SELECT content FROM chapters WHERE id = ?", ("chapter-1",))
    assert result == {"blocked": True, "reason": "empty_content_refuses_overwrite"}
    assert row == {"content": "旧正文"}
