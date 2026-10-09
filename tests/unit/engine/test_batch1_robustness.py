"""批次 1（E 组健壮性）回归测试：E1/E2/E3/E5/E6/E7/E11 等。"""
import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── E1: _call_with_timeout 超时/异常计入失败上报 ──────────────────────────

@pytest.mark.asyncio
async def test_call_with_timeout_reports_llm_failure_on_timeout():
    from engine.runtime.daemon_host import DaemonHostMixin

    host = object.__new__(DaemonHostMixin)
    host._report_llm_failure = MagicMock()
    host._update_shared_state = MagicMock()

    async def _hang():
        await asyncio.sleep(999)

    result = await host._call_with_timeout(_hang(), timeout=0.1, novel_id="n-1", label="审计", timeout_default={})
    assert result == {}
    host._report_llm_failure.assert_called_once()
    assert "超时" in host._report_llm_failure.call_args[0][1]


@pytest.mark.asyncio
async def test_call_with_timeout_reports_llm_failure_on_exception():
    from engine.runtime.daemon_host import DaemonHostMixin

    host = object.__new__(DaemonHostMixin)
    host._report_llm_failure = MagicMock()
    host._update_shared_state = MagicMock()

    async def _boom():
        raise RuntimeError("auth failed")

    result = await host._call_with_timeout(_boom(), timeout=1.0, novel_id="n-1", label="审计", timeout_default=None)
    assert result is None
    host._report_llm_failure.assert_called_once()
    assert "RuntimeError" in host._report_llm_failure.call_args[0][1]


@pytest.mark.asyncio
async def test_call_with_timeout_success_does_not_report():
    from engine.runtime.daemon_host import DaemonHostMixin

    host = object.__new__(DaemonHostMixin)
    host._report_llm_failure = MagicMock()
    host._update_shared_state = MagicMock()

    async def _ok():
        return {"fine": True}

    result = await host._call_with_timeout(_ok(), timeout=1.0, novel_id="n-1", label="审计", timeout_default=None)
    assert result == {"fine": True}
    host._report_llm_failure.assert_not_called()


# ── E2: 张力评分解析失败不伪造 50 分 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_tension_llm_unparseable_output_returns_none(monkeypatch):
    from engine.pipeline.base import BaseStoryPipeline

    pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)

    class _Invoker:
        async def invoke_text(self, request):
            return "评分失败无法输出数字"

    monkeypatch.setattr(
        "application.ai_invocation.autopilot.factory.get_or_create_autopilot_helper_invoker",
        lambda owner: _Invoker(),
    )
    ctx = MagicMock()
    ctx.novel_id = "n-1"
    ctx.chapter_number = 3
    ctx.chapter_content = "正文" * 100
    ctx.llm_service = MagicMock()

    assert await pipeline._score_tension_via_llm(ctx) is None


# ── E3: 僵死会话阈值可配且默认 ≥ provider 读超时 ─────────────────────────

def test_stalled_invocation_threshold_default_900():
    from engine.runtime.act_planning_delegate import _stalled_invocation_max_age_seconds

    import os
    os.environ.pop("PLOTPILOT_STALLED_INVOCATION_MAX_AGE_SECONDS", None)
    assert _stalled_invocation_max_age_seconds() == 900.0


def test_stalled_invocation_threshold_env_override(monkeypatch):
    from engine.runtime.act_planning_delegate import _stalled_invocation_max_age_seconds

    monkeypatch.setenv("PLOTPILOT_STALLED_INVOCATION_MAX_AGE_SECONDS", "1200")
    assert _stalled_invocation_max_age_seconds() == 1200.0


def test_stalled_invocation_threshold_invalid_env_falls_back(monkeypatch):
    from engine.runtime.act_planning_delegate import _stalled_invocation_max_age_seconds

    monkeypatch.setenv("PLOTPILOT_STALLED_INVOCATION_MAX_AGE_SECONDS", "abc")
    assert _stalled_invocation_max_age_seconds() == 900.0


# ── E4: legacy 空拍重试分支存在（源码契约，重函数不宜单测） ──────────────

def test_legacy_beat_empty_retry_branch_exists():
    text = Path("engine/runtime/legacy_writing_delegate.py").read_text(encoding="utf-8")
    assert "首次生成内容为空，退避后重试一次" in text
    assert "连续两次生成为空" in text
    assert "last_empty_beat_chapter" in text


# ── E5: 章节落库按效果确认（不等全队列） ─────────────────────────────────

def test_wait_for_chapter_persistence_polls_until_visible():
    from engine.pipeline.base import BaseStoryPipeline

    pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)
    pipeline.CHAPTER_PERSIST_WAIT_SECONDS = 0.5
    ctx = MagicMock()

    calls = {"n": 0}

    def _visible(c):
        calls["n"] += 1
        return calls["n"] >= 3  # 第 3 次轮询时可见

    pipeline._chapter_completed_in_repository = _visible
    assert pipeline._wait_for_chapter_persistence(ctx) is True
    assert calls["n"] >= 3


def test_wait_for_chapter_persistence_times_out():
    from engine.pipeline.base import BaseStoryPipeline

    pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)
    pipeline.CHAPTER_PERSIST_WAIT_SECONDS = 0.3
    ctx = MagicMock()
    pipeline._chapter_completed_in_repository = lambda c: False
    assert pipeline._wait_for_chapter_persistence(ctx) is False


# ── E6: BoundedStore 容量 LRU + TTL ──────────────────────────────────────

def test_bounded_store_evicts_lru_over_capacity():
    from application.blueprint.services.bounded_stores import BoundedStore

    store = BoundedStore(max_size=3, ttl_seconds=3600)
    for i in range(5):
        store.set(f"k{i}", i)
    assert len(store) == 3
    assert store.get("k0") is None  # 最老的被淘汰
    assert store.get("k4") == 4


def test_bounded_store_ttl_expiry():
    import time as _time

    from application.blueprint.services.bounded_stores import BoundedStore

    store = BoundedStore(max_size=3, ttl_seconds=0.01)  # 极短 TTL
    store.set("k", "v")
    _time.sleep(0.02)
    assert store.get("k") is None


def test_bounded_store_ttl_zero_means_disabled():
    from application.blueprint.services.bounded_stores import BoundedStore

    store = BoundedStore(max_size=3, ttl_seconds=0.0)  # 0 = 禁用 TTL（与 MemoryEngine 范式一致）
    store.set("k", "v")
    assert store.get("k") == "v"


def test_bounded_store_dict_compatible_interface():
    from application.blueprint.services.bounded_stores import BoundedStore

    store = BoundedStore(max_size=3, ttl_seconds=3600)
    store["a"] = 1
    assert store["a"] == 1
    assert "a" in store
    store.delete("a")
    assert "a" not in store


# ── E7: gate 角色提及判定（引号实体/单字门槛） ───────────────────────────

def test_outline_mentions_quoted_entity_exact_match():
    from application.evolution.services.gate_service import _outline_mentions

    assert _outline_mentions("回忆《林澈》当年的约定", {"林澈"}) == "林澈"


def test_outline_mentions_single_char_requires_quotes():
    from application.evolution.services.gate_service import _outline_mentions

    # 单字名裸出现不命中（旧实现这里会误报 DEAD_CHARACTER_REAPPEARS）
    assert _outline_mentions("林间小路上走着一位老者", {"林"}) == ""
    assert _outline_mentions("「林」低声道", {"林"}) == "林"


def test_outline_mentions_multi_char_bare_match():
    from application.evolution.services.gate_service import _outline_mentions

    assert _outline_mentions("林澈推门而入", {"林澈"}) == "林澈"


# ── E10: flush_pending_writes 无队列时直连已落盘 ─────────────────────────

def test_flush_pending_writes_true_without_queue():
    from infrastructure.persistence.database.connection import DatabaseConnection

    db = DatabaseConnection.__new__(DatabaseConnection)
    # get_persistence_queue 返回 None（未初始化）→ 视为已落盘
    import application.engine.services.persistence_queue as pq_mod

    original = pq_mod.get_persistence_queue
    pq_mod.get_persistence_queue = lambda: None
    try:
        assert db.flush_pending_writes() is True
    finally:
        pq_mod.get_persistence_queue = original


# ── E11: UA 产品标识 + 无重复方法定义（源码契约） ────────────────────────

def test_anthropic_ua_is_product_identity():
    text = Path("infrastructure/ai/providers/anthropic_provider.py").read_text(encoding="utf-8")
    assert "claude-cli" not in text
    assert text.count("plotpilot/1.0") == 2  # 同步 + 流式两处


def test_openai_stream_chunk_extractor_defined_once():
    text = Path("infrastructure/ai/providers/openai_provider.py").read_text(encoding="utf-8")
    assert text.count("def _extract_text_from_stream_chunk") == 1
    assert "_remember_class_cache" in text  # 类级缓存走上限助手
