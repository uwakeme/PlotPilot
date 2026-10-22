"""X-02：AI 参与能力的**总闸**（模型异常 / 成本失控时的回退路径）。

背景：此前只有少数几项「AI 参与」能力各自带开关
（``chapter_mode`` / ``chapter_target`` / ``context_weights`` / 声音分析的 LLM 模式），
上一批新增的 AI 调用点**一个开关都没有** ⇒ 出问题只能回滚代码。

本文件的守卫钉住总闸的三条性质：

1. **默认不改变今天的行为**（``master_enabled`` 默认 True）；
2. **总闸优先于逐条开关**（总闸关闭 ⇒ 逐条开关即使写着 enabled 也不生效）；
3. **登记了就要真的接线**（AST 扫描：登记在册的每一项都必须有调用点读它，
   否则「回退路径」是假的 —— 登记了却没人读，总闸照样关不掉它）。
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.engine.services.ai_participation_settings import (
    AI_PARTICIPATION_FEATURES,
    ai_participation_enabled,
    get_ai_participation_runtime_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
SCAN_DIRS = ("application", "engine", "interfaces", "infrastructure", "domain")


def _cfg(**sections) -> SimpleNamespace:
    return SimpleNamespace(**{k: dict(v) for k, v in sections.items()})


# ─────────────────────────── 总闸本身 ───────────────────────────


def test_master_switch_defaults_to_on(monkeypatch):
    """默认开着 ⇒ 默认行为与今天逐位一致（总闸不是新功能，是回退路径）。"""
    monkeypatch.setattr(
        "application.engine.services.ai_participation_settings.get_config",
        lambda: _cfg(ai_participation={}),
    )

    settings = get_ai_participation_runtime_settings()

    assert settings.master_enabled is True
    assert settings.disabled_features == ()
    for feature in AI_PARTICIPATION_FEATURES:
        assert ai_participation_enabled(feature) is True


def test_master_switch_off_disables_every_registered_feature(monkeypatch):
    """总闸关闭 ⇒ 登记在册的能力**全部**不许发起模型调用。"""
    monkeypatch.setattr(
        "application.engine.services.ai_participation_settings.get_config",
        lambda: _cfg(ai_participation={"master_enabled": False}),
    )

    for feature in AI_PARTICIPATION_FEATURES:
        assert ai_participation_enabled(feature) is False


def test_disabled_features_can_turn_off_one_feature(monkeypatch):
    """逐条点名：总闸开着时也能单独关某一项。"""
    monkeypatch.setattr(
        "application.engine.services.ai_participation_settings.get_config",
        lambda: _cfg(ai_participation={"disabled_features": ["chapter_mode"]}),
    )

    assert ai_participation_enabled("chapter_mode") is False
    assert ai_participation_enabled("chapter_target") is True


def test_unregistered_feature_is_refused(monkeypatch):
    """**没登记**的 feature id 一律拒绝。

    不认识的名字绝不默认放行 —— 那是「登记漏了 ⇒ 总闸失效」的入口。
    """
    monkeypatch.setattr(
        "application.engine.services.ai_participation_settings.get_config",
        lambda: _cfg(ai_participation={"master_enabled": True}),
    )

    assert ai_participation_enabled("没登记过的能力") is False
    assert ai_participation_enabled("") is False


def test_master_switch_reads_the_config_section():
    """配置段真的存在（不是只写在注释里）。"""
    import yaml

    raw = (REPO_ROOT / "config" / "performance.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(raw)

    section = data.get("ai_participation")
    assert isinstance(section, dict), "config/performance.yaml 缺 ai_participation 段"
    assert "master_enabled" in section
    assert "disabled_features" in section


# ─────────────── 总闸优先于逐条开关（逐条开关要真的读总闸） ───────────────


@pytest.mark.parametrize(
    "module_path, feature, section_name",
    [
        (
            "application.engine.services.chapter_mode_settings",
            "chapter_mode",
            "chapter_mode",
        ),
        (
            "application.engine.services.chapter_target_settings",
            "chapter_target",
            "chapter_target",
        ),
        (
            "application.engine.services.context_weight_settings",
            "context_weights",
            "context_weights",
        ),
    ],
)
def test_per_feature_switch_respects_the_master_switch(
    monkeypatch, module_path, feature, section_name
):
    """逐条开关写着 enabled: true，但总闸关闭 ⇒ 仍然不许调用。"""
    import importlib

    module = importlib.import_module(module_path)
    getter = next(
        getattr(module, name)
        for name in dir(module)
        if name.startswith("get_") and name.endswith("_runtime_settings")
    )

    # 逐条开关开着
    monkeypatch.setattr(
        module, "get_config", lambda: _cfg(**{section_name: {"enabled": True}})
    )
    monkeypatch.setattr(
        "application.engine.services.ai_participation_settings.get_config",
        lambda: _cfg(ai_participation={"master_enabled": True}),
    )
    assert getter().enabled is True, "前置条件：逐条开关本身是开的"

    # 总闸关闭（总闸在**另一个模块**里读配置 ⇒ 两处都要打桩）
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: _cfg(
            **{section_name: {"enabled": True}},
            ai_participation={"master_enabled": False},
        ),
    )
    monkeypatch.setattr(
        "application.engine.services.ai_participation_settings.get_config",
        lambda: _cfg(ai_participation={"master_enabled": False}),
    )
    assert getter().enabled is False, "总闸关闭时逐条开关必须失效"


# ─────────────── 登记了就要真的接线（AST 扫描） ───────────────


def test_every_registered_feature_is_actually_wired():
    """登记在册的每一项都必须**真的**有调用点读它。

    否则「登记了却没人读」⇒ 总闸关不掉它 ⇒ 回退路径是假的。
    """
    wired: set[str] = set()
    for dirname in SCAN_DIRS:
        for path in (REPO_ROOT / dirname).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name != "ai_participation_enabled":
                    continue
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    wired.add(node.args[0].value)

    assert wired == set(AI_PARTICIPATION_FEATURES), (
        "登记在册的 AI 参与能力与实际接线的不一致："
        f"登记={sorted(AI_PARTICIPATION_FEATURES)} 接线={sorted(wired)}"
    )
