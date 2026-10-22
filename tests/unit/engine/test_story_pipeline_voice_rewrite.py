"""管线声线漂移定向改写：真实改写并如实记录 attempts（历史 stub 只谎报 applied）。"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.engine.services.voice_rewrite_result import VoiceRewriteOutcome
from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext


class _Pipeline(BaseStoryPipeline):
    """最小管线：只测改写循环本身。"""


def _make_ctx(similarity: float, drift: bool = True):
    ctx = PipelineContext(
        novel_id="n-1",
        chapter_number=7,
        chapter_content="原始正文" * 20,
        outline="大纲",
    )
    ctx.similarity_score = similarity
    ctx.drift_alert = drift

    fingerprint_repo = MagicMock()
    fingerprint_repo.get_by_novel.return_value = None
    voice = MagicMock()
    voice.fingerprint_repo = fingerprint_repo
    voice.score_chapter.return_value = {"similarity_score": 0.9, "drift_alert": False}
    ctx.voice_drift_service = voice
    ctx.llm_service = MagicMock()
    return ctx


@pytest.mark.asyncio
async def test_rewrite_loop_rewrites_content_and_stops_when_score_recovers(monkeypatch):
    """改写真实发生：内容被替换、applied=True、复评达标后停止。"""
    ctx = _make_ctx(similarity=0.5)
    pipeline = _Pipeline.__new__(_Pipeline)  # 绕过 __init__ 的重依赖

    async def _fake_invoke(p, c, variables):
        # E-03：桩跟着接口改（返回改写产出，不只是正文）
        return VoiceRewriteOutcome(rewritten="改写后的正文" * 20)

    monkeypatch.setattr(_Pipeline, "_invoke_voice_rewrite", _fake_invoke)

    await pipeline._apply_voice_rewrite_loop(ctx)

    assert ctx.rewrite_applied is True
    assert ctx.rewrite_attempts == 1
    assert ctx.chapter_content.startswith("改写后的正文")
    assert ctx.similarity_score == 0.9
    assert ctx.drift_alert is False


@pytest.mark.asyncio
async def test_rewrite_loop_records_failure_honestly(monkeypatch):
    """LLM 失败时如实记录 attempts，不谎报 rewrite_applied。"""
    ctx = _make_ctx(similarity=0.4)
    pipeline = _Pipeline.__new__(_Pipeline)

    async def _fail_invoke(p, c, variables):
        return None

    monkeypatch.setattr(_Pipeline, "_invoke_voice_rewrite", _fail_invoke)

    await pipeline._apply_voice_rewrite_loop(ctx)

    assert ctx.rewrite_applied is False  # 没改成就不能说改过
    assert ctx.rewrite_attempts == 1
    assert ctx.chapter_content.startswith("原始正文")


@pytest.mark.asyncio
async def test_rewrite_loop_gives_up_when_output_unchanged(monkeypatch):
    """改写输出与原文相同：停止重试，attempts 如实记录。"""
    ctx = _make_ctx(similarity=0.4)
    pipeline = _Pipeline.__new__(_Pipeline)

    async def _same_invoke(p, c, variables):
        return VoiceRewriteOutcome(rewritten=ctx.chapter_content)

    monkeypatch.setattr(_Pipeline, "_invoke_voice_rewrite", _same_invoke)

    await pipeline._apply_voice_rewrite_loop(ctx)

    assert ctx.rewrite_applied is False
    assert ctx.rewrite_attempts == 1


def test_t0_overflow_warns_when_high_importance_dropped(caplog):
    """高优先级伏笔被 T0 上限挤出时必须告警（回收断链风险），不能静默丢弃。"""
    import logging
    from domain.novel.entities.foreshadowing_registry import ForeshadowingRegistry
    from domain.novel.value_objects.foreshadowing import (
        Foreshadowing,
        ImportanceLevel,
        ForeshadowingStatus,
    )
    from domain.novel.value_objects.novel_id import NovelId

    registry = ForeshadowingRegistry(id="fr-1", novel_id=NovelId("n-1"))
    for i in range(10):
        registry._foreshadowings.append(Foreshadowing(
            id=f"f-{i}",
            description=f"关键伏笔{i}",
            importance=ImportanceLevel.HIGH,
            status=ForeshadowingStatus.PLANTED,
            planted_in_chapter=1,
            suggested_resolve_chapter=100 + i,
        ))

    with caplog.at_level(logging.WARNING, logger="domain.novel.entities.foreshadowing_registry"):
        eligible = registry.get_t0_eligible_foreshadowings(
            current_chapter=50, max_items=8
        )

    assert len(eligible) == 8
    assert any("高优先级伏笔被挤出" in r.message for r in caplog.records)
