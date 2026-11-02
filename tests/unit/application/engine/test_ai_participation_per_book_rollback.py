# -*- coding: utf-8 -*-
"""第 83 批（N-09）：AI 参与总闸的**三级**回退（config / env / novel）。

病根（第 80 批独立验收 N-09）
----------------------------
规格 §3.2 承诺「novel 级 > env 级 > 默认级」，但那说的是**统一开关读取层**
（只覆盖 5 个 key / 3 个 feature）。而**总闸**（25 个 feature 的那把大闸）
此前**只有 config 一级** ⇒

* 「这台机器上回退」做不到（没有 env 级）；
* 「**按这本书**回退」做不到（没有 novel 级）⇒ 粒度只能一刀切（feature 级）。

⇒ 本批给总闸补上 **env 级**（部署级）与 **novel 级**（按这本书），
  并把「novel 级**只能关、不能开**」钉住（总闸是**回退路径**，不是开关）。
"""
from __future__ import annotations

import pytest

from application.engine.services import ai_participation_settings as settings_module
from application.engine.services.ai_participation_settings import (
    AI_PARTICIPATION_FEATURES,
    ENV_DISABLED_FEATURES,
    ENV_MASTER_ENABLED,
    ai_participation_enabled,
    get_ai_participation_runtime_settings,
)
from application.engine.services.ai_switch_layer import (
    current_novel_switches,
    reset_novel_switches,
    set_novel_switches,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """⚠ env 级是**部署级** ⇒ 每个用例都要从干净状态出发。"""
    monkeypatch.delenv(ENV_MASTER_ENABLED, raising=False)
    monkeypatch.delenv(ENV_DISABLED_FEATURES, raising=False)
    yield


def _config(monkeypatch, master: bool = True, disabled=()) -> None:
    monkeypatch.setattr(
        settings_module,
        "get_config",
        lambda: type(
            "_Cfg",
            (),
            {"ai_participation": {"master_enabled": master, "disabled_features": list(disabled)}},
        )(),
    )


# ── ① config 级（既有行为，必须保持） ────────────────────────────────


def test_the_config_level_still_works(monkeypatch):
    _config(monkeypatch, master=True, disabled=["chapter_mode"])

    assert ai_participation_enabled("chapter_mode") is False
    assert ai_participation_enabled("rhythm_quota") is True


def test_the_master_switch_still_kills_everything(monkeypatch):
    _config(monkeypatch, master=False)

    for feature in AI_PARTICIPATION_FEATURES:
        assert ai_participation_enabled(feature) is False


def test_an_unregistered_feature_id_is_still_refused(monkeypatch):
    """⚠ 不认识的 id 绝不默认放行（那是「登记漏了 ⇒ 总闸失效」的入口）。"""
    _config(monkeypatch, master=True)
    assert ai_participation_enabled("not_a_registered_feature") is False


# ── ② env 级（部署级回退，此前不存在） ────────────────────────────────


def test_the_env_level_can_kill_the_master_switch(monkeypatch):
    """⚠ 「这台机器上回退」：``PLOTPILOT_AI_MASTER_ENABLED=0``。"""
    _config(monkeypatch, master=True)
    monkeypatch.setenv(ENV_MASTER_ENABLED, "0")

    assert get_ai_participation_runtime_settings().master_enabled is False
    for feature in AI_PARTICIPATION_FEATURES:
        assert ai_participation_enabled(feature) is False


def test_the_env_level_can_disable_single_features(monkeypatch):
    """⚠ 逐条点名关闭（部署级）。"""
    _config(monkeypatch, master=True)
    monkeypatch.setenv(ENV_DISABLED_FEATURES, "chapter_mode, rhythm_quota")

    assert ai_participation_enabled("chapter_mode") is False
    assert ai_participation_enabled("rhythm_quota") is False
    assert ai_participation_enabled("context_weights") is True


def test_the_env_level_overrides_the_config_level(monkeypatch):
    """⚠ 优先级 **env > config**（与统一开关层同一口径）。"""
    _config(monkeypatch, master=False)
    monkeypatch.setenv(ENV_MASTER_ENABLED, "1")

    assert get_ai_participation_runtime_settings().master_enabled is True
    assert ai_participation_enabled("chapter_mode") is True


def test_an_unparsable_env_value_falls_through(monkeypatch):
    """⚠ 认不出来的 env 值 ⇒ **当没给**（不许猜）。"""
    _config(monkeypatch, master=True)
    monkeypatch.setenv(ENV_MASTER_ENABLED, "maybe")

    assert get_ai_participation_runtime_settings().master_enabled is True


# ── ③ novel 级（按这本书回退 —— 规格承诺的那一层） ─────────────────────


def test_a_novel_can_turn_a_feature_off(monkeypatch):
    """⚠ **按这本书**回退：这本书把某项能力关了。"""
    _config(monkeypatch, master=True)
    token = set_novel_switches({"chapter_mode": False})
    try:
        assert ai_participation_enabled("chapter_mode") is False
        assert ai_participation_enabled("rhythm_quota") is True
    finally:
        reset_novel_switches(token)


def test_the_novel_level_does_not_leak_to_the_next_book(monkeypatch):
    """⚠ A 书关了，B 书不受影响（按书隔离）。"""
    _config(monkeypatch, master=True)
    token = set_novel_switches({"chapter_mode": False})
    try:
        assert ai_participation_enabled("chapter_mode") is False
    finally:
        reset_novel_switches(token)

    assert current_novel_switches() is None
    assert ai_participation_enabled("chapter_mode") is True


def test_the_novel_level_cannot_turn_the_master_switch_back_on(monkeypatch):
    """⚠ novel 级**只能关，不能开** —— 总闸是回退路径，不是开关。"""
    _config(monkeypatch, master=False)
    token = set_novel_switches({"chapter_mode": True})
    try:
        assert ai_participation_enabled("chapter_mode") is False
    finally:
        reset_novel_switches(token)


def test_the_novel_level_cannot_override_the_env_level(monkeypatch):
    """⚠ env 关掉的 ⇒ 这本书也开不回来（env 是部署级回退）。"""
    _config(monkeypatch, master=True)
    monkeypatch.setenv(ENV_DISABLED_FEATURES, "chapter_mode")
    token = set_novel_switches({"chapter_mode": True})
    try:
        assert ai_participation_enabled("chapter_mode") is False
    finally:
        reset_novel_switches(token)


# ── ④ 键空间不许悄悄撞车 ─────────────────────────────────────────────


def test_the_feature_ids_only_overlap_the_switch_keys_on_purpose():
    """⚠ feature id（总闸粒度）与逐条开关 key（设置粒度）**可能重名**。

    ⚠ 重名的后果：novel 级设了那个键 ⇒ **两处都生效**。
    ⇒ 目前只允许「同一个能力」的重名（``voice_llm_mode``），
      新增别的重名 ⇒ 必须先想清楚语义 ⇒ 这里会报红。
    """
    from application.engine.services.ai_switch_layer import AI_SWITCH_KEYS

    overlap = set(AI_PARTICIPATION_FEATURES) & set(AI_SWITCH_KEYS)
    assert overlap == {"voice_llm_mode"}, (
        "feature id 与逐条开关 key 出现了新的重名（%s）⇒ "
        "novel 级设了它会**同时**关掉两处，语义必须先确认" % sorted(overlap)
    )
