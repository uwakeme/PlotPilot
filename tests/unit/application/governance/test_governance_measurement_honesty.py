"""I-03 / I-04：治理闸门诚实化 + budget_patch 被消费。

复刻两种线上形态（生产库实测，见 task-13 简报）：

- ``promise_hit_rate`` 在 598 条报告里有 524 条（87.6%）是 0.0，根因是
  ``theme_anchors_json`` 全库为空数组，``_promise_hit_rate`` 退化成对超长句子的
  逐字子串匹配。原实现在「无可用锚点」时返回 **1.0（满分）**、在「正文为空」时
  返回 **0.0（假装测过）**——两处都是把「没测出来」伪装成测量结论。
- 旧的 ``should_pause`` 判定带 ``chapter_number <= 12`` 章号闸；一旦算出暂停建议就
  自动 ``_pause_autopilot``，写 ``autopilot_status='stopped'``（与「已达目标章数」
  同一个状态，不可恢复）。按原计划把漂移升成 high，87.6% 的章节会被永久停掉。

本文件全部是行为断言：不读源码、不断言实现文本。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from application.governance.models import GovernanceReport, NarrativeContract
from application.governance.service import NarrativeGovernanceService
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.migration_runner import apply_migration_files
from infrastructure.persistence.database.sqlite_governance_repository import (
    SqliteGovernanceRepository,
)


@dataclass
class FakeNovel:
    title: str
    premise: str
    target_chapters: int = 80


class FakeNovelRepo:
    def get_by_id(self, _novel_id):
        return FakeNovel(
            title="我不是剑仙",
            premise="【类型：东方玄幻】核心冲突：无根仙体被迫走上反剑仙之路\n开篇钩子：旧案冤情牵出宗门旧债",
        )


class FakeGovernanceRepo:
    """行为替身。方法集严格对齐 SqliteGovernanceRepository 已核实的接口。"""

    def __init__(self):
        self.contract = None
        self.storylines = {}
        self.reports = []
        self.events = []
        self.debts = []

    def get_contract(self, novel_id):
        return self.contract if self.contract and self.contract.novel_id == novel_id else None

    def save_contract(self, contract):
        self.contract = contract

    def list_storylines(self, novel_id):
        return [line for line in self.storylines.values() if line.novel_id == novel_id]

    def upsert_storyline(self, storyline):
        self.storylines[storyline.canonical_id] = storyline

    def get_storyline(self, canonical_id):
        return self.storylines.get(canonical_id)

    def delete_storyline(self, canonical_id):
        self.storylines.pop(canonical_id, None)

    def save_report(self, report):
        self.reports.append(report)

    def latest_report(self, novel_id):
        matches = [r for r in self.reports if r.novel_id == novel_id]
        return matches[-1] if matches else None

    def get_report(self, novel_id, report_id):
        for report in self.reports:
            if report.novel_id == novel_id and report.report_id == report_id:
                return report
        return None

    def update_report_status(self, novel_id, report_id, status):
        for report in self.reports:
            if report.novel_id == novel_id and report.report_id == report_id:
                report.review_status = status

    def append_event(self, event_id, novel_id, event_type, chapter_number, payload):
        self.events.append((event_id, novel_id, event_type, chapter_number, payload))

    def list_open_debts(self, novel_id, limit=50):
        return self.debts[:limit]


class RecordingConnection:
    """只记录 SQL，不落库——用来断言治理层到底往 novels 表写了什么。"""

    def __init__(self, statements: list[str]):
        self.statements = statements

    def execute(self, sql, params=()):
        self.statements.append(" ".join(str(sql).split()))
        return self

    def commit(self):
        return None


class RecordingDb:
    """数据库替身。fetch_one 一律返回 None（无书 / 无章节），stage 落到确定性默认值。"""

    def __init__(self):
        self.statements: list[str] = []

    def get_connection(self):
        return RecordingConnection(self.statements)

    def fetch_one(self, _sql, _params=()):
        return None

    def novel_updates(self) -> list[str]:
        return [sql for sql in self.statements if "UPDATE novels" in sql]


def make_service(contract: NarrativeContract | None = None, with_db: bool = False):
    repo = FakeGovernanceRepo()
    if contract is not None:
        repo.save_contract(contract)
    db = RecordingDb() if with_db else None
    return NarrativeGovernanceService(repo, FakeNovelRepo(), db=db), repo, db


# 只有承诺词、一个承诺词都不沾的正文：命中 0/全部
DRIFTING_CONTENT = "配角闲谈许久，最后身份揭晓，真相大白。"


# --------------------------------------------------------------------------
# Part A：测不出来就是测不出来
# --------------------------------------------------------------------------


def test_hit_rate_is_none_when_contract_has_no_usable_anchor():
    """无可用锚点 → None。原先返回 1.0，等于给每章发一张满分卷。"""
    service, _repo, _db = make_service(
        contract=NarrativeContract(novel_id="n1", title_promise="", core_question="", theme_anchors=[])
    )

    report = service.evaluate_after_chapter("n1", 5, "他走在雪地里，东方玄幻的门在远处开着。")

    assert report.promise_hit_rate is None
    assert report.promise_hit_rate is not 1.0


def test_hit_rate_is_none_when_content_is_empty():
    """正文为空 → None。原先返回 0.0，伪装成「测了，是零命中」。"""
    service, _repo, _db = make_service()

    report = service.evaluate_after_chapter("n1", 5, "")

    assert report.promise_hit_rate is None
    assert report.promise_hit_rate is not 0.0


def test_unmeasured_chapter_reports_no_drift_but_says_unavailable():
    """没测出来就没有资格说漂移，但必须让「没测」在报告里可见。"""
    service, _repo, _db = make_service()

    report = service.evaluate_after_chapter("n1", 5, "")

    codes = {issue.code for issue in report.issues}
    assert "promise_drift" not in codes
    assert "promise_measurement_unavailable" in codes
    unavailable = next(i for i in report.issues if i.code == "promise_measurement_unavailable")
    assert unavailable.severity == "info"


def test_measured_zero_hit_rate_is_still_reported_as_drift():
    """反向护栏：真·零命中必须照旧产出漂移，不能被「诚实化」顺手抹掉。"""
    service, _repo, _db = make_service(
        contract=NarrativeContract(
            novel_id="n1",
            title_promise="无根剑骨",
            core_question="谁杀了师尊",
            theme_anchors=["玄冰剑", "旧案"],
        )
    )

    report = service.evaluate_after_chapter("n1", 5, "雪停了，什么也没发生。")

    assert report.promise_hit_rate == 0.0
    assert "promise_drift" in {issue.code for issue in report.issues}


# --------------------------------------------------------------------------
# Part B：暂停权需要可信证据
# --------------------------------------------------------------------------


def test_should_pause_is_none_when_measurement_unavailable():
    """未测量 → None（本次未能评估），不是 False。"""
    service, _repo, _db = make_service()

    report = service.evaluate_after_chapter("n1", 5, "")

    assert report.should_pause_autopilot is None


def test_should_pause_is_false_when_measured_and_clean():
    """评估过且没事 → False。与 None 必须可区分。"""
    service, _repo, _db = make_service()

    report = service.evaluate_after_chapter("n1", 30, "他重回东方玄幻的起点，剑仙之路重启。")

    assert report.promise_hit_rate is not None
    assert report.should_pause_autopilot is False


def test_should_pause_is_true_for_drift_plus_premature_payoff():
    """评估过且该停 → True。三态齐全。"""
    service, _repo, _db = make_service()

    report = service.evaluate_after_chapter("n1", 5, DRIFTING_CONTENT)

    assert report.should_pause_autopilot is True


def test_late_chapter_drift_does_not_pause():
    """第 12 章后的漂移不该开始停书。

    这是原计划事故的护栏：若把漂移 severity 无条件抬成 high 并保留章号闸，
    生产库 87.6% 的零命中章节会被逐个暂停。
    """
    service, _repo, _db = make_service()

    report = service.evaluate_after_chapter("n1", 30, DRIFTING_CONTENT)

    assert report.promise_hit_rate == 0.0
    assert "promise_drift" in {issue.code for issue in report.issues}
    assert report.should_pause_autopilot is False


def test_evaluation_never_pauses_autopilot_and_emits_recommendation_only():
    """治理层只出建议：不写 novels 表，且不再自称「已暂停」。"""
    service, repo, db = make_service(with_db=True)

    report = service.evaluate_after_chapter("n1", 5, DRIFTING_CONTENT)

    assert report.should_pause_autopilot is True
    assert db.novel_updates() == []
    event_types = {evt[2] for evt in repo.events}
    assert "GovernancePauseRecommendedEvent" in event_types
    assert "AutopilotPausedForGovernanceEvent" not in event_types


def test_review_accept_pauses_when_report_recommended_pause():
    """人工 accept 一条暂停建议 → 真的暂停。暂停权交回给人。"""
    service, repo, db = make_service(with_db=True)
    report = service.evaluate_after_chapter("n1", 5, DRIFTING_CONTENT)
    assert db.novel_updates() == []

    result = service.review_action("n1", {"report_id": report.report_id, "action": "accept"})

    assert result["autopilot_paused"] is True
    assert result["status"] == "accepted"
    assert len(db.novel_updates()) == 1
    assert "paused_for_review" in db.novel_updates()[0]
    assert repo.get_report("n1", report.report_id).review_status == "accepted"
    assert "GovernancePauseAcceptedEvent" in {evt[2] for evt in repo.events}


def test_review_accept_does_not_pause_when_report_has_no_recommendation():
    """accept 不是无脑暂停：没有暂停建议的报告，accept 也不能停书。"""
    service, _repo, db = make_service(with_db=True)
    report = service.evaluate_after_chapter("n1", 30, "他重回东方玄幻的起点，剑仙之路重启。")
    assert report.should_pause_autopilot is False

    result = service.review_action("n1", {"report_id": report.report_id, "action": "accept"})

    assert result["autopilot_paused"] is False
    assert db.novel_updates() == []


def test_review_ignore_does_not_pause_even_when_pause_recommended():
    """ignore 是显式不采纳：即便报告建议暂停也不停。"""
    service, _repo, db = make_service(with_db=True)
    report = service.evaluate_after_chapter("n1", 5, DRIFTING_CONTENT)

    result = service.review_action("n1", {"report_id": report.report_id, "action": "ignore"})

    assert result["status"] == "ignored"
    assert result["autopilot_paused"] is False
    assert db.novel_updates() == []


def test_review_accept_on_unmeasured_report_does_not_pause():
    """should_pause is None 的报告没有可执行的建议 → 不停书。"""
    service, _repo, db = make_service(with_db=True)
    report = service.evaluate_after_chapter("n1", 5, "")
    assert report.should_pause_autopilot is None

    result = service.review_action("n1", {"report_id": report.report_id, "action": "accept"})

    assert result["autopilot_paused"] is False
    assert db.novel_updates() == []


# --------------------------------------------------------------------------
# Part C：budget_patch 被真正消费（不是从 severity 猜）
# --------------------------------------------------------------------------

# development 阶段：max_new_storylines=1 / allowed_reveal_level='partial'
DEVELOPMENT_CHAPTER = 31


def _budget_with_db(patch: dict, chapter_number: int = 3):
    service, _repo, db = make_service(with_db=True)
    service.repository.save_report(
        GovernanceReport(
            report_id="gov_patch",
            novel_id="n1",
            chapter_number=chapter_number,
            severity="medium",
            promise_hit_rate=0.0,
            budget_patch=patch,
            should_pause_autopilot=False,
        )
    )
    return service, db


def test_preview_budget_without_patch_keeps_stage_defaults():
    """无 patch 时就是阶段默认值，消费逻辑不得凭空收紧。"""
    service, _repo, _db = make_service()

    budget = service.preview_chapter_budget("n1", chapter_number=DEVELOPMENT_CHAPTER)

    assert budget.max_new_storylines == 1
    assert budget.allowed_reveal_level == "partial"


def test_preview_budget_consumes_storyline_delta():
    """must_reduce_new_storylines + max_new_storylines_delta → 真的减 max_new。"""
    service, _db = _budget_with_db(
        {"must_reduce_new_storylines": True, "must_retouch_promise": False, "max_new_storylines_delta": -1}
    )

    budget = service.preview_chapter_budget("n1", chapter_number=DEVELOPMENT_CHAPTER)

    assert budget.max_new_storylines == 0
    assert "压缩新增故事线" in " ".join(budget.notes)


def test_preview_budget_consumes_promise_retouch_and_reveal_cap():
    """must_retouch_promise → 明确要求修复承诺；allowed_reveal_level='hint' → 收紧档位。"""
    service, _db = _budget_with_db(
        {"must_reduce_new_storylines": False, "must_retouch_promise": True, "allowed_reveal_level": "hint"}
    )

    budget = service.preview_chapter_budget("n1", chapter_number=DEVELOPMENT_CHAPTER)

    assert "承诺漂移" in " ".join(budget.notes)
    assert budget.allowed_reveal_level == "hint"
    # 只收紧不放宽：档位没有要求压缩故事线，max_new 不动
    assert budget.max_new_storylines == 1


def test_preview_budget_never_widens_reveal_level():
    """patch 要求放开到 payoff 时不得放宽——治理 patch 只能收紧。"""
    service, _db = _budget_with_db(
        {"must_reduce_new_storylines": False, "must_retouch_promise": False, "allowed_reveal_level": "payoff"}
    )

    budget = service.preview_chapter_budget("n1", chapter_number=DEVELOPMENT_CHAPTER)

    assert budget.allowed_reveal_level == "partial"


def test_preview_budget_ignores_patch_without_quantified_delta():
    """must_reduce_new_storylines=True 但没有 delta 时不编造减多少。"""
    service, _db = _budget_with_db({"must_reduce_new_storylines": True, "must_retouch_promise": False})

    budget = service.preview_chapter_budget("n1", chapter_number=DEVELOPMENT_CHAPTER)

    assert budget.max_new_storylines == 1


# --------------------------------------------------------------------------
# Part A4：None 必须能穿过 repository 往返（真库，不裸开 sqlite3）
# --------------------------------------------------------------------------


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "1")
    # DatabaseConnection 初始化时会跑全部迁移，本文件的新迁移也在其中，
    # 所以这是「迁移 + repository」的真实端到端路径。
    return DatabaseConnection(str(tmp_path / "governance.db"))


def test_report_none_values_round_trip_through_repository(real_db):
    repo = SqliteGovernanceRepository(real_db)

    repo.save_report(
        GovernanceReport(
            report_id="unmeasured",
            novel_id="n1",
            chapter_number=3,
            severity="info",
            promise_hit_rate=None,
            should_pause_autopilot=None,
        )
    )
    repo.save_report(
        GovernanceReport(
            report_id="evaluated_clean",
            novel_id="n1",
            chapter_number=4,
            severity="info",
            promise_hit_rate=0.5,
            should_pause_autopilot=False,
        )
    )

    raw = real_db.fetch_one(
        "SELECT promise_hit_rate, should_pause_autopilot FROM governance_reports WHERE report_id = ?",
        ("unmeasured",),
    )
    assert raw["promise_hit_rate"] is None
    assert raw["should_pause_autopilot"] is None

    unmeasured = repo.get_report("n1", "unmeasured")
    assert unmeasured.promise_hit_rate is None
    assert unmeasured.should_pause_autopilot is None

    evaluated = repo.get_report("n1", "evaluated_clean")
    assert evaluated.promise_hit_rate == 0.5
    assert evaluated.should_pause_autopilot is False


def test_latest_report_keeps_none_distinguishable_from_false(real_db):
    """latest_report 读出侧同样不能把 None 塌成 False/0。"""
    repo = SqliteGovernanceRepository(real_db)
    service = NarrativeGovernanceService(repo, FakeNovelRepo())

    service.evaluate_after_chapter("n1", 3, "")
    latest = repo.latest_report("n1")

    assert latest.promise_hit_rate is None
    assert latest.should_pause_autopilot is None
    assert "promise_measurement_unavailable" in {issue.code for issue in latest.issues}


def test_governance_reports_columns_are_nullable_after_migration(real_db):
    """迁移确实把两列的 NOT NULL 去掉了（否则写 None 会直接 IntegrityError）。"""
    info = {row["name"]: row for row in real_db.fetch_all("PRAGMA table_info(governance_reports)")}

    assert info["promise_hit_rate"]["notnull"] == 0
    assert info["should_pause_autopilot"]["notnull"] == 0


MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[4]
    / "infrastructure"
    / "persistence"
    / "database"
    / "migrations"
)
MIGRATION_NAME = "add_nullable_governance_measurements.sql"

# 迁移前的表形态，逐字对照 add_narrative_governance.sql：两列都是 NOT NULL。
LEGACY_GOVERNANCE_REPORTS_DDL = """
CREATE TABLE governance_reports (
    report_id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    severity TEXT NOT NULL,
    promise_hit_rate REAL NOT NULL DEFAULT 0,
    issues_json TEXT NOT NULL DEFAULT '[]',
    budget_patch_json TEXT NOT NULL DEFAULT '{}',
    should_pause_autopilot INTEGER NOT NULL DEFAULT 0,
    review_status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def test_migration_run_does_not_disable_foreign_key_enforcement(tmp_path, monkeypatch):
    """迁移不得关掉外键强制——本仓库把它当工程不变量（sqlite_pragmas.py:87 断言为 1）。

    这条曾经真的踩中：迁移里写了 ``PRAGMA foreign_keys = OFF/ON`` 一对（重建表的
    通用做法）。但 migration_runner 是逐语句执行、中途不 commit 的，INSERT 之后
    事务已开着，而 SQLite 规定 PRAGMA foreign_keys 在事务内是空操作——于是 OFF
    生效了、结尾的 ON 永远落不了地。

    本迁移压根不需要它：全仓没有任何外键引用 governance_reports。
    """
    monkeypatch.setenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "1")
    db = DatabaseConnection(str(tmp_path / "fk.db"))
    conn = db.get_connection()

    # 把本迁移退回「未应用」，并把表还原成迁移前的 NOT NULL 形态 + 一条存量行。
    conn.execute("DELETE FROM migrations_applied WHERE migration_file = ?", (MIGRATION_NAME,))
    conn.execute("DROP TABLE IF EXISTS governance_reports")
    conn.execute(LEGACY_GOVERNANCE_REPORTS_DDL)
    conn.execute(
        "INSERT INTO governance_reports VALUES ('legacy1', 'n1', 3, 'medium', 0.0, '[]', '{}', 0,"
        " 'open', '2026-10-01')"
    )
    conn.commit()

    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    apply_migration_files(conn, MIGRATIONS_DIR)

    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    # 反空洞护栏：确认上面断言不是「因为迁移压根没跑」而通过的。
    info = {row["name"]: row for row in db.fetch_all("PRAGMA table_info(governance_reports)")}
    assert info["promise_hit_rate"]["notnull"] == 0
    assert db.fetch_one("SELECT report_id FROM governance_reports WHERE report_id = ?", ("legacy1",))
