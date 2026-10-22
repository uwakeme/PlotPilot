"""E-07：正文采样参数单一来源 + 生产提示词不再自相矛盾。

改造前
------
1. ``temperature=0.85`` 在 **5 个正文生成点**写死
   （``engine/pipeline/prose_composer.py`` ×2 / ``engine/runtime/daemon_host.py`` ×1 /
   ``engine/runtime/legacy_writing_delegate.py`` ×2）⇒ 改一次要改 5 处；
2. 生产正文提示词 ``chapter-prose-generation/system.md:10`` 写着
   「短剧短句行文：**全程使用超短段落**」，
   而**同一份生产上下文**里还有（``rule_parser.build_behavior_protocol_block``，
   经 ``context_budget_allocator`` 注入）「全章独段比例**不得超过 15%**」
   ⇒ 两条法则**直接矛盾**，且前者优先级最高。

改造后
------
· 温度的**唯一来源** = ``config/performance.yaml`` 的 ``prose_sampling:`` 段
  （默认 0.85 ⇒ 行为逐位不变）；
· 生产提示词那条改成不与 15% 法则冲突的措辞（长短段交替）；
  15% 那个口径**保留**（它在 9 处、且真的进了生产上下文）。

⚠ **明确不在本笔范围内**：``auto_novel_generation_workflow`` 里
``GenerationConfig()`` 用的是 domain 类的默认 temperature=**1.0**
（两个 ``GenerationConfig`` 类的默认值本来就不同）—— 那是**改造前就有**的
路径差异，改它就是行为变更 ⇒ 只记一笔，不在这里悄悄统一。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PROSE_SITES = (
    "engine/pipeline/prose_composer.py",
    "engine/runtime/daemon_host.py",
    "engine/runtime/legacy_writing_delegate.py",
)
PROSE_PROMPT = "infrastructure/ai/prompt_packages/nodes/chapter-prose-generation/system.md"
RULE_PARSER = "application/engine/rules/rule_parser.py"


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_bytes().decode("utf-8")


# ======================================================================
#  A. 单一来源
# ======================================================================


def test_the_config_section_exists_with_the_previous_values():
    """配置段存在，且默认值 = 改造前的值（行为逐位不变）。"""
    import yaml

    data = yaml.safe_load(_read("config/performance.yaml"))
    section = data.get("prose_sampling")

    assert isinstance(section, dict), "config/performance.yaml 缺 prose_sampling 段"
    assert section["temperature"] == 0.85
    assert section["top_p"] == 0.95


def test_the_default_matches_the_previous_behaviour():
    """读不到配置时回落的默认值 = 改造前的 0.85。"""
    from application.engine.services.prose_sampling_settings import (
        DEFAULT_PROSE_TEMPERATURE,
        ProseSamplingSettings,
    )

    assert DEFAULT_PROSE_TEMPERATURE == 0.85
    assert ProseSamplingSettings().temperature == 0.85


def test_no_production_site_hardcodes_the_prose_temperature():
    """生产正文生成点不得再写死 **0.85**（AST 判数值常量，不判文本）。

    ⚠ 判据只针对 **0.85**（正文温度那个值）：``daemon_host.py`` 里还有一处
    ``temperature=0.35``，那是**声线重写**（E-03 的范围），不是正文温度
    ⇒ 「这三个文件里不得有任何硬编码 temperature」判据过宽，会误伤。
    """
    offenders = []
    for rel in PROSE_SITES:
        tree = ast.parse(_read(rel))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "GenerationConfig":
                continue
            for kw in node.keywords:
                if kw.arg != "temperature":
                    continue
                value = kw.value
                if isinstance(value, ast.Constant) and value.value == 0.85:
                    offenders.append(f"{rel}:L{node.lineno} temperature=0.85")
    assert not offenders, "正文生成点还写死了温度：" + "；".join(offenders)


def test_the_configured_value_is_actually_used(monkeypatch):
    """改了配置就要生效（否则「可配置化」是假的）。"""
    import application.engine.services.prose_sampling_settings as mod

    class _Section(dict):
        pass

    class _Cfg:
        prose_sampling = {"temperature": 0.42, "top_p": 0.8}

    monkeypatch.setattr(mod, "get_config", lambda: _Cfg())

    settings = mod.get_prose_sampling_settings()

    assert settings.temperature == 0.42
    assert settings.top_p == 0.8
    assert mod.prose_temperature() == 0.42


#: 每个文件里「读单一来源」的调用点个数（正文生成点一共 5 处）
EXPECTED_SAMPLING_CALLS = {
    "engine/pipeline/prose_composer.py": 2,
    "engine/runtime/daemon_host.py": 1,
    "engine/runtime/legacy_writing_delegate.py": 2,
}


def test_every_prose_site_reads_the_single_source():
    """**每一个**正文生成点都必须读那个单一来源（按文件钉住个数）。

    ⚠ 只判「这个文件里出现过 prose_temperature」是不够的 ——
    5 个正文生成点里**少改一个**照样绿（变异 M5 就这么漏过去的）。
    """
    for rel, expected in EXPECTED_SAMPLING_CALLS.items():
        tree = ast.parse(_read(rel))
        count = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name == "prose_temperature":
                count += 1
        assert count == expected, (
            f"{rel} 里读单一来源的调用点有 {count} 个（应为 {expected}）"
            "⇒ 有正文生成点还写死了温度"
        )


# ======================================================================
#  B. 生产提示词不再自相矛盾
# ======================================================================


def test_the_production_prompt_no_longer_demands_short_paragraphs_throughout():
    """生产正文提示词不得再要求「全程使用超短段落」（与 15% 法则直接矛盾）。"""
    text = _read(PROSE_PROMPT)

    assert "全程使用超短段落" not in text, (
        "生产正文提示词仍要求「全程使用超短段落」"
        "⇒ 与上下文里的「独段比例不得超过 15%」直接矛盾"
    )
    assert "长短段交替" in text


def test_the_15_percent_rule_is_still_there():
    """15% 那个口径**保留**（它在 9 处、且真的进了生产上下文）。"""
    assert "全章独段（1句成段）比例不得超过15%" in _read(RULE_PARSER)


def test_the_two_rules_are_not_both_in_the_same_prompt():
    """生产正文提示词里不得同时出现「全程超短段落」与「不得超过15%」。

    ⚠ 文本匹配这里指的是**提示词正文**，不是源码注释 ——
    文件里的说明注释提到旧措辞是允许的（这里查的是规则句本身）。
    """
    text = _read(PROSE_PROMPT)

    assert not ("全程使用超短段落" in text and "不得超过15%" in text), (
        "同一份生产提示词里同时要求「全程超短段落」和「独段不超过 15%」"
    )
