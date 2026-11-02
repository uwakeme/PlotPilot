# -*- coding: utf-8 -*-
"""第 79 批守卫：`voice_llm_mode` 的 **novel 级**真的生效。

⚠ 之前够不到，原因是**两个**（只修一个不够）：
  ① ``use_llm_mode`` 是**构造时冻结**的普通属性 ⇒ 本书的覆盖没有落点；
  ② LLM 文风服务**只在启动时按 env 决定要不要建** ⇒ 就算开关解析对了，
     服务不存在也走不到 LLM 模式。

本文件钉住（行为级，不看源码形状）
----------------------------------
1. **novel 级**能把 ``use_llm_mode`` 打开（之前恒为构造时的值）；
2. **env 级**仍然有效（不能被 novel 级实现顺手弄坏）；
3. **默认级**（都没给）仍然是构造时的取值（零行为变化）；
4. novel 级打开 ⇒ 分派点真的走 **LLM 路径**（LLM 服务被调用）；
5. novel 级关闭 ⇒ 分派点走**统计路径**（LLM 服务**没被调用**）；
6. **懒创建**：统计模式下**启动时什么都不建**（不擅自开 LLM 调用链）；
7. 总闸关掉 ⇒ 即使 novel 级开着也**不走** LLM（闸门仍在分派点收口）。
"""
from __future__ import annotations

import pytest

from application.analyst.services.voice_drift_service import VoiceDriftService
from application.engine.services.ai_switch_layer import (
    reset_novel_switches,
    set_novel_switches,
)


class _FingerprintRepo:
    def get_by_novel(self, novel_id, pov_character_id=None):
        return None  # 没有指纹 ⇒ similarity 为 None ⇒ 不告警


class _ScoreRepo:
    def __init__(self):
        self.upserts: list[dict] = []

    def upsert(self, **kwargs):
        self.upserts.append(kwargs)

    def list_by_novel(self, novel_id, limit=10):
        return []


class _FakeLLMVoiceService:
    """记录自己被调用的假 LLM 文风服务。"""

    def __init__(self):
        self.calls = 0

    async def analyze_chapter_style(self, novel_id, chapter_number, content):
        self.calls += 1
        return {"adjective_density": 0.1, "avg_sentence_length": 20, "sentence_count": 5}

    def compute_drift_score(self, style_vector, baseline):
        return 0.9

    async def compute_baseline(self, novel_id, style_vectors):
        return {"adjective_density": 0.1, "avg_sentence_length": 20, "sentence_count": 5}

    def _compute_simple_baseline(self, style_vectors):
        return {"adjective_density": 0.1, "avg_sentence_length": 20, "sentence_count": 5}


def _service(use_llm_mode=False, llm_service=None, factory=None) -> VoiceDriftService:
    return VoiceDriftService(
        _ScoreRepo(),
        _FingerprintRepo(),
        llm_voice_service=llm_service,
        use_llm_mode=use_llm_mode,
        runtime_settings=None,
        llm_service_factory=factory,
    )


@pytest.fixture()
def no_env_switch(monkeypatch):
    """把 env 级清干净（只留 novel 级 / 默认级的判定）。"""
    monkeypatch.delenv("PLOTPILOT_AI_VOICE_LLM_MODE", raising=False)


# ─────────────────────── 1) novel 级真的能打开 ───────────────────────


def test_the_novel_level_can_turn_the_llm_mode_on(no_env_switch):
    """⚠ 本书把 ``voice_llm_mode`` 设成 true ⇒ 服务这边要**看得见**。"""
    service = _service(use_llm_mode=False)
    assert service.use_llm_mode is False, "默认级没给时不该是 LLM 模式"

    token = set_novel_switches({"voice_llm_mode": True})
    try:
        assert service.use_llm_mode is True, (
            "novel 级设了 voice_llm_mode=True，服务却读不到 ⇒ "
            "本书的覆盖**静默无效**"
        )
    finally:
        reset_novel_switches(token)

    assert service.use_llm_mode is False, "作用域结束后必须还原（不漏到下一章）"


def test_the_env_level_still_works(no_env_switch, monkeypatch):
    """⚠ 改了实现之后，env 级不能被顺手弄坏。"""
    monkeypatch.setenv("PLOTPILOT_AI_VOICE_LLM_MODE", "1")
    service = _service(use_llm_mode=False)
    assert service.use_llm_mode is True, "env 级失效了"


def test_the_default_level_is_unchanged(no_env_switch):
    """⚠ 零行为变化：都没给时就是构造时的取值。"""
    service = _service(use_llm_mode=True)
    assert service.use_llm_mode is True
    service2 = _service(use_llm_mode=False)
    assert service2.use_llm_mode is False


# ─────────────────────── 2) 分派点真的走对路 ───────────────────────


async def test_the_novel_level_makes_the_dispatcher_use_the_llm_path(no_env_switch):
    """⚠ 开关打开了 ⇒ 分派点必须真的走 LLM 路径（LLM 服务被调用）。"""
    fake = _FakeLLMVoiceService()
    service = _service(use_llm_mode=False, llm_service=fake)

    token = set_novel_switches({"voice_llm_mode": True})
    try:
        result = await service.score_chapter_async("n1", 1, "他嘴角勾起一丝冷笑。")
    finally:
        reset_novel_switches(token)

    assert fake.calls == 1, "novel 级开着却没走 LLM 路径（分数是统计口径）"
    assert result.get("mode") == "llm", result


async def test_without_the_novel_level_the_dispatcher_stays_on_statistics(no_env_switch):
    """⚠ 没开 ⇒ 统计路径 ⇒ **一个模型都不调**。"""
    fake = _FakeLLMVoiceService()
    service = _service(use_llm_mode=False, llm_service=fake)

    result = await service.score_chapter_async("n1", 1, "他嘴角勾起一丝冷笑。")

    assert fake.calls == 0, "统计模式却调了 LLM"
    assert result.get("mode") == "statistics", result


async def test_the_master_gate_still_blocks_the_llm_path(no_env_switch, monkeypatch):
    """⚠ **总闸**关掉 ⇒ 即使 novel 级开着也不走 LLM（闸门仍在分派点收口）。"""
    monkeypatch.setattr(
        "application.analyst.services.voice_drift_service.ai_participation_enabled",
        lambda feature: False,
    )
    fake = _FakeLLMVoiceService()
    service = _service(use_llm_mode=True, llm_service=fake)

    token = set_novel_switches({"voice_llm_mode": True})
    try:
        result = await service.score_chapter_async("n1", 1, "他嘴角勾起一丝冷笑。")
    finally:
        reset_novel_switches(token)

    assert fake.calls == 0, "总闸关着还打了模型 ⇒ 回退路径是假的"
    assert result.get("mode") == "statistics", result


# ─────────────────────── 3) 懒创建 ───────────────────────


def test_the_llm_service_is_not_created_until_it_is_needed(no_env_switch):
    """⚠ 统计模式下**启动时什么都不建**（不擅自开 LLM 调用链）。"""
    created = []

    def _factory():
        created.append(1)
        return _FakeLLMVoiceService()

    service = _service(use_llm_mode=False, factory=_factory)
    assert created == [], "构造时就建了 LLM 服务 ⇒ 统计模式也在白建"
    assert service.llm_voice_service is None


async def test_the_llm_service_is_created_on_demand(no_env_switch):
    """⚠ 真要跑 LLM 模式时才建（懒创建 ⇒ 之后就能用）。"""
    created = []

    def _factory():
        created.append(1)
        return _FakeLLMVoiceService()

    service = _service(use_llm_mode=False, factory=_factory)

    token = set_novel_switches({"voice_llm_mode": True})
    try:
        await service.score_chapter_async("n1", 1, "他嘴角勾起一丝冷笑。")
    finally:
        reset_novel_switches(token)

    assert created == [1], "要走 LLM 模式时却没建服务"
    assert service.llm_voice_service is not None
