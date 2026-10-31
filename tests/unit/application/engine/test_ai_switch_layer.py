"""第 74 批守卫：Q-1 —— 统一开关读取层（novel 级 > env 级 > 默认级）。

来源：设计规格 §3.2「开关怎么设计」：

    优先级：novel.generation_prefs.<key>  >  env  >  硬编码默认
    第 1 批（13 条）+ 第 2 批（17 条）共 30 条共用这个读取层。
    **不给每个条目单独造配置项**

⚠ 之前只有 ``config/performance.yaml`` 一层 ⇒ env 与 novel 两级**都不存在**
  ⇒ 「按部署回退」「按这本书回退」两件事都做不到 ⇒ 验收 3 判不了「通过」。

本文件钉住
----------
1. **三级优先级**（novel > env > 默认）；
2. **零行为变化**：novel / env 都没给时，返回的就是默认级；
3. env 值**认不出来就落回**（不许猜）；
4. novel 级**按章隔离**（不会漏到下一章 / 下一本书）；
5. 逐条开关**真的**走了这个读取层；
6. 开关键**登记在册**（不许出现没登记的键）。
"""
from __future__ import annotations

import pytest

from application.engine.services.ai_switch_layer import (
    AI_SWITCH_KEYS,
    current_novel_switches,
    env_switch_name,
    parse_env_bool,
    reset_novel_switches,
    resolve_switch,
    set_novel_switches,
)


# ─────────────────────── 1) 三级优先级 ───────────────────────


def test_the_novel_level_wins_over_env_and_default(monkeypatch):
    monkeypatch.setenv("PLOTPILOT_AI_CHAPTER_MODE", "0")
    resolved = resolve_switch(
        "chapter_mode.enabled",
        default=False,
        novel_switches={"chapter_mode.enabled": True},
    )
    assert resolved is True, "novel 级没有压过 env 级"


def test_the_env_level_wins_over_the_default(monkeypatch):
    monkeypatch.setenv("PLOTPILOT_AI_CHAPTER_MODE", "1")
    assert resolve_switch("chapter_mode.enabled", default=False) is True
    monkeypatch.setenv("PLOTPILOT_AI_CHAPTER_MODE", "0")
    assert resolve_switch("chapter_mode.enabled", default=True) is False


def test_the_default_level_is_used_when_nothing_is_set(monkeypatch):
    """⚠ **零行为变化**：novel / env 都没给 ⇒ 就是默认级。"""
    monkeypatch.delenv("PLOTPILOT_AI_CHAPTER_MODE", raising=False)
    assert resolve_switch("chapter_mode.enabled", default=False) is False
    assert resolve_switch("chapter_mode.enabled", default=True) is True


def test_the_contextvar_is_used_when_no_explicit_mapping_is_given():
    """⚠ 不显式传映射时，读 contextvar 里「当前这本书」的覆盖。"""
    token = set_novel_switches({"context_weights.enabled": True})
    try:
        assert resolve_switch("context_weights.enabled", default=False) is True
        assert current_novel_switches() == {"context_weights.enabled": True}
    finally:
        reset_novel_switches(token)


# ─────────────────────── 2) env 认不出来就落回（不许猜） ───────────────────────


@pytest.mark.parametrize("raw", ["", "maybe", "2", "开", "enabled please"])
def test_an_unparsable_env_value_falls_through(raw, monkeypatch):
    """⚠ 认不出来的 env 值 ⇒ **当没给**（落回默认级），不许猜。"""
    monkeypatch.setenv("PLOTPILOT_AI_CHAPTER_MODE", raw)
    assert resolve_switch("chapter_mode.enabled", default=True) is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True), ("true", True), ("YES", True), ("on", True),
        ("0", False), ("false", False), ("no", False), ("off", False),
    ],
)
def test_the_env_words_are_parsed(raw, expected):
    assert parse_env_bool(raw) is expected


# ─────────────────────── 3) novel 级按章隔离 ───────────────────────


def test_the_novel_switches_do_not_leak_to_the_next_book():
    """⚠ 还原后必须**读不到**上一本书的覆盖（否则 A 书的开关会漏到 B 书）。"""
    token = set_novel_switches({"chapter_mode.enabled": True})
    try:
        assert resolve_switch("chapter_mode.enabled", default=False) is True
    finally:
        reset_novel_switches(token)

    assert current_novel_switches() is None, "还原后 contextvar 里还留着上一本的覆盖"
    assert resolve_switch("chapter_mode.enabled", default=False) is False


def test_setting_none_clears_the_novel_level():
    token = set_novel_switches({"chapter_mode.enabled": True})
    try:
        token2 = set_novel_switches(None)
        try:
            assert resolve_switch("chapter_mode.enabled", default=False) is False
        finally:
            reset_novel_switches(token2)
    finally:
        reset_novel_switches(token)


# ─────────────────────── 4) 逐条开关真的走了这个读取层 ───────────────────────


def test_the_chapter_mode_settings_honor_the_novel_level():
    """⚠ 逐条开关必须**真的**走读取层（否则这层就是装饰品）。"""
    from application.engine.services.chapter_mode_settings import (
        get_chapter_mode_runtime_settings,
    )

    assert get_chapter_mode_runtime_settings().enabled is False, "默认必须仍是关闭"

    settings = get_chapter_mode_runtime_settings(
        novel_switches={"chapter_mode.enabled": True, "chapter_mode.apply": True}
    )
    assert settings.enabled is True
    assert settings.apply is True


def test_the_context_weight_settings_honor_the_novel_level():
    from application.engine.services.context_weight_settings import (
        get_context_weight_runtime_settings,
    )

    assert get_context_weight_runtime_settings().enabled is False

    settings = get_context_weight_runtime_settings(
        novel_switches={"context_weights.enabled": True, "context_weights.shadow": False}
    )
    assert settings.enabled is True
    assert settings.shadow is False


def test_the_voice_settings_honor_the_env_level(monkeypatch):
    """⚠ 文风这一项的 novel 级够不到（服务是进程级单例）⇒ **env 级**必须可用。"""
    from application.analyst.services.voice_analysis_settings import (
        get_voice_analysis_runtime_settings,
    )

    monkeypatch.setenv("PLOTPILOT_AI_VOICE_LLM_MODE", "1")
    assert get_voice_analysis_runtime_settings().use_llm_mode is True

    monkeypatch.setenv("PLOTPILOT_AI_VOICE_LLM_MODE", "0")
    assert get_voice_analysis_runtime_settings().use_llm_mode is False


# ─────────────────────── 5) 开关键登记在册 ───────────────────────


def test_every_switch_key_in_use_is_registered():
    """⚠ 用到的开关 key 必须**登记在册**（⇒ env 变量名不会各写一份）。"""
    from application.analyst.services.voice_analysis_settings import (
        get_voice_analysis_runtime_settings,
    )

    assert "chapter_mode.enabled" in AI_SWITCH_KEYS
    assert "chapter_mode.apply" in AI_SWITCH_KEYS
    assert "context_weights.enabled" in AI_SWITCH_KEYS
    assert "context_weights.shadow" in AI_SWITCH_KEYS
    assert "voice_llm_mode" in AI_SWITCH_KEYS


def test_the_env_names_are_the_documented_ones():
    """⚠ env 变量名钉死（部署脚本按这个名字配）。"""
    assert env_switch_name("chapter_mode.enabled") == "PLOTPILOT_AI_CHAPTER_MODE"
    assert env_switch_name("voice_llm_mode") == "PLOTPILOT_AI_VOICE_LLM_MODE"
    # 未登记的 key ⇒ 按统一命名规则推导（不报错，但名字有规律）
    assert env_switch_name("some.new_key") == "PLOTPILOT_AI_SOME_NEW_KEY"


# ─────────────────────── 6) 领域层：只存布尔 ───────────────────────


def test_the_domain_only_keeps_boolean_switches():
    """⚠ novel 级覆盖只存**布尔**（其它类型一律丢掉 ⇒ 不给「半个值」留位置）。"""
    from domain.novel.value_objects.generation_preferences import GenerationPreferences

    prefs = GenerationPreferences.from_dict(
        {
            "ai_switches": {
                "chapter_mode.enabled": True,
                "context_weights.enabled": "yes",     # 字符串 ⇒ 丢
                "voice_llm_mode": 1,                   # int ⇒ 丢（bool 是 int 的子类，要用 isinstance(bool) 判）
            }
        }
    )

    assert prefs.ai_switches == {"chapter_mode.enabled": True}, prefs.ai_switches


def test_the_domain_round_trips_the_switches():
    from domain.novel.value_objects.generation_preferences import GenerationPreferences

    prefs = GenerationPreferences(ai_switches={"chapter_mode.enabled": True})
    restored = GenerationPreferences.from_json(prefs.to_dict() and __import__("json").dumps(prefs.to_dict()))

    assert restored.ai_switches == {"chapter_mode.enabled": True}
