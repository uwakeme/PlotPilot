"""F-14b：审稿总分由 AI 给（代码公式删除）。

红线（本文件负责钉死）：

1. **公式删除** —— ``100 − critical×15 − warning×5 − suggestion×2`` 那种
   「把问题条数换算成分数」的写法不许留在代码里。
2. **总分来自 AI** —— 复用活的那条审稿路径同一个服务（``ChapterAIReviewService``），
   两条路径口径一致。
3. **判不出来 ⇒ 未评估（None）** —— 绝不回落成 70 / 100 之类的中位分：
   那个数会被读成「AI 打了 70 分」。
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.audit.services.chapter_review_service import ChapterReviewService

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
SERVICE_PATH = REPO_ROOT / "application/audit/services/chapter_review_service.py"


def _service(ai_review_service=None):
    svc = ChapterReviewService.__new__(ChapterReviewService)
    svc.ai_review_service = ai_review_service
    return svc


def _chapter():
    return SimpleNamespace(
        number=7,
        title="第七章",
        content="沈砚把卷宗摔在案上。",
        outline="沈砚当堂翻案",
        generation_hint="",
    )


# ── 1. 公式删除 ────────────────────────────────────────────────────


def test_the_formula_is_gone():
    assert not hasattr(ChapterReviewService, "_calculate_overall_score")


def test_no_base_score_identifier_remains():
    """AST 扫标识符 —— 公式里的 ``base_score`` 不许还在。"""
    tree = ast.parse(SERVICE_PATH.read_bytes().decode("utf-8"))
    names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    assert "base_score" not in names


# ── 2. 总分来自 AI ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_overall_score_comes_from_the_ai_review():
    ai = MagicMock()
    ai.review = AsyncMock(return_value=SimpleNamespace(score=88))

    score = await _service(ai)._judge_overall_score(_chapter())

    assert score == 88
    ai.review.assert_awaited_once()


@pytest.mark.asyncio
async def test_ai_review_receives_the_chapter_content():
    """分数必须基于**正文** —— 只给问题清单的话，干净章节会被打出假高分。"""
    ai = MagicMock()
    ai.review = AsyncMock(return_value=SimpleNamespace(score=90))

    await _service(ai)._judge_overall_score(_chapter())

    kwargs = ai.review.call_args.kwargs
    assert kwargs["chapter_content"] == "沈砚把卷宗摔在案上。"
    assert kwargs["chapter_number"] == 7


# ── 3. 判不出来 ⇒ 未评估（绝不编一个分）────────────────────────────


@pytest.mark.asyncio
async def test_ai_failure_is_unassessed_not_a_mid_score():
    class _Boom:
        async def review(self, **kwargs):
            raise RuntimeError("endpoint down")

    score = await _service(_Boom())._judge_overall_score(_chapter())

    assert score is None


@pytest.mark.asyncio
async def test_contract_violation_is_unassessed():
    from application.audit.services.chapter_ai_review_service import (
        ChapterAIReviewContractError,
    )

    class _Violates:
        async def review(self, **kwargs):
            raise ChapterAIReviewContractError("AI 审阅结果缺少 score")

    score = await _service(_Violates())._judge_overall_score(_chapter())

    assert score is None


@pytest.mark.asyncio
async def test_without_the_ai_service_it_is_unassessed():
    assert await _service(None)._judge_overall_score(_chapter()) is None


# ── 4. 主流程与响应模型 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_chapter_returns_the_ai_score():
    svc = ChapterReviewService.__new__(ChapterReviewService)
    svc.chapter_repo = MagicMock()
    svc.chapter_repo.get_by_number = MagicMock(return_value=_chapter())
    svc._check_character_consistency = AsyncMock(return_value=[])
    svc._check_timeline_consistency = AsyncMock(return_value=[])
    svc._check_storyline_consistency = AsyncMock(return_value=[])
    svc._check_foreshadowing_usage = AsyncMock(return_value=[])
    svc._generate_improvement_suggestions = AsyncMock(return_value=[])
    svc.setting_drift_audit = None
    ai = MagicMock()
    ai.review = AsyncMock(return_value=SimpleNamespace(score=76))
    svc.ai_review_service = ai

    result = await svc.review_chapter("n-1", 7)

    assert result.overall_score == 76


def test_response_model_defaults_to_unassessed():
    from interfaces.api.v1.audit.chapter_review_routes import ChapterReviewResponse

    response = ChapterReviewResponse(
        chapter_number=7, issues=[], improvement_suggestions=[], reviewed_at="now"
    )

    assert response.overall_score is None
