# -*- coding: utf-8 -*-
"""第 81 批：把**灰度开关的当前状态**钉住。

⚠ 为什么要有这条：默认关闭的灰度开关**最容易被悄悄改掉**
  （改了没人知道 ⇒ 台账上的「已做」与生产实际不一致）。
⇒ 这里把「哪些 AI 参与通道当前是开的」**写死**：
  以后任何人想改，都必须**显式改这张表**（⇒ 变更在 diff 里看得见）。

⚠ 顺带钉住「**两个必须一起改**」的语义：
  ``context_weights`` 只开 ``enabled`` 是**空转**（shadow 模式只记录不施加）
  ⇒ 两者要么都开、要么都关 ⇒ 这里直接钉住两个值。
"""
from __future__ import annotations

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "performance.yaml"

#: 通道 → (enabled, shadow/apply 等附加开关, 为什么是这个状态)
#:
#: ⚠ 值是**当前生产状态**（2026-11-02 第 81 批按独立验收建议打开灰度）。
#: ⚠ 改这里 = 显式承认「我要改生产行为」⇒ 这是刻意的摩擦，不是阻碍。
GRAYSCALE_STATE = {
    # D-02 上下文槽位权重：enabled + shadow:false **两者都要**（只开 enabled 是空转）
    "context_weights": {"enabled": True, "shadow": False},
    # D-05 每章字数目标：AI 在 ±30% 内提议（超出即拒绝）
    "chapter_target": {"enabled": True},
    # E-06 每章节奏配额：送进正文提示词（≤600 字符）
    "rhythm_quota": {"enabled": True},
    # D-01 余韵章判定：**只判定不施加**（apply 仍关 ⇒ 正文提示词不变）
    "chapter_mode": {"enabled": False, "apply": False},
    # E-01b 章前剧本结构：改了会变**细纲形状** ⇒ 风险最大，保持关
    "chapter_plan_structure": {"enabled": False},
    # ⚠ 第 84 批（F-04 / N-05）：文风漂移**默认切 LLM 模式**
    #   （登记表 F-04 的「该谁定」列写的就是这一句）
    #   ⇒ 统计模式在写作链路上**没有基准**（voice_vault 0 行）⇒ 一章都没真评过
    #   ⇒ LLM 模式用本书前 5 章自建基准 ⇒ 第 6 章起真的能评估。
    #   ⚠ 段是**嵌套**的（``analyst.voice_analysis``）⇒ 用点号路径。
    "analyst.voice_analysis": {"use_llm_mode": True},
}


def _dig(config: dict, dotted: str):
    """按点号路径取段（``analyst.voice_analysis`` → 嵌套两层）。"""
    node = config
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8-sig")) or {}


def test_the_grayscale_state_is_pinned():
    """⚠ 灰度状态与本表不一致 ⇒ 生产行为被悄悄改过。"""
    config = _config()
    drift: list[str] = []
    for section, expected in GRAYSCALE_STATE.items():
        actual_section = _dig(config, section)
        if not isinstance(actual_section, dict):
            drift.append("%s: 配置里没有这一段" % section)
            continue
        for key, expected_value in expected.items():
            actual = actual_section.get(key)
            if actual != expected_value:
                drift.append(
                    "%s.%s 现在是 %r，本表钉的是 %r" % (section, key, actual, expected_value)
                )
    assert not drift, (
        "AI 参与通道的灰度状态漂了（生产行为被悄悄改了）：\n  " + "\n  ".join(drift)
    )


def test_the_context_weights_switch_is_not_half_open():
    """⚠ D-02：``enabled`` 与 ``shadow`` 必须**同时**开或同时关。

    ⚠ 只开 ``enabled`` 而 ``shadow: true`` ⇒ **空转**（只记录不施加）
    ⇒ 台账说「已启用」，生产上其实没生效 ⇒ 正是要防的那种漂移。
    """
    section = _config().get("context_weights") or {}
    enabled = bool(section.get("enabled"))
    shadow = bool(section.get("shadow"))
    assert enabled == (not shadow), (
        "context_weights 的 enabled=%r / shadow=%r ⇒ 处于「半开」状态"
        "（只记录不施加 ⇒ 等于没开）" % (enabled, shadow)
    )


def test_the_master_switch_is_still_on_by_default():
    """⚠ 总闸默认**开**（它是回退路径，不是新功能）⇒ 默认行为与今天逐位一致。"""
    section = _config().get("ai_participation") or {}
    assert section.get("master_enabled") is True, (
        "总闸默认关了 ⇒ 所有 AI 参与能力都不生效（默认行为变了）"
    )
