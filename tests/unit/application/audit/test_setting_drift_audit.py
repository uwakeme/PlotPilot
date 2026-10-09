"""A1: 设定漂移审计——基准收集、解析、降级语义。"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.audit.services.setting_drift_audit import SettingDriftAuditService


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
    return SettingDriftAuditService(llm_service=MagicMock(), bible_repository=MagicMock())


def test_collect_setting_facts_from_bible():
    svc = _service()
    svc._bible_repository.get_by_novel_id = MagicMock(return_value=_bible())
    facts = svc._collect_setting_facts("n-1")
    assert any("修为分九境" in f for f in facts)
    assert any("临渊城" in f for f in facts)
    assert all(f.startswith("[") for f in facts)


@pytest.mark.asyncio
async def test_audit_returns_unaudited_without_facts():
    svc = _service()
    svc._bible_repository.get_by_novel_id = MagicMock(return_value=None)

    result = await svc.audit_chapter("n-1", 3, "正文内容" * 50)

    assert result["audited"] is False  # 无基准不假装通过
    assert result["drift_score"] is None


@pytest.mark.asyncio
async def test_audit_parses_llm_response():
    svc = _service()
    svc._bible_repository.get_by_novel_id = MagicMock(return_value=_bible())
    svc._invoke = AsyncMock(return_value=json.dumps({
        "drift_score": 35,
        "issues": [
            {
                "severity": "warning",
                "category": "ability",
                "description": "正文让主角越境斩杀元婴修士，违反「不可越境战斗」",
                "suggestion": "改为借阵法/代价重伤对手",
            }
        ],
        "checked_facts": 2,
    }))

    result = await svc.audit_chapter("n-1", 3, "正文内容" * 50)

    assert result["audited"] is True
    assert result["drift_score"] == 35
    assert result["issues"][0]["category"] == "ability"


@pytest.mark.asyncio
async def test_audit_unparseable_output_is_unaudited():
    svc = _service()
    svc._bible_repository.get_by_novel_id = MagicMock(return_value=_bible())
    svc._invoke = AsyncMock(return_value="我觉得写得还不错！")

    result = await svc.audit_chapter("n-1", 3, "正文内容" * 50)

    assert result["audited"] is False
    assert "无法解析" in result["reason"]


@pytest.mark.asyncio
async def test_audit_llm_failure_is_unaudited():
    svc = _service()
    svc._bible_repository.get_by_novel_id = MagicMock(return_value=_bible())
    svc._invoke = AsyncMock(side_effect=RuntimeError("endpoint down"))

    result = await svc.audit_chapter("n-1", 3, "正文内容" * 50)

    assert result["audited"] is False
    assert "RuntimeError" in result["reason"]


@pytest.mark.asyncio
async def test_review_service_wires_drift_issues():
    from application.audit.services.chapter_review_service import ChapterReviewService

    svc = ChapterReviewService.__new__(ChapterReviewService)
    drift = MagicMock()
    drift.audit_chapter = AsyncMock(return_value={
        "audited": True,
        "drift_score": 40,
        "issues": [{"severity": "critical", "category": "rule", "description": "规则崩坏", "suggestion": "修"}],
    })
    svc.setting_drift_audit = drift

    chapter = MagicMock()
    chapter.number = 7
    chapter.content = "正文"
    result = await svc._check_setting_drift("n-1", chapter)

    assert result["audited"] is True
    drift.audit_chapter.assert_awaited_once_with(novel_id="n-1", chapter_number=7, content="正文")
