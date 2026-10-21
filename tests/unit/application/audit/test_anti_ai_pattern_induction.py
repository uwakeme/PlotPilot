"""F-06：AI 归纳本书特有的 AI 味表达 → 校验 → 落库 → 回灌进扫描器。

红线（本文件负责钉死）：

1. **不越界** —— 归纳出的关键词必须是**纯字面**（含正则元字符的一律丢弃），
   且**必须在本章正文里确实出现**（AI 编的丢弃）。
2. **三态可分辨** —— AI 不可用 / 输出不合契约 ⇒ **不产生任何模式**（不编），
   也不回落成「代码统计出来的模式」。
3. **闭环** —— 归纳出的规则落库后，**扫描器真的能报出它**（不是只存不用）。
4. **规则库版本化** —— 新增 +1；驳回**不回退**（登记表 F-06 的不变量）。
5. **旧的固定模板/触发词不得复活** —— 那是「代码假装学习」的残留。
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.audit.services.anti_ai_learning import AntiAILearningService
from application.audit.services.anti_ai_pattern_induction import AntiAiPatternInducer
from application.audit.services.anti_ai_pattern_induction_schema import (
    SOURCE_AI,
    SOURCE_NONE,
    is_literal_keyword,
    validate_induced_patterns,
)
from application.audit.services.cliche_scanner import ClicheHit, ClicheScanner
from infrastructure.persistence.database.sqlite_anti_ai_pattern_repository import (
    AntiAiPatternRepository,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
MIGRATIONS_DIR = REPO_ROOT / "infrastructure/persistence/database/migrations"
#: F-06 的迁移（按文件名排序 ⇒ 建表在前、唯一索引在后）
MIGRATIONS = sorted(MIGRATIONS_DIR.glob("add_anti_ai*.sql"))
LEARNING_SRC = (
    REPO_ROOT / "application/audit/services/anti_ai_learning.py"
).read_bytes().decode("utf-8")


class _TempDB:
    """最小 DatabaseConnection 替身（只提供 execute / fetch_one / fetch_all）。

    **不碰生产库** —— 单元测试用临时 sqlite 文件。
    迁移按**真实顺序**全部执行（建表 → 唯一索引），
    否则索引那条不会被测到。
    """

    def __init__(self, path):
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        for mig in MIGRATIONS:
            self._conn.executescript(mig.read_bytes().decode("utf-8"))

    def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def fetch_one(self, sql, params=()):
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetch_all(self, sql, params=()):
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]


@pytest.fixture()
def repo(tmp_path):
    return AntiAiPatternRepository(_TempDB(tmp_path / "patterns.db"))


# ── 0. 迁移本身有效 ────────────────────────────────────────────────


def test_the_migration_sql_is_valid(repo):
    """迁移能被真实 sqlite 执行 ⇒ 表与索引都建起来了。"""
    assert repo.current_version("n1") == 0


def test_the_rule_library_has_a_unique_key_per_novel_and_keyword(repo):
    """(novel_id, keyword) 必须唯一。

    坑：建表语句只有 ``id TEXT PRIMARY KEY``（id 每次新 uuid），
    而仓储代码的注释曾写「表的唯一键在 (novel_id, keyword) 上」——**那是假的**。
    没有唯一约束 ⇒ 同一关键词可能攒出多行 ⇒ 扫描器把一条规则算成多次命中。
    """
    repo.upsert_approved(novel_id="n1", keyword="缓缓的雨", name="a")
    with pytest.raises(sqlite3.IntegrityError):
        repo._get_db().execute(
            """
            INSERT INTO anti_ai_learned_patterns
                (id, novel_id, keyword, name, severity, category, reason, hint,
                 status, source, rule_version, first_seen_chapter, hit_count,
                 created_at, updated_at)
            VALUES ('dup', 'n1', '缓缓的雨', '', 'info', '', '', '',
                    'approved', 'ai', 99, 0, 0, '', '')
            """
        )


# ── 1. 不越界：纯字面 + 必须在正文里出现 ───────────────────────────


@pytest.mark.parametrize(
    "keyword",
    ["缓缓的雨", "眼中闪过一丝", "微微一笑", "嘴角勾起", "不易察觉的"],
)
def test_plain_keywords_are_accepted(keyword):
    assert is_literal_keyword(keyword) is True


@pytest.mark.parametrize(
    "keyword",
    ["缓缓.*雨", "眼中(闪过|掠过)", "a+b", "^开头", "结尾$", "有\\反斜杠", "有 空格", "", "短"],
)
def test_non_literal_or_oversized_keywords_are_rejected(keyword):
    assert is_literal_keyword(keyword) is False


def test_a_keyword_absent_from_the_text_is_dropped():
    """AI 编出来的模式：正文里根本没有 ⇒ 丢弃。"""
    patterns = validate_induced_patterns(
        {"patterns": [
            {"keyword": "缓缓的雨", "name": "本书句式", "severity": "warning"},
            {"keyword": "从未出现过的句子", "name": "编的", "severity": "warning"},
        ]},
        content="他站在窗前，缓缓的雨落在瓦上。",
    )

    assert [p.keyword for p in patterns] == ["缓缓的雨"]


def test_illegal_severity_is_dropped():
    patterns = validate_induced_patterns(
        {"patterns": [
            {"keyword": "缓缓的雨", "severity": "fatal"},
            {"keyword": "微微一笑", "severity": "info"},
        ]},
        content="缓缓的雨。微微一笑。",
    )

    assert [p.keyword for p in patterns] == ["微微一笑"]


def test_unknown_shape_is_unassessed():
    assert validate_induced_patterns(None, content="x") == []
    assert validate_induced_patterns({"patterns": "不是列表"}, content="x") == []


# ── 2. 三态：AI 不可用 ⇒ 不产生模式 ────────────────────────────────


@pytest.mark.asyncio
async def test_the_inducer_returns_unassessed_when_the_llm_fails():
    class _Boom:
        async def generate(self, prompt, config=None):
            raise RuntimeError("endpoint down")

    patterns, source = await AntiAiPatternInducer(_Boom()).induce(
        chapter_number=3, content="缓缓的雨。"
    )

    assert patterns == []
    assert source == SOURCE_NONE


@pytest.mark.asyncio
async def test_the_learning_service_produces_nothing_when_the_ai_is_unassessed(repo):
    """未评估 ⇒ **不产生任何模式**（不编，也不回落成代码统计的模式）。"""
    class _Boom:
        async def generate(self, prompt, config=None):
            raise RuntimeError("endpoint down")

    service = AntiAILearningService(repository=repo)
    stored = await service.induce_and_store_patterns(
        novel_id="n1", chapter_number=3, content="缓缓的雨。", llm_service=_Boom()
    )

    assert stored == []
    assert repo.list_approved("n1") == []


# ── 3. 闭环：归纳 → 落库 → 扫描器真的报出来 ─────────────────────────


@pytest.mark.asyncio
async def test_the_learned_pattern_is_detected_by_the_scanner(repo):
    class _LLM:
        async def generate(self, prompt, config=None):
            return SimpleNamespace(content=json.dumps({"patterns": [
                {"keyword": "缓缓的雨", "name": "本书句式", "severity": "warning",
                 "reason": "作者写惯了的转折模板"},
            ]}))

    service = AntiAILearningService(repository=repo)
    stored = await service.induce_and_store_patterns(
        novel_id="n1", chapter_number=3, content="他站在窗前，缓缓的雨落在瓦上。",
        llm_service=_LLM(),
    )

    assert len(stored) == 1
    approved = repo.list_approved("n1")
    assert approved and approved[0][0] == "缓缓的雨"

    # 回灌：扫描器用上了这条规则 ⇒ 真的报出它
    scanner = ClicheScanner(use_enhanced=True, extra_patterns=approved)
    hits = scanner.scan_cliches("他站在窗前，缓缓的雨落在瓦上。")
    assert any(h.pattern == "本书句式" for h in hits)


def test_the_auditor_loads_the_learned_patterns_for_the_novel(repo, monkeypatch):
    from application.audit.services import anti_ai_audit as audit_mod

    repo.upsert_approved(
        novel_id="n1", keyword="缓缓的雨", name="本书句式", severity="warning"
    )
    monkeypatch.setattr(
        audit_mod.AntiAIAuditor,
        "_load_learned_patterns",
        staticmethod(lambda novel_id: repo.list_approved(novel_id) if novel_id else []),
    )

    report = audit_mod.AntiAIAuditor().scan_chapter("ch-3", "缓缓的雨落在瓦上。", novel_id="n1")
    assert any(h.pattern == "本书句式" for h in report.hits)


# ── 4. 规则库版本化 ───────────────────────────────────────────────


def test_the_rule_version_increments_and_does_not_regress_on_reject(repo):
    v1 = repo.upsert_approved(novel_id="n1", keyword="缓缓的雨", name="a")
    v2 = repo.upsert_approved(novel_id="n1", keyword="微微一笑", name="b")
    assert v1 == 1 and v2 == 2
    assert repo.current_version("n1") == 2

    repo.reject("n1", "缓缓的雨")
    assert repo.current_version("n1") == 2, "驳回不能让版本回退"
    assert [p[0] for p in repo.list_approved("n1")] == ["微微一笑"]


def test_patterns_are_scoped_per_novel(repo):
    repo.upsert_approved(novel_id="n1", keyword="缓缓的雨", name="a")
    repo.upsert_approved(novel_id="n2", keyword="微微一笑", name="b")

    assert [p[0] for p in repo.list_approved("n1")] == ["缓缓的雨"]
    assert [p[0] for p in repo.list_approved("n2")] == ["微微一笑"]


# ── 5. 旧的固定模板/触发词不得复活 ─────────────────────────────────


def test_the_fixed_templates_and_trigger_words_are_gone():
    """旧实现：6 个结构模板正则 + 7 个触发词 —— 那不是学习。"""
    tree = ast.parse(LEARNING_SRC)
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for phrase in (
        "仿佛", "宛如", "缓缓", "微微", "暗自", "分明",
        "副词+动词结构", "不禁系列", "一X就Y结构",
    ):
        assert phrase not in literals, f"旧的固定模板/触发词还在：{phrase}"


def test_the_learning_service_has_no_template_based_learning():
    tree = ast.parse(LEARNING_SRC)
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "analyze_chapter_audit" not in names, "旧的模板式学习入口还在"
    assert "induce_and_store_patterns" in names


# ── 6. 回归守卫：实现层踩过的坑 ──────────────────────────────────────
#
# 这一节的每一条都对应一个**已经写错过**的地方。
# 它们全是「静默降级」型缺陷：不报错、不变红，只是能力悄悄没了。


@pytest.mark.asyncio
async def test_the_known_hits_reach_the_prompt():
    """已知命中必须真的进提示词。

    坑：``ClicheHit`` 的字段是 ``pattern`` / ``text``，
    而 ``pattern_name`` / ``matched_text`` 是**另一个扫描器**
    （``application/engine/rules/stream_ac_scanner.py``）的命中类型。
    写错字段名 ⇒ 命中永远传不进提示词，而渲染照样成功 ⇒ 静默降级。
    """
    captured = {}

    class _LLM:
        async def generate(self, prompt, config=None):
            captured["prompt"] = prompt
            return SimpleNamespace(content=json.dumps({"patterns": []}))

    hits = [
        ClicheHit(
            pattern="本书句式",
            text="缓缓的雨落在瓦上",
            start=0,
            end=7,
            severity="warning",
        )
    ]
    await AntiAiPatternInducer(_LLM()).induce(
        chapter_number=3, content="缓缓的雨落在瓦上。", known_hits=hits
    )

    assert "本书句式" in captured["prompt"].user, "已知命中的模式名没进提示词"
    assert "缓缓的雨落在瓦上" in captured["prompt"].user, "已知命中的原文没进提示词"


def test_a_learned_rule_is_matched_literally_not_as_a_regex(repo):
    """已学规则是**字面关键词** ⇒ 正则元字符按字面处理。

    坑：``upsert_approved`` 走人工路径时不校验元字符，
    而扫描器若直接 ``re.compile(keyword)``，
    人工加一条 ``(a+)+$`` 就能让扫描线程卡在灾难性回溯上。
    """
    service = AntiAILearningService(repository=repo)
    assert service.approve_pattern(
        novel_id="n1", keyword="缓缓.*雨", name="人工规则"
    )

    scanner = ClicheScanner(
        use_enhanced=True, extra_patterns=service.get_approved_patterns("n1")
    )

    literal_hits = scanner.scan_cliches("缓缓.*雨落在瓦上。")
    assert any(h.pattern == "人工规则" for h in literal_hits), "字面出现却没命中"

    # 「看着像正则」但字面不在这儿：按正则解释会命中，按字面解释不该命中。
    regex_hits = scanner.scan_cliches("缓缓的大雨落在瓦上。")
    assert not any(h.pattern == "人工规则" for h in regex_hits), "被当成正则了"


@pytest.mark.asyncio
async def test_a_rejected_rule_is_not_resurrected_by_the_next_chapter(repo):
    """人工驳回必须**永久**：下一章 AI 再提同一条，也不能复活。

    坑（复审 B1）：驳回只把 ``status`` 置 ``rejected``，
    但「别再提」清单只含**已批准**的关键词 ⇒ AI 下一章会重新提 ⇒
    ``upsert_approved`` 命中旧行并**无条件**改回 ``approved``
    ⇒ **唯一的人工控制手段失效**。
    """
    service = AntiAILearningService(repository=repo)
    repo.upsert_approved(novel_id="n1", keyword="缓缓的雨", name="本书句式")
    assert service.reject_pattern("n1", "缓缓的雨")
    assert repo.list_approved("n1") == []

    class _LLM:
        async def generate(self, prompt, config=None):
            return SimpleNamespace(content=json.dumps({"patterns": [
                {"keyword": "缓缓的雨", "name": "本书句式", "severity": "warning"},
            ]}))

    stored = await service.induce_and_store_patterns(
        novel_id="n1", chapter_number=4, content="缓缓的雨落在瓦上。", llm_service=_LLM(),
    )

    assert stored == [], "被驳回的模式不该再被归纳出来"
    assert repo.list_approved("n1") == [], "被驳回的模式被复活了"
    row = repo._get_db().fetch_one(
        "SELECT status FROM anti_ai_learned_patterns"
        " WHERE novel_id = 'n1' AND keyword = '缓缓的雨'"
    )
    assert row["status"] == "rejected", "状态必须仍是 rejected"


def test_a_manual_approval_can_restore_a_rejected_rule(repo):
    """人工**显式**恢复是允许的（驳回不是删除）。"""
    service = AntiAILearningService(repository=repo)
    repo.upsert_approved(novel_id="n1", keyword="缓缓的雨", name="本书句式")
    service.reject_pattern("n1", "缓缓的雨")

    assert service.approve_pattern(novel_id="n1", keyword="缓缓的雨", name="本书句式")
    assert [p[0] for p in repo.list_approved("n1")] == ["缓缓的雨"]


def test_the_repository_never_revives_a_rejected_rule(repo):
    """**仓库层**防线：AI 路径写一条被驳回过的规则 ⇒ 不复活、返回 None。

    这条与「别再提」清单（**服务层**防线）互相独立：
    仓库层被删掉 ⇒ 服务层仍拦得住（用户看不出差别），
    ⇒ 必须**单独**钉住仓库层，否则它没有牙齿。
    """
    repo.upsert_approved(novel_id="n1", keyword="缓缓的雨", name="本书句式")
    repo.reject("n1", "缓缓的雨")

    assert repo.upsert_approved(
        novel_id="n1", keyword="缓缓的雨", name="本书句式"
    ) is None, "被驳回的行被复活了"
    assert repo.list_approved("n1") == []
    row = repo._get_db().fetch_one(
        "SELECT status FROM anti_ai_learned_patterns"
        " WHERE novel_id = 'n1' AND keyword = '缓缓的雨'"
    )
    assert row["status"] == "rejected"


@pytest.mark.asyncio
async def test_the_rejected_keywords_are_told_to_the_ai(repo, monkeypatch):
    """**服务层**防线：被驳回的关键词必须进「别再提」清单。

    这条与仓库层的「不复活」互相独立：
    服务层被删掉 ⇒ 仓库层仍拦得住 ⇒ 必须**单独**钉住服务层。
    """
    from application.audit.services import anti_ai_pattern_induction as induction_mod

    repo.upsert_approved(novel_id="n1", keyword="缓缓的雨", name="本书句式")
    repo.reject("n1", "缓缓的雨")

    seen = {}

    async def _fake_induce(self, **kwargs):  # noqa: N805
        seen.update(kwargs)
        return [], SOURCE_AI

    monkeypatch.setattr(induction_mod.AntiAiPatternInducer, "induce", _fake_induce)

    service = AntiAILearningService(repository=repo)
    await service.induce_and_store_patterns(
        novel_id="n1",
        chapter_number=4,
        content="缓缓的雨落在瓦上。",
        llm_service=object(),
    )

    assert "缓缓的雨" in seen.get("known_keywords", []), (
        "被驳回的关键词没进『别再提』清单 ⇒ AI 会重新提 ⇒ 规则会被复活"
    )


def test_readding_the_same_keyword_updates_in_place(repo):
    """同一关键词重复归纳 ⇒ **原地更新**，不产生重复行。

    坑：``upsert_approved`` 原来用 ``ON CONFLICT(id)``，
    但 id 每次都是新 uuid ⇒ 冲突永不触发 ⇒ 同一关键词会攒出多行，
    扫描器就会把同一条规则算成多次命中。
    """
    repo.upsert_approved(novel_id="n1", keyword="缓缓的雨", name="初版")
    repo.upsert_approved(novel_id="n1", keyword="缓缓的雨", name="修订版")

    rows = repo.list_approved("n1")
    assert len(rows) == 1, f"同一关键词产生了 {len(rows)} 行"
    assert rows[0][1] == "修订版"


def test_the_auditor_really_loads_the_rules_through_the_learning_service(repo, monkeypatch):
    """审计器 ⇒ 学习服务 ⇒ 规则库，这条链必须真通。

    坑：``AntiAIAuditor._load_learned_patterns`` 在早前的用例里被整个
    monkeypatch 掉了 ⇒ **真实链路从未被测过**。
    这里不打桩，直接走真实实现。
    """
    from application.audit.services import anti_ai_audit as audit_mod
    from application.audit.services import anti_ai_learning as learning_mod

    repo.upsert_approved(novel_id="n1", keyword="缓缓的雨", name="本书句式")
    monkeypatch.setattr(
        learning_mod,
        "get_anti_ai_learning_service",
        lambda: AntiAILearningService(repository=repo),
    )

    report = audit_mod.AntiAIAuditor().scan_chapter(
        "ch-3", "缓缓的雨落在瓦上。", novel_id="n1"
    )

    assert any(h.pattern == "本书句式" for h in report.hits), "规则没进扫描器"


@pytest.mark.asyncio
async def test_a_contract_violating_output_is_unassessed():
    """输出不合契约 ⇒ **整体未评估**（一条模式都不产生）。

    坑：契约形状之前是 ``List[Dict[str, Any]]`` ⇒ 等于没约束，
    跳过 ``validate_output`` 整套守卫也不红。现在每条至少要有 ``keyword``。
    """
    class _LLM:
        async def generate(self, prompt, config=None):
            return SimpleNamespace(
                content=json.dumps({"patterns": [{"name": "没有关键词"}]})
            )

    patterns, source = await AntiAiPatternInducer(_LLM()).induce(
        chapter_number=3, content="缓缓的雨。"
    )

    assert patterns == [], "不合契约的输出不得产出模式"
    assert source == SOURCE_NONE


@pytest.mark.asyncio
async def test_the_daemon_triggers_the_induction(monkeypatch):
    """归纳必须真的在 daemon 里被触发。

    坑：能力写好了、库里也建了表，但没人调用 ⇒ 死链。
    真实 daemon 的 ``llm_service`` 由 ``init_daemon_dependencies`` 挂上
    （daemon_host.py:79），这里照抄那条路径。
    """
    from engine.runtime.daemon_host import DaemonHostMixin
    from application.audit.services import anti_ai_learning as learning_mod

    calls = []

    class _LearningService:
        async def induce_and_store_patterns(self, **kwargs):
            calls.append(kwargs)
            return []

    monkeypatch.setattr(
        learning_mod, "get_anti_ai_learning_service", lambda: _LearningService()
    )

    class _Host(DaemonHostMixin):
        llm_service = object()

        def _sync_anti_ai_audit(self, novel_id, chapter_number, content):
            return SimpleNamespace(
                hits=[],
                metrics=SimpleNamespace(
                    overall_assessment="纯净",
                    severity_score=0.0,
                    total_hits=0,
                    critical_hits=0,
                ),
                recommendations=[],
            )

        def _update_shared_state(self, novel_id, **fields):
            pass

    await _Host()._run_anti_ai_audit("n1", 3, "正文内容")

    assert len(calls) == 1, "daemon 没有触发归纳"
    assert calls[0]["novel_id"] == "n1"
    assert calls[0]["chapter_number"] == 3
    assert calls[0]["content"] == "正文内容"
