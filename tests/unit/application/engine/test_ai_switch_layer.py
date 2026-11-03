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
    derive_env_name,
    env_switch_name,
    parse_env_bool,
    reset_novel_switches,
    resolve_switch,
    set_novel_switches,
)


# ─────────────────────── 1) 三级优先级 ───────────────────────


def test_the_novel_level_wins_over_env_and_default(monkeypatch):
    monkeypatch.setenv("PLOTPILOT_AI_CHAPTER_MODE_ENABLED", "0")
    resolved = resolve_switch(
        "chapter_mode.enabled",
        default=False,
        novel_switches={"chapter_mode.enabled": True},
    )
    assert resolved is True, "novel 级没有压过 env 级"


def test_the_env_level_wins_over_the_default(monkeypatch):
    monkeypatch.setenv("PLOTPILOT_AI_CHAPTER_MODE_ENABLED", "1")
    assert resolve_switch("chapter_mode.enabled", default=False) is True
    monkeypatch.setenv("PLOTPILOT_AI_CHAPTER_MODE_ENABLED", "0")
    assert resolve_switch("chapter_mode.enabled", default=True) is False


def test_the_default_level_is_used_when_nothing_is_set(monkeypatch):
    """⚠ **零行为变化**：novel / env 都没给 ⇒ 就是默认级。"""
    monkeypatch.delenv("PLOTPILOT_AI_CHAPTER_MODE_ENABLED", raising=False)
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
    monkeypatch.setenv("PLOTPILOT_AI_CHAPTER_MODE_ENABLED", raw)
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

    assert get_chapter_mode_runtime_settings().enabled is True, (
        "第 89 批：用户拍板打开 D-01 ⇒ 运行时默认应跟着配置走"
    )

    settings = get_chapter_mode_runtime_settings(
        novel_switches={"chapter_mode.enabled": True, "chapter_mode.apply": True}
    )
    assert settings.enabled is True
    assert settings.apply is True


def test_the_context_weight_settings_honor_the_novel_level(monkeypatch):
    """⚠ 逐条开关必须**真的**走读取层（否则这层就是装饰品）。

    ⚠ 第 81 批：D-02 已灰度打开（config 级 = 开）⇒ 要证明「novel 级压过 config 级」，
      novel 覆盖必须与 config 取**相反极性**
      （两边都是 True ⇒ 断言恒真 ⇒ 这条守卫会变成空洞守卫）。
    """
    from application.engine.services.context_weight_settings import (
        get_context_weight_runtime_settings,
    )

    # 部署环境可能配过 env 级 ⇒ 别让它干扰「config 级 vs novel 级」这条断言
    monkeypatch.delenv("PLOTPILOT_AI_CONTEXT_WEIGHTS_ENABLED", raising=False)
    monkeypatch.delenv("PLOTPILOT_AI_CONTEXT_WEIGHTS_SHADOW", raising=False)

    # config 级：读出来必须**就是配置里写的**（读不到 ⇒ 配置与生产脱节）
    # ⚠ 灰度状态本身由 tests/unit/test_ai_participation_grayscale_state.py 钉住；
    #   这里**不写死**当前是开是关（否则关灰度会变成一条看不懂的失败）。
    config_level = get_context_weight_runtime_settings()

    # novel 级：覆盖值取 config 级的**相反极性** ⇒ 断言有鉴别力
    # （两边同极性 ⇒ 断言恒真 ⇒ 这条守卫会退化成空洞守卫）。
    novel_enabled = not config_level.enabled
    novel_shadow = not config_level.shadow
    settings = get_context_weight_runtime_settings(
        novel_switches={
            "context_weights.enabled": novel_enabled,
            "context_weights.shadow": novel_shadow,
        }
    )
    assert settings.enabled is novel_enabled, "novel 级没有压过 config 级"
    assert settings.shadow is novel_shadow, "novel 级没有压过 config 级"


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
    """⚠ env 变量名钉死（部署脚本按这个名字配）。

    ⚠ 第 76 批（S-5'）：规格 §3.2 写的是 ``PLOTPILOT_AI_<KEY>``
      ⇒ **带点的键**必须把点也变成下划线。
      ⇒ 之前手写的那张表写成了 ``PLOTPILOT_AI_CHAPTER_MODE``
      ⇒ **按规格配静默不生效**（而且那个名字正好撞上未登记父键的推导名）。
    """
    assert env_switch_name("chapter_mode.enabled") == "PLOTPILOT_AI_CHAPTER_MODE_ENABLED"
    assert env_switch_name("chapter_mode.apply") == "PLOTPILOT_AI_CHAPTER_MODE_APPLY"
    assert env_switch_name("context_weights.enabled") == "PLOTPILOT_AI_CONTEXT_WEIGHTS_ENABLED"
    assert env_switch_name("context_weights.shadow") == "PLOTPILOT_AI_CONTEXT_WEIGHTS_SHADOW"
    assert env_switch_name("voice_llm_mode") == "PLOTPILOT_AI_VOICE_LLM_MODE"
    # 未登记的 key ⇒ 按统一命名规则推导（不报错，但名字有规律）
    assert env_switch_name("some.new_key") == "PLOTPILOT_AI_SOME_NEW_KEY"


def test_the_env_names_are_derived_from_the_keys_not_written_by_hand():
    """⚠ env 名**由键推导** ⇒ 不可能「登记表和代码各写一份」而悄悄漂移。"""
    for key, name in AI_SWITCH_KEYS.items():
        assert name == derive_env_name(key), key
        assert name.startswith("PLOTPILOT_AI_"), key


def test_the_env_names_are_unique():
    """⚠ 两个键推出同一个 env 名 ⇒ 后一个会**抢走**前一个的开关。"""
    names = list(AI_SWITCH_KEYS.values())
    assert len(names) == len(set(names)), names


def test_a_registered_key_does_not_collide_with_its_parent_key():
    """⚠ 父键（``chapter_mode``）的推导名**不许**等于子键（``chapter_mode.enabled``）。

    ⚠ 这正是上一版手写表的病根：``chapter_mode.enabled`` 叫
    ``PLOTPILOT_AI_CHAPTER_MODE`` ⇒ 正好是父键 ``chapter_mode`` 的推导名
    ⇒ 未登记的父键会抢走已登记子键的 env。
    """
    for key in AI_SWITCH_KEYS:
        parent = key.split(".")[0]
        if parent == key:
            continue
        assert env_switch_name(parent) != env_switch_name(key), key


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


# ─────────────────────── 7) 第 76 批：旧别名 / 未登记出声 / novel 级够不到出声 ───────────────────────


def test_the_legacy_env_name_still_works_but_warns(monkeypatch, caplog):
    """⚠ 旧名仍然生效（**不静默失效**），但必须提醒改名。

    ⚠ 为什么要留旧名：改名字会让**已经在部署环境配好的**变量悄悄失效
      ⇒ 「配了不生效」是最坏的失败方式。
    """
    import logging

    from application.engine.services.ai_switch_layer import LEGACY_ENV_NAMES

    legacy = "PLOTPILOT_AI_CHAPTER_MODE"
    assert legacy in LEGACY_ENV_NAMES, "旧名必须登记在册（否则改了名就静默失效）"

    monkeypatch.setenv(legacy, "1")
    with caplog.at_level(logging.WARNING):
        resolved = resolve_switch("chapter_mode.enabled", default=False)
    assert resolved is True, "旧名仍然要生效"
    assert any("已废弃" in rec.getMessage() for rec in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_the_canonical_env_name_wins_over_the_legacy_one(monkeypatch):
    """⚠ 新旧同时配 ⇒ 以**新名**为准（改名后旧名不该继续抢）。"""
    monkeypatch.setenv("PLOTPILOT_AI_CHAPTER_MODE_ENABLED", "1")
    monkeypatch.setenv("PLOTPILOT_AI_CHAPTER_MODE", "0")
    assert resolve_switch("chapter_mode.enabled", default=False) is True


def test_an_unregistered_env_switch_is_reported(monkeypatch, caplog):
    """⚠ 配了**未登记**的开关 ⇒ 必须出声（之前完全静默 ⇒ 配了不生效）。"""
    import logging

    from application.engine.services.ai_switch_layer import audit_env_switches

    monkeypatch.setenv("PLOTPILOT_AI_NOT_A_REAL_SWITCH", "1")
    monkeypatch.setenv("PLOTPILOT_AI_CHAPTER_MODE_ENABLED", "1")
    with caplog.at_level(logging.WARNING):
        warnings = audit_env_switches()

    assert any("PLOTPILOT_AI_NOT_A_REAL_SWITCH" in w for w in warnings), warnings
    assert not any("PLOTPILOT_AI_CHAPTER_MODE_ENABLED" in w for w in warnings), warnings


def test_the_env_audit_is_wired_into_the_startup_paths():
    """⚠ 「出声」要有出口 ⇒ 启动路径必须真的调用体检。

    ⚠ 只测函数不测接线 ⇒ 等于没测（审计函数没人调 = 没人出声）。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[4]
    daemon = (root / "scripts" / "start_daemon.py").read_text(encoding="utf-8")
    api = (root / "interfaces" / "main.py").read_text(encoding="utf-8")
    assert "audit_env_switches()" in daemon, "守护进程启动没体检 AI 开关环境变量"
    assert "audit_env_switches()" in api, "API 启动没体检 AI 开关环境变量"


def test_a_novel_level_switch_that_cannot_be_reached_warns():
    """⚠ 「这本书」的覆盖在进程级单例那儿**没有落点** ⇒ 设了要出声。

    ⚠ 之前是**静默无效**（用户以为配了，其实在跑 env/默认级）。
    """
    from application.engine.services.ai_switch_layer import (
        NOVEL_UNREACHABLE_KEYS,
        warn_unreachable_novel_switches,
    )

    warned = warn_unreachable_novel_switches({"voice_llm_mode": True}, novel_id="novel-x")
    # ⚠ 第 79 批：``voice_llm_mode`` **够得到**了（文风服务改成每次读都解析
    #   + LLM 服务懒创建）⇒ 不在「够不到」清单里 ⇒ 不该再喊「无效」。
    assert warned == [], "voice_llm_mode 现在生效了 ⇒ 不该再被当成『无效』"

    # ⚠ 能到的键不许误报
    assert warn_unreachable_novel_switches(
        {"chapter_mode.enabled": True}, novel_id="novel-x"
    ) == []
    assert warn_unreachable_novel_switches(None) == []
    assert warn_unreachable_novel_switches({}) == []


def test_the_unreachable_warning_is_wired_into_the_novel_processing():
    """⚠ 出声要有出口 ⇒ 设 novel 覆盖的地方必须真的喊。"""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[4]
    lifecycle = (root / "engine" / "runtime" / "novel_lifecycle.py").read_text(encoding="utf-8")
    writing = (root / "engine" / "runtime" / "writing_delegate.py").read_text(encoding="utf-8")
    assert "warn_unreachable_novel_switches(" in lifecycle, "整本小说的处理没喊"
    assert "warn_unreachable_novel_switches(" in writing, "写作 delegate 没喊"


def test_the_novel_switch_scope_sets_and_restores():
    """⚠ 作用域必须**用完自动还原**（不漏到下一章 / 下一本书）。"""
    from application.engine.services.ai_switch_layer import (
        novel_switch_scope,
        current_novel_switches,
    )

    assert current_novel_switches() is None
    with novel_switch_scope({"chapter_mode.enabled": True}):
        assert current_novel_switches() == {"chapter_mode.enabled": True}
        assert resolve_switch("chapter_mode.enabled", default=False) is True
    assert current_novel_switches() is None
    assert resolve_switch("chapter_mode.enabled", default=False) is False


def test_the_novel_switch_scope_is_the_only_place_that_sets_the_contextvar():
    """⚠ 同一逻辑写两份 ⇒ 迟早有一处漏还原 ⇒ 开关会漏到别的书。

    ⚠ 只允许开关层自己 set/reset（context manager 里）。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[4]
    offenders = []
    for rel in ("engine", "application", "interfaces", "scripts"):
        for path in (root / rel).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "ai_switch_layer" in text and "set_novel_switches(" in text:
                if "services/ai_switch_layer.py" in path.as_posix():
                    continue
                offenders.append(path.relative_to(root).as_posix())
    assert not offenders, "这些地方自己 set/reset 了 contextvar ⇒ 统一走 novel_switch_scope：%s" % offenders


async def test_the_legacy_writing_path_runs_inside_the_novel_switch_scope(monkeypatch):
    """⚠ 第 76 批（U-3'）：legacy 写作路径也必须在「这本书的开关」作用域内。

    ⚠ 以前只有 **StoryPipeline** 那条路设了 contextvar（``writing_delegate``）
      ⇒ legacy 路径（``PLOTPILOT_USE_STORY_PIPELINE=off`` 的回退路径）
      的 novel 级开关**静默无效**。
    ⇒ 作用域提到 ``process_novel`` 这一层 ⇒ 两条写作路径都在里面。
    """
    from types import SimpleNamespace

    from application.engine.services.ai_switch_layer import current_novel_switches
    from domain.novel.entities.novel import AutopilotStatus, NovelStage
    import engine.runtime.novel_lifecycle as lifecycle
    import engine.runtime.writing_delegate as writing_delegate

    seen: dict = {}

    async def _fake_run_writing(host, novel):
        seen["switches"] = current_novel_switches()

    monkeypatch.setattr(writing_delegate, "run_writing", _fake_run_writing)
    # 恢复策略不参与（它会改阶段 ⇒ 走不到写作）
    monkeypatch.setattr(
        "application.engine.services.autopilot_recovery_policy."
        "AutopilotRecoveryPolicy.decide_on_daemon_tick",
        lambda self, novel: SimpleNamespace(
            next_stage=None, reason="", clear_pending_invocation=False
        ),
    )
    monkeypatch.setattr(
        "application.engine.services.autopilot_recovery_policy."
        "AutopilotRecoveryPolicy.apply_transient_cleanup",
        lambda self, decision: None,
    )

    novel = SimpleNamespace(
        novel_id=SimpleNamespace(value="novel-scope-x"),
        current_stage=NovelStage.WRITING,
        current_act=1,
        autopilot_status=AutopilotStatus.RUNNING,
        generation_prefs=SimpleNamespace(ai_switches={"chapter_mode.enabled": True}),
        consecutive_error_count=0,
        target_chapters=10,
    )
    host = SimpleNamespace(
        _is_still_running=lambda n: True,
        _update_shared_state=lambda *a, **k: None,
        _save_novel_state=lambda n: None,
        _read_autopilot_status_ephemeral=lambda nid: AutopilotStatus.RUNNING,
        _merge_autopilot_status_from_db=lambda n: None,
        circuit_breaker=None,
    )

    await lifecycle.process_novel(host, novel)

    assert "switches" in seen, "process_novel 没走到写作阶段（守卫前提失效）"
    assert seen["switches"] == {"chapter_mode.enabled": True}, (
        "写作阶段拿不到「这本书的开关覆盖」⇒ novel 级静默失效"
    )
