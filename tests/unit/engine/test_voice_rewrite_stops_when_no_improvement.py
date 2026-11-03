# -*- coding: utf-8 -*-
"""第 85 批：定向修文循环「**分数没变好就停**」。

起因（**真跑一本书**时暴露出来的钱坑）
----------------------------------------
生产实跑（``novel-1792047827087`` 第 8 章）：连续 **3 次**定向修文，
相似度**始终是 0.4167** ⇒ 每次都是「改了也没变好」。

根因：相似度是「**落在容差内的维度计数**」（离散分桶）⇒ 重写后的风格向量
常常落回同一批桶 ⇒ 分数纹丝不动 ⇒ 后两次调用**纯烧钱**（每章 2 次模型调用）。

⇒ 两条改写链（StoryPipeline 与 daemon）现在都「没变好就停」。
⚠ 两条链**共用**同一个判定函数（同一逻辑不许写两份）。

⚠ 「分数变好」的**判定口径**（连续量 vs 离散分桶）本身是**设计层**的事
   ⇒ 本批只做「没变好就停」（省钱、不改语义）⇒ 口径登记在台账里待拍板。
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


# ── 1. 判定函数的语义 ───────────────────────────────────────────────────


def test_the_improvement_check_needs_a_real_gain():
    """⚠ 变好 = 严格变大（带一点容差，避免浮点噪声）。"""
    from application.engine.services.voice_rewrite_result import (
        voice_similarity_improved,
    )

    assert voice_similarity_improved(0.4, 0.5) is True
    assert voice_similarity_improved(0.4, 0.4) is False, "分数没动却算「变好」"
    assert voice_similarity_improved(0.5, 0.4) is False, "分数变差却算「变好」"


@pytest.mark.parametrize("before,after", [(None, 0.5), (0.4, None), (None, None), ("x", 0.5)])
def test_an_unreadable_score_never_counts_as_improvement(before, after):
    """⚠ 任一侧缺失 / 解析不了 ⇒ 视为「没变好」（宁可少改一次，别空烧调用）。"""
    from application.engine.services.voice_rewrite_result import (
        voice_similarity_improved,
    )

    assert voice_similarity_improved(before, after) is False


# ── 2. 行为级：StoryPipeline 那条链 ────────────────────────────────────


@pytest.mark.asyncio
async def test_the_pipeline_stops_when_the_score_does_not_improve(monkeypatch):
    """⚠ 分数不动 ⇒ **只跑 1 轮**（而不是白烧 3 次模型调用）。"""
    from engine.pipeline.base import BaseStoryPipeline
    from engine.pipeline.context import PipelineContext

    calls: list[dict] = []

    class _Pipeline(BaseStoryPipeline):
        pass

    async def _fake_invoke(p, c, variables):
        calls.append(dict(variables))
        return SimpleNamespace(
            rewritten="第%d次改写" % len(calls),
            diagnosis="诊断",
            next_technique="手法",
            structured=True,
        )

    monkeypatch.setattr(_Pipeline, "_invoke_voice_rewrite", _fake_invoke)

    ctx = PipelineContext(
        novel_id="n-1", chapter_number=7, chapter_content="原始正文" * 20, outline="大纲"
    )
    ctx.similarity_score = 0.30
    ctx.drift_alert = True
    voice = MagicMock()
    voice.fingerprint_repo = None
    # ⚠ 复评**一直**是同一个分数
    # ⚠ 第 86 批：真因是**缓存键不含内容身份**（改写后重评命中旧缓存），
    #   不是「离散分桶」；取值也按新口径下调（修文阈值 0.40 ⇒ 取值必须低于它）。
    voice.score_chapter.return_value = {"similarity_score": 0.30, "drift_alert": True}
    ctx.voice_drift_service = voice
    ctx.llm_service = MagicMock()

    pipeline = _Pipeline.__new__(_Pipeline)
    await pipeline._apply_voice_rewrite_loop(ctx)

    assert len(calls) == 1, (
        "分数没变好却还改了 %d 次 ⇒ 白烧了 %d 次模型调用" % (len(calls), len(calls) - 1)
    )
    assert ctx.rewrite_attempts == 1


@pytest.mark.asyncio
async def test_the_pipeline_keeps_going_while_the_score_improves(monkeypatch):
    """⚠ 对照组：分数**在变好** ⇒ 继续改（别把这个功能改没了）。"""
    from engine.pipeline.base import BaseStoryPipeline
    from engine.pipeline.context import PipelineContext

    calls: list[dict] = []

    class _Pipeline(BaseStoryPipeline):
        pass

    async def _fake_invoke(p, c, variables):
        calls.append(dict(variables))
        return SimpleNamespace(
            rewritten="第%d次改写" % len(calls),
            diagnosis="诊断",
            next_technique="手法",
            structured=True,
        )

    monkeypatch.setattr(_Pipeline, "_invoke_voice_rewrite", _fake_invoke)

    ctx = PipelineContext(
        novel_id="n-1", chapter_number=7, chapter_content="原始正文" * 20, outline="大纲"
    )
    ctx.similarity_score = 0.30
    ctx.drift_alert = True
    voice = MagicMock()
    voice.fingerprint_repo = None
    voice.score_chapter.side_effect = [
        {"similarity_score": 0.35, "drift_alert": True},
        {"similarity_score": 0.40, "drift_alert": True},
        {"similarity_score": 0.45, "drift_alert": True},
    ]
    ctx.voice_drift_service = voice
    ctx.llm_service = MagicMock()

    pipeline = _Pipeline.__new__(_Pipeline)
    await pipeline._apply_voice_rewrite_loop(ctx)

    assert len(calls) >= 2, "分数在变好却只改了一次 ⇒ 把「继续改」的能力改没了"


# ── 3. 行为级：daemon 那条链 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_daemon_chain_stops_when_the_score_does_not_improve(monkeypatch):
    """⚠ daemon 那条链也必须「没变好就停」（两条链同口径）。"""
    from engine.runtime.daemon_host import DaemonHostMixin

    rewrites: list[str] = []

    class _Host(DaemonHostMixin):
        def __init__(self):
            pass

        def _is_still_running(self, _novel):
            return True

        async def _rewrite_chapter_for_voice(self, novel, chapter, content, similarity, attempt, **kw):
            rewrites.append(content)
            return SimpleNamespace(
                rewritten="改写%d" % len(rewrites), diagnosis="d", next_technique="t"
            )

        def _save_chapter_ephemeral(self, *args, **kwargs):
            return None

        async def _score_voice_only(self, novel_id, number, content):
            # ⚠ 复评**一直**是同一个分数（第 86 批：真因是缓存键不含内容身份）
            return {"similarity_score": 0.30, "drift_alert": False}

    host = _Host()
    novel = SimpleNamespace(novel_id=SimpleNamespace(value="n-1"))
    chapter = SimpleNamespace(number=8)

    _content, _result = await host._apply_voice_rewrite_loop(
        novel, chapter, "原始正文", {"drift_alert": True, "similarity_score": 0.30}
    )

    assert len(rewrites) == 1, (
        "daemon 那条链：分数没变好却改了 %d 次 ⇒ 白烧了 %d 次模型调用"
        % (len(rewrites), len(rewrites) - 1)
    )


# ── 4. 两条链**共用**同一个判定（不许写两份） ────────────────────────────


def test_both_chains_use_the_shared_helper():
    """⚠ 同一逻辑写两份 ⇒ 迟早有一处漏改（这次就是 daemon 与 pipeline 两条）。"""
    from application.engine.services.voice_rewrite_result import (
        voice_similarity_improved,
    )

    for rel in (
        "engine/pipeline/base.py",
        "engine/runtime/daemon_host.py",
    ):
        src = (ROOT / rel).read_text(encoding="utf-8-sig")
        assert "voice_similarity_improved(" in src, "%s 没用共用的判定函数" % rel
        # 不许自己再写一份「变好没有」
        assert "_voice_similarity_improved(" not in src, (
            "%s 自己写了一份判定 ⇒ 两份逻辑会漂" % rel
        )
    assert callable(voice_similarity_improved)
