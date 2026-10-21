"""F-13b：设定漂移审计分段送审。

两个缺陷，一前一后：

1. ``content[:12000]`` 截断 —— 长章后半段的漂移永远审不到，却照样返回
   ``audited=True``（**假通过**）。现在改为按段落边界切段逐段送审；
   段数超过 token 护栏时只审前 N 段，并**显式报覆盖不全**
   （``coverage="partial"`` / ``audited=False`` / ``drift_score=None``）。
2. ``audit.setting.drift`` 的调用契约从未发布 ⇒ ``_invoke`` 抛
   ``PromptTemplateUnavailable`` ⇒ 整套设定漂移审计**一次都没跑过**，
   而且外面只看到「没审」，看不到原因。

本文件守的是**关系与不变量**，不钉死具体数值。
"""
import json
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.audit.services.setting_drift_audit import (
    _MAX_SEGMENTS,
    _SEGMENT_CHARS,
    SettingDriftAuditService,
    _split_into_segments,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]


def _bible():
    bible = MagicMock()
    ws = MagicMock()
    ws.category = "力量体系"
    ws.content = "修为分九境，不可越境战斗"
    loc = MagicMock()
    loc.name = "临渊城"
    loc.description = "北境边城，常年冰封"
    loc.location_type = "city"
    bible.world_settings = [ws]
    bible.locations = [loc]
    return bible


def _service():
    svc = SettingDriftAuditService(llm_service=MagicMock(), bible_repository=MagicMock())
    svc._bible_repository.get_by_novel_id = MagicMock(return_value=_bible())
    return svc


def _capture(svc, responses):
    """让 ``_invoke`` 依次返回 responses（可为 str 或 Exception），并记录每次入参。"""
    calls = []

    async def _fake(variables):
        calls.append(variables)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    svc._invoke = _fake
    return calls


def _ok(score=0, issues=None):
    return json.dumps({"drift_score": score, "issues": issues or []})


# ── _split_into_segments 的不变量 ────────────────────────────────


def test_split_short_text_is_single_segment():
    text = "短章节正文。" * 20
    assert _split_into_segments(text, _SEGMENT_CHARS) == [text]


def test_split_keeps_every_character():
    """无缝无丢字 —— 否则「整章都审过了」这句话就是假的。

    文本里同时放了「段内硬切」和「段落空行切」两种切点：
    两种切法各自吞一个字都要被抓到。
    """
    text = (
        ("第一段内容。" * 200)
        + "\n\n"
        + ("第二段内容。" * 200)
        + "\n\n"
        + ("第三段内容。" * 200)
    )
    segments = _split_into_segments(text, 500)
    assert len(segments) > 1
    assert "".join(segments) == text


def test_split_each_segment_within_limit():
    text = ("段落。" * 900) + "\n\n" + ("另一段。" * 900)
    segments = _split_into_segments(text, 500)
    assert all(len(s) <= 500 for s in segments)


def test_split_cuts_at_paragraph_boundary():
    """优先在段落空行处切，避免把一个词劈成两半送审。"""
    text = "甲" * 400 + "\n\n" + "乙" * 400
    segments = _split_into_segments(text, 500)
    assert segments[0] == "甲" * 400 + "\n\n"


def test_split_hard_cuts_when_no_paragraph_break():
    text = "无换行长文本" * 500
    segments = _split_into_segments(text, 500)
    assert len(segments) > 1
    assert "".join(segments) == text
    assert all(len(s) <= 500 for s in segments)


def test_split_empty_text():
    assert _split_into_segments("", 100) == []


# ── audit_chapter：短章行为不变 ──────────────────────────────────


@pytest.mark.asyncio
async def test_short_chapter_sends_whole_text_in_one_call():
    svc = _service()
    calls = _capture(svc, [_ok(score=10)])
    text = "短章节正文。" * 20

    result = await svc.audit_chapter("n-1", 3, text)

    assert len(calls) == 1
    assert calls[0]["chapter_content"] == text
    assert calls[0]["segment_index"] == 1
    assert calls[0]["segment_total"] == 1
    assert result["audited"] is True
    assert result["coverage"] == "full"
    assert result["drift_score"] == 10
    assert result["segments_total"] == 1
    assert result["segments_audited"] == 1


# ── audit_chapter：长章后半段必须真的送到模型（F-13 核心）───────


@pytest.mark.asyncio
async def test_long_chapter_whole_text_reaches_the_model():
    """长章不再被截断：送出去的内容拼起来必须等于全文。"""
    svc = _service()
    calls = _capture(svc, [_ok()])
    text = "开头段落。" * 1200 + "\n\n" + "结尾段落。" * 1200

    result = await svc.audit_chapter("n-1", 3, text)

    assert len(calls) > 1
    sent = "".join(c["chapter_content"] for c in calls)
    assert sent == text
    assert all(len(c["chapter_content"]) <= _SEGMENT_CHARS for c in calls)
    assert result["audited"] is True
    assert result["coverage"] == "full"


@pytest.mark.asyncio
async def test_tail_only_violation_is_detected():
    """只出现在尾部（旧实现会连同正文一起截掉）的违规必须能被发现。"""
    svc = _service()

    async def _fake(variables):
        if "尾部违规标记" in variables["chapter_content"]:
            return _ok(score=70, issues=[{
                "severity": "critical",
                "category": "ability",
                "description": "尾部违反修为分九境",
                "suggestion": "改回九境",
            }])
        return _ok(score=0)

    svc._invoke = _fake
    text = "开头段落。" * 2000 + "\n\n" + "填充内容。" * 1000 + "尾部违规标记。"

    result = await svc.audit_chapter("n-1", 3, text)

    assert result["audited"] is True
    assert result["drift_score"] == 70
    assert any("尾部违反" in i["description"] for i in result["issues"])


@pytest.mark.asyncio
async def test_drift_score_is_max_across_segments():
    """任一段漂移即本章漂移：合并取最大，不是平均也不是最后一段的。"""
    svc = _service()
    calls = _capture(svc, [_ok(score=12), _ok(score=88), _ok(score=30)])
    text = "段落内容。" * 2500  # 12500 字 ⇒ 3 段

    result = await svc.audit_chapter("n-1", 3, text)

    assert len(calls) == 3
    assert result["drift_score"] == 88


@pytest.mark.asyncio
async def test_issues_merged_across_segments():
    svc = _service()
    calls = _capture(svc, [
        _ok(score=5, issues=[{"severity": "warning", "category": "rule", "description": "甲规则漂移"}]),
        _ok(score=5, issues=[{"severity": "critical", "category": "ability", "description": "乙越境"}]),
    ])
    text = "段落内容。" * 1500  # 7500 字 ⇒ 2 段

    result = await svc.audit_chapter("n-1", 3, text)

    assert len(calls) == 2
    descriptions = {i["description"] for i in result["issues"]}
    assert descriptions == {"甲规则漂移", "乙越境"}


# ── 覆盖不全：显式报未审完，绝不报假通过 ──────────────────────────


@pytest.mark.asyncio
async def test_too_many_segments_reports_partial_not_pass():
    svc = _service()
    calls = _capture(svc, [_ok(score=5)])
    text = "超长段落。" * 5000  # 25000 字 ⇒ 5 段，超出段数护栏

    result = await svc.audit_chapter("n-1", 3, text)

    assert len(calls) == _MAX_SEGMENTS
    assert result["audited"] is False
    assert result["coverage"] == "partial"
    assert result["drift_score"] is None
    assert result["segments_audited"] == _MAX_SEGMENTS
    assert result["segments_total"] > _MAX_SEGMENTS
    assert "过长" in result["reason"]
    # 模型看到的「整章共几段」必须是真实总段数，不能是「实际送审了几段」——
    # 否则模型会以为这就是整章，替没审到的部分下结论
    assert calls[0]["segment_total"] == result["segments_total"]
    assert calls[0]["segment_total"] > _MAX_SEGMENTS
    assert calls[0]["segment_index"] == 1


@pytest.mark.asyncio
async def test_partial_keeps_issues_from_audited_segments():
    """覆盖不全时，已经查到的问题要留着供人工复核，不能一起丢掉。"""
    svc = _service()
    calls = _capture(svc, [_ok(score=30, issues=[{
        "severity": "warning", "category": "rule", "description": "规则漂移", "suggestion": "改",
    }])])
    text = "超长段落。" * 5000

    result = await svc.audit_chapter("n-1", 3, text)

    assert result["audited"] is False
    assert result["drift_score"] is None
    assert result["segments_audited"] == _MAX_SEGMENTS
    # 每个审成的段各报一次，同一条描述
    assert len(result["issues"]) == _MAX_SEGMENTS
    assert {i["description"] for i in result["issues"]} == {"规则漂移"}


@pytest.mark.asyncio
async def test_one_segment_failure_reports_partial_and_keeps_other_findings():
    svc = _service()
    calls = _capture(svc, [
        _ok(score=10, issues=[{
            "severity": "critical", "category": "ability", "description": "越境", "suggestion": "改",
        }]),
        RuntimeError("endpoint down"),
    ])
    text = "段落内容。" * 1500  # 7500 字 ⇒ 2 段

    result = await svc.audit_chapter("n-1", 3, text)

    assert result["audited"] is False
    assert result["coverage"] == "partial"
    assert result["drift_score"] is None
    assert result["segments_audited"] == 1
    assert result["segments_total"] == 2
    assert "RuntimeError" in result["reason"]
    assert [i["description"] for i in result["issues"]] == ["越境"]


@pytest.mark.asyncio
async def test_all_segments_failing_is_not_audited():
    svc = _service()
    calls = _capture(svc, [RuntimeError("endpoint down")])
    text = "段落内容。" * 1500

    result = await svc.audit_chapter("n-1", 3, text)

    assert result["audited"] is False
    assert result["coverage"] == "none"
    assert result["drift_score"] is None
    assert result["issues"] == []
    assert "RuntimeError" in result["reason"]


@pytest.mark.asyncio
async def test_unparseable_segment_output_is_not_audited():
    svc = _service()
    calls = _capture(svc, ["我觉得写得还不错！"])

    result = await svc.audit_chapter("n-1", 3, "短正文。")

    assert result["audited"] is False
    assert result["drift_score"] is None
    assert "无法解析" in result["reason"]


@pytest.mark.asyncio
async def test_no_baseline_facts_is_not_audited():
    svc = _service()
    svc._bible_repository.get_by_novel_id = MagicMock(return_value=None)
    calls = _capture(svc, [_ok()])

    result = await svc.audit_chapter("n-1", 3, "短正文。")

    assert result["audited"] is False
    assert result["drift_score"] is None
    assert calls == []


# ── 调用方：覆盖不全时已查到的问题不能被静默丢弃 ─────────────────


@pytest.mark.asyncio
async def test_review_chapter_surfaces_partial_drift_issues():
    from application.audit.services.chapter_review_service import ChapterReviewService

    svc = ChapterReviewService.__new__(ChapterReviewService)
    svc.chapter_repo = MagicMock()
    chapter = MagicMock()
    chapter.number = 7
    chapter.content = "正文"
    svc.chapter_repo.get_by_number = MagicMock(return_value=chapter)

    svc._check_character_consistency = AsyncMock(return_value=[])
    svc._check_timeline_consistency = AsyncMock(return_value=[])
    svc._check_storyline_consistency = AsyncMock(return_value=[])
    svc._check_foreshadowing_usage = AsyncMock(return_value=[])
    svc._generate_improvement_suggestions = AsyncMock(return_value=[])

    drift = MagicMock()
    drift.audit_chapter = AsyncMock(return_value={
        "audited": False,
        "reason": "章节过长，仅送审前 4/6 段",
        "drift_score": None,
        "issues": [{"severity": "critical", "category": "rule", "description": "规则崩坏", "suggestion": "修"}],
        "coverage": "partial",
        "segments_audited": 4,
        "segments_total": 6,
    })
    svc.setting_drift_audit = drift

    result = await svc.review_chapter("n-1", 7)

    drift_issues = [i for i in result.issues if i.issue_type == "setting_drift_rule"]
    assert len(drift_issues) == 1
    assert drift_issues[0].description == "规则崩坏"
    # 覆盖不全时不能拿漂移分当结论 —— 那是 None
    assert "未完成" in drift_issues[0].location
    assert "None" not in drift_issues[0].location


# ── 调用契约必须可发布（否则整套审计一次都跑不起来）─────────────


def test_setting_drift_audit_contract_is_publishable(monkeypatch):
    """``audit.setting.drift`` 必须有可发布的调用契约。

    没有这条契约，``ensure_invocation_contract`` 抛
    ``ValueError: Unsupported invocation contract`` → ``_invoke`` 抛
    ``PromptTemplateUnavailable`` → 审计恒返回 ``audited=False``。
    """
    import infrastructure.persistence.database.sqlite_ai_invocation_repository as repo_mod
    from application.ai_invocation.contracts import autopilot_helpers
    from application.ai_invocation.contracts.registry import InvocationContractRegistry
    from application.ai_invocation.dtos import InvocationSpec

    published = {}

    def _fake_ensure(db=None):
        published["ensured"] = True

    class _FakeRepo:
        def __init__(self, db):
            pass

        def get(self, operation, node_key):
            published["got"] = (operation, node_key)
            return InvocationSpec(operation=operation, node_key=node_key)

    monkeypatch.setattr(autopilot_helpers, "ensure_setting_drift_audit_contract", _fake_ensure)
    monkeypatch.setattr(repo_mod, "SqliteInvocationSpecRepository", _FakeRepo)

    spec = InvocationContractRegistry(db=object()).ensure_published(
        "audit.setting.drift", "setting-drift-audit"
    )

    assert published.get("ensured") is True
    assert published.get("got") == ("audit.setting.drift", "setting-drift-audit")
    assert spec.node_key == "setting-drift-audit"


# ── 提示词契约：代码传的段号变量，模板必须真的用上 ───────────────


def test_prompt_template_uses_segment_variables():
    """代码传了 segment_index/segment_total，模板必须引用，否则模型看不到自己看的是第几段。"""
    user_md = (
        REPO_ROOT / "infrastructure/ai/prompt_packages/nodes/setting-drift-audit/user.md"
    ).read_bytes().decode("utf-8")
    assert "{segment_index}" in user_md
    assert "{segment_total}" in user_md
