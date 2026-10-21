import sqlite3
import json
import logging
from types import SimpleNamespace

from application.engine.services.autopilot_recovery_policy import AutopilotRecoveryPolicy


class _Db:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE novels (
                id TEXT PRIMARY KEY,
                current_stage TEXT DEFAULT 'writing',
                autopilot_status TEXT DEFAULT 'running'
            );
            CREATE TABLE chapters (
                id TEXT PRIMARY KEY,
                novel_id TEXT NOT NULL,
                number INTEGER NOT NULL,
                status TEXT DEFAULT 'draft',
                content TEXT DEFAULT '',
                outline TEXT DEFAULT '',
                word_count INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(novel_id, number)
            );
            CREATE TABLE story_nodes (
                id TEXT PRIMARY KEY,
                novel_id TEXT NOT NULL,
                number INTEGER NOT NULL,
                node_type TEXT NOT NULL,
                outline TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE ai_invocation_sessions (
                id TEXT PRIMARY KEY,
                operation TEXT,
                status TEXT,
                context_json TEXT DEFAULT '{}',
                metadata_json TEXT DEFAULT '{}',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE ai_adoption_decisions (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                attempt_id TEXT DEFAULT '',
                decision TEXT DEFAULT 'accepted',
                accepted_by TEXT DEFAULT ''
            );
            CREATE TABLE variable_values (
                id TEXT PRIMARY KEY,
                variable_key TEXT NOT NULL,
                scope_level TEXT NOT NULL DEFAULT 'global',
                scope_key TEXT NOT NULL DEFAULT 'global',
                value_json TEXT NOT NULL,
                value_hash TEXT NOT NULL DEFAULT '',
                version_number INTEGER NOT NULL DEFAULT 1,
                is_current INTEGER NOT NULL DEFAULT 1,
                source_session_id TEXT NOT NULL DEFAULT '',
                source_attempt_id TEXT NOT NULL DEFAULT '',
                source_trace_id TEXT NOT NULL DEFAULT '',
                source_node_key TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def fetch_one(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetch_all(self, sql, params=()):
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def commit(self):
        self.conn.commit()


def test_recovery_policy_retries_writing_and_discards_transient_generation():
    db = _Db()
    db.execute("INSERT INTO novels (id, current_stage) VALUES ('novel-1', 'writing')")
    db.execute("INSERT INTO chapters (id, novel_id, number, status, content) VALUES ('c1', 'novel-1', 1, 'draft', '半章')")

    decision = AutopilotRecoveryPolicy(db).decide_on_start("novel-1")

    assert decision.next_stage == "writing"
    assert decision.chapter_number == 1
    assert decision.discard_transient_generation is True
    assert decision.reason == "retry_writing_step"


def test_recovery_policy_preserves_completed_chapter_for_auditing():
    db = _Db()
    db.execute("INSERT INTO novels (id, current_stage) VALUES ('novel-1', 'auditing')")
    db.execute("INSERT INTO chapters (id, novel_id, number, status, content) VALUES ('c1', 'novel-1', 2, 'completed', '完整正文')")

    decision = AutopilotRecoveryPolicy(db).decide_on_start("novel-1")

    assert decision.next_stage == "auditing"
    assert decision.chapter_number == 2
    assert decision.discard_transient_generation is False
    assert decision.reason == "completed_chapter_reaudit"


def test_recovery_policy_preserves_paused_review_gate():
    db = _Db()
    db.execute("INSERT INTO novels (id, current_stage) VALUES ('novel-1', 'paused_for_review')")

    decision = AutopilotRecoveryPolicy(db).decide_on_start("novel-1")

    assert decision.next_stage == "paused_for_review"
    assert decision.preserve_review_gate is True
    assert decision.clear_pending_invocation is False
    assert decision.discard_transient_invocations is False


def test_recovery_policy_discards_stopped_paused_prose_review_gate():
    db = _Db()
    db.execute(
        "INSERT INTO novels (id, current_stage, autopilot_status) VALUES ('novel-1', 'paused_for_review', 'stopped')"
    )
    db.execute(
        "INSERT INTO chapters (id, novel_id, number, status, content) VALUES ('c1', 'novel-1', 1, 'draft', '')"
    )
    db.execute(
        """
        INSERT INTO ai_invocation_sessions (id, operation, status, context_json, metadata_json)
        VALUES (
          's-prose',
          'autopilot.chapter.prose',
          'completed',
          '{"novel_id":"novel-1","chapter_number":1}',
          '{"stage":"writing","commit_owner":"story_pipeline_save_step"}'
        )
        """
    )
    db.execute(
        """
        INSERT INTO variable_values
          (id, variable_key, scope_level, scope_key, value_json, is_current, source_session_id)
        VALUES
          ('v1', 'chapter.prose.draft', 'novel', 'novel_id:novel-1|chapter_number:1', '"旧正文"', 1, 's-prose')
        """
    )

    policy = AutopilotRecoveryPolicy(db)
    decision = policy.decide_on_start("novel-1")

    assert decision.next_stage == "writing"
    assert decision.preserve_review_gate is False
    assert decision.discard_transient_generation is True
    assert decision.discard_transient_invocations is True
    assert decision.reason == "discard_interrupted_review_gate"

    policy.apply_transient_cleanup(decision)

    assert db.fetch_one("SELECT status FROM ai_invocation_sessions WHERE id = 's-prose'")["status"] == "cancelled"
    assert db.fetch_one("SELECT is_current FROM variable_values WHERE id = 'v1'")["is_current"] == 0


def test_recovery_policy_discards_stopped_paused_macro_review_gate_before_structure_applied():
    db = _Db()
    db.execute(
        "INSERT INTO novels (id, current_stage, autopilot_status) VALUES ('novel-1', 'paused_for_review', 'stopped')"
    )
    db.execute(
        """
        INSERT INTO ai_invocation_sessions (id, operation, status, context_json, metadata_json)
        VALUES (
          's-macro',
          'autopilot.macro.plan',
          'completed',
          '{"novel_id":"novel-1"}',
          '{"stage":"planning"}'
        )
        """
    )
    db.execute(
        """
        INSERT INTO variable_values
          (id, variable_key, scope_level, scope_key, value_json, is_current, source_session_id)
        VALUES
          ('v1', 'novel.planning.macro.parts', 'novel', 'novel_id:novel-1', '[]', 1, 's-macro')
        """
    )

    policy = AutopilotRecoveryPolicy(db)
    decision = policy.decide_on_start("novel-1")

    assert decision.next_stage == "macro_planning"
    assert decision.discard_transient_invocations is True

    policy.apply_transient_cleanup(decision)

    assert db.fetch_one("SELECT status FROM ai_invocation_sessions WHERE id = 's-macro'")["status"] == "cancelled"
    assert db.fetch_one("SELECT is_current FROM variable_values WHERE id = 'v1'")["is_current"] == 0


def test_recovery_policy_preserves_legacy_writing_resume_semantics():
    db = _Db()
    db.execute("INSERT INTO novels (id, current_stage) VALUES ('novel-1', 'writing')")
    db.execute("INSERT INTO chapters (id, novel_id, number, status, content) VALUES ('c1', 'novel-1', 1, 'draft', '半章')")

    decision = AutopilotRecoveryPolicy(db, story_pipeline_writing_enabled=False).decide_on_start("novel-1")

    assert decision.next_stage == "writing"
    assert decision.chapter_number == 1
    assert decision.discard_transient_generation is False
    assert decision.reason == "resume_legacy_writing"


def test_recovery_cleanup_discards_transient_chapter_preplan_for_writing_retry():
    db = _Db()
    preplan_metadata = {
        "act_chapter_plan": {
            "number": 1,
            "title": "第1章",
            "main_event": "轻量主事件",
            "handoff_from_previous": "本幕入口",
            "handoff_to_next": "下一章钩子",
            "required_threads": ["主线"],
        },
        "chapter_preplan": {
            "detail_outline": "一、开篇切入点：临时七段细纲",
            "continuity_context": "坏的连续性上下文",
        },
    }
    db.execute(
        "INSERT INTO novels (id, current_stage, autopilot_status) VALUES ('novel-1', 'writing', 'stopped')"
    )
    db.execute(
        """
        INSERT INTO story_nodes (id, novel_id, number, node_type, outline, metadata)
        VALUES ('sn-1', 'novel-1', 1, 'chapter', '一、开篇切入点：临时七段细纲', ?)
        """,
        (json.dumps(preplan_metadata, ensure_ascii=False),),
    )
    db.execute(
        """
        INSERT INTO chapters (id, novel_id, number, status, content)
        VALUES ('c1', 'novel-1', 1, 'draft', '半章草稿')
        """
    )
    db.execute(
        """
        INSERT INTO variable_values
          (id, variable_key, scope_level, scope_key, value_json, is_current, source_node_key)
        VALUES
          ('v-preplan-outline', 'chapter.outline', 'chapter', 'novel_id:novel-1|chapter_number:1', '"七段细纲"', 1, 'planning-chapter-preplan'),
          ('v-preplan-context', 'chapter.continuity_context', 'chapter', 'novel_id:novel-1|chapter_number:1', '"坏上下文"', 1, 'planning-chapter-preplan'),
          ('v-act-outline', 'chapter.outline', 'chapter', 'novel_id:novel-1|chapter_number:1', '"轻量链条"', 1, 'planning-act')
        """
    )

    policy = AutopilotRecoveryPolicy(db)
    decision = policy.decide_on_start("novel-1")
    assert decision.discard_transient_generation is True

    policy.apply_transient_cleanup(decision)

    story_node = db.fetch_one("SELECT outline, metadata FROM story_nodes WHERE id = 'sn-1'")
    chapter = db.fetch_one("SELECT outline, content, word_count, status FROM chapters WHERE id = 'c1'")
    preplan_flags = {
        row["id"]: row["is_current"]
        for row in db.fetch_all(
            "SELECT id, is_current FROM variable_values WHERE id LIKE 'v-preplan-%'",
            (),
        )
    }
    act_flag = db.fetch_one("SELECT is_current FROM variable_values WHERE id = 'v-act-outline'")["is_current"]

    assert "主事件：轻量主事件" in story_node["outline"]
    assert "chapter_preplan" not in json.loads(story_node["metadata"])
    assert chapter["content"] == ""
    assert chapter["word_count"] == 0
    assert chapter["status"] == "draft"
    assert "主事件：轻量主事件" in chapter["outline"]
    assert preplan_flags == {"v-preplan-outline": 0, "v-preplan-context": 0}
    assert act_flag == 1


def test_recovery_cleanup_cancels_only_retryable_pending_invocations():
    db = _Db()
    db.execute("INSERT INTO novels (id, current_stage) VALUES ('novel-1', 'writing')")
    db.execute(
        """
        INSERT INTO ai_invocation_sessions (id, operation, status, context_json, metadata_json)
        VALUES
          ('s-retry', 'autopilot.chapter.prose', 'generating', '{"novel_id":"novel-1"}', '{"stage":"writing"}'),
          ('s-review', 'autopilot.chapter.review', 'awaiting_acceptance', '{"novel_id":"novel-1"}', '{"stage":"paused_for_review"}'),
          ('s-human', 'autopilot.chapter.prose', 'awaiting_acceptance', '{"novel_id":"novel-1"}', '{"stage":"writing"}')
        """
    )
    db.execute(
        "INSERT INTO ai_adoption_decisions (id, session_id, accepted_by) VALUES ('d1', 's-human', 'user')"
    )

    policy = AutopilotRecoveryPolicy(db)
    decision = policy.decide_on_start("novel-1")
    assert decision.clear_pending_invocation is True

    policy.apply_transient_cleanup(decision)

    statuses = {
        row["id"]: row["status"]
        for row in db.fetch_all("SELECT id, status FROM ai_invocation_sessions", ())
    }
    assert statuses["s-retry"] == "cancelled"
    assert statuses["s-review"] == "awaiting_acceptance"
    assert statuses["s-human"] == "awaiting_acceptance"


# ══════════════════════ D-07：空大纲覆盖 + 每 tick 都清 + 失败被吞 ══════════════════════
#
# D-07 的三段病因（都已用运行时探针实证，不是读代码推断）：
#
# 1. **空串覆盖**：`_discard_transient_chapter_preplan` 的 `lightweight_outline`
#    初值是 `""` ⇒ 章节节点上没有 `act_chapter_plan` 时，
#    `chapters.outline` 被写成**空字符串**（真实七段剧本凭空消失）。
# 2. **每 tick 都清**：`decide_on_daemon_tick` 在 WRITING 阶段把
#    `discard_transient_generation` 置 True ⇒ 常规 tick 也在丢生成临时产物，
#    而丢弃只该发生在「真被打断」或「启停」时。
# 3. **失败被吞**：清理异常被 `except Exception` + `logger.debug` 吞掉
#    ⇒ 降级路径完全不可见。


def _novel(novel_id: str = "novel-1"):
    return SimpleNamespace(novel_id=SimpleNamespace(value=novel_id))


def _seed_writing_novel(db, *, with_act_plan: bool, chapter_outline: str, node_outline: str):
    db.execute(
        "INSERT INTO novels (id, current_stage, autopilot_status)"
        " VALUES ('novel-1', 'writing', 'running')"
    )
    db.execute(
        "INSERT INTO chapters (id, novel_id, number, status, content, outline, word_count)"
        " VALUES ('c1', 'novel-1', 1, 'draft', '半章草稿', ?, 1234)",
        (chapter_outline,),
    )
    metadata = {}
    if with_act_plan:
        metadata["act_chapter_plan"] = {
            "number": 1,
            "title": "第1章",
            "main_event": "轻量主事件",
            "handoff_from_previous": "本幕入口",
            "handoff_to_next": "下一章钩子",
            "required_threads": ["主线"],
        }
    metadata["chapter_preplan"] = {"detail_outline": "一、开篇切入点：临时七段细纲"}
    db.execute(
        "INSERT INTO story_nodes (id, novel_id, number, node_type, outline, metadata)"
        " VALUES ('sn-1', 'novel-1', 1, 'chapter', ?, ?)",
        (node_outline, json.dumps(metadata, ensure_ascii=False)),
    )


def test_daemon_tick_does_not_discard_transient_generation():
    """常规 tick **不许**丢弃生成临时产物。

    丢弃只该发生在「真被打断」（`decide_after_interrupted_writing`）
    或「启停」（`decide_on_start`）时。
    """
    db = _Db()
    db.execute("INSERT INTO novels (id, current_stage, autopilot_status) VALUES ('novel-1', 'writing', 'running')")
    db.execute(
        "INSERT INTO chapters (id, novel_id, number, status, content, outline)"
        " VALUES ('c1', 'novel-1', 1, 'draft', '半章', '真实七段细纲')"
    )

    decision = AutopilotRecoveryPolicy(db, story_pipeline_writing_enabled=True).decide_on_daemon_tick(
        _novel()
    )

    assert decision.discard_transient_generation is False


def test_interrupted_writing_still_discards_transient_generation():
    """反向守卫：真被中断时**必须**丢 —— 否则半截七段规划被当稳定输入复用。"""
    db = _Db()
    db.execute("INSERT INTO novels (id, current_stage, autopilot_status) VALUES ('novel-1', 'writing', 'running')")
    db.execute(
        "INSERT INTO chapters (id, novel_id, number, status, content, outline)"
        " VALUES ('c1', 'novel-1', 1, 'draft', '半章', '真实七段细纲')"
    )

    policy = AutopilotRecoveryPolicy(db, story_pipeline_writing_enabled=True)
    decision = policy.decide_after_interrupted_writing(_novel())

    assert decision.discard_transient_generation is True
    assert decision.chapter_number == 1


def test_start_path_still_discards_transient_generation():
    """反向守卫：启停路径照旧丢弃（既有语义不许被改坏）。"""
    db = _Db()
    db.execute("INSERT INTO novels (id, current_stage, autopilot_status) VALUES ('novel-1', 'writing', 'stopped')")
    db.execute(
        "INSERT INTO chapters (id, novel_id, number, status, content, outline)"
        " VALUES ('c1', 'novel-1', 1, 'draft', '半章', '真实七段细纲')"
    )

    decision = AutopilotRecoveryPolicy(db, story_pipeline_writing_enabled=True).decide_on_start("novel-1")

    assert decision.discard_transient_generation is True


def test_cleanup_never_overwrites_real_outline_with_empty_string():
    """D-07 核心：没有 act_chapter_plan 时，**不许**把真实大纲写成空串。"""
    db = _Db()
    _seed_writing_novel(
        db,
        with_act_plan=False,
        chapter_outline="真实七段细纲（幕级规划产出的正文骨架）",
        node_outline="章节节点上的真实大纲",
    )

    policy = AutopilotRecoveryPolicy(db, story_pipeline_writing_enabled=True)
    decision = policy.decide_after_interrupted_writing(_novel())
    policy.apply_transient_cleanup(decision)

    chapter = db.fetch_one("SELECT outline, content, word_count, status FROM chapters WHERE id = 'c1'")
    node = db.fetch_one("SELECT outline, metadata FROM story_nodes WHERE id = 'sn-1'")

    # 大纲必须原样保留（不是 ""、也不是被降级）
    assert chapter["outline"] == "真实七段细纲（幕级规划产出的正文骨架）"
    assert node["outline"] == "章节节点上的真实大纲"
    # 但「半截产物」仍然要清掉：正文 / 字数 / 草稿状态
    assert chapter["content"] == ""
    assert chapter["word_count"] == 0
    assert chapter["status"] == "draft"
    # preplan 元数据照样摘掉
    assert "chapter_preplan" not in json.loads(node["metadata"])


def test_cleanup_still_downgrades_to_lightweight_chain_when_available():
    """反向守卫：有 act_chapter_plan 时**仍然**换成轻量链条（既有语义不许被改坏）。"""
    db = _Db()
    _seed_writing_novel(
        db,
        with_act_plan=True,
        chapter_outline="真实七段细纲（幕级规划产出的正文骨架）",
        node_outline="章节节点上的真实大纲",
    )

    policy = AutopilotRecoveryPolicy(db, story_pipeline_writing_enabled=True)
    decision = policy.decide_after_interrupted_writing(_novel())
    policy.apply_transient_cleanup(decision)

    chapter = db.fetch_one("SELECT outline FROM chapters WHERE id = 'c1'")
    node = db.fetch_one("SELECT outline FROM story_nodes WHERE id = 'sn-1'")

    assert "主事件：轻量主事件" in chapter["outline"]
    assert "主事件：轻量主事件" in node["outline"]


class _BrokenDb(_Db):
    """`UPDATE chapters` 一执行就失败（模拟 database is locked）。"""

    def execute(self, sql, params=()):
        if "UPDATE chapters" in sql:
            raise RuntimeError("db is locked")
        return super().execute(sql, params)


def test_cleanup_failure_is_logged_as_warning_not_debug(caplog):
    """D-07 ③：清理失败必须**看得见** —— 降级路径不许 `logger.debug` 吞掉。"""
    db = _BrokenDb()
    _seed_writing_novel(
        db,
        with_act_plan=False,
        chapter_outline="真实七段细纲",
        node_outline="章节节点上的真实大纲",
    )

    policy = AutopilotRecoveryPolicy(db, story_pipeline_writing_enabled=True)
    decision = policy.decide_after_interrupted_writing(_novel())

    with caplog.at_level(logging.WARNING, logger="application.engine.services.autopilot_recovery_policy"):
        policy.apply_transient_cleanup(decision)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "清理失败必须记成 warning（不能只 debug）"
    assert any("章前规划清理失败" in r.getMessage() for r in warnings)


def test_cleanup_failure_does_not_raise():
    """反向守卫：清理失败仍然不许把异常抛到调用方（守护进程不能因此崩）。"""
    db = _BrokenDb()
    _seed_writing_novel(
        db,
        with_act_plan=False,
        chapter_outline="真实七段细纲",
        node_outline="章节节点上的真实大纲",
    )

    policy = AutopilotRecoveryPolicy(db, story_pipeline_writing_enabled=True)
    decision = policy.decide_after_interrupted_writing(_novel())

    policy.apply_transient_cleanup(decision)  # 不抛异常即通过

