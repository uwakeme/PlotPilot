"""统一开关读取层（设计规格 §3.2）。

规格原文（``docs/superpowers/specs/2026-10-16-ai-participation-decision-design.md`` §3.2）::

    仿 PLOTPILOT_USE_STORY_PIPELINE 模式，但收敛成**统一读取层**，不散落各处：
        优先级：novel.generation_prefs.<key>  >  env  >  硬编码默认
    第 1 批（13 条）+ 第 2 批（17 条）共 30 条共用这个读取层。
    **不给每个条目单独造配置项** —— 否则配置面会比代码改动还大。

为什么之前没做到（第二轮审查 Q-1）
-----------------------------------
实现里**只有 ``config/performance.yaml` 一层** ⇒ env 级与 novel 级**都不存在**
⇒ 「按部署回退」「按这本书回退」两件事都做不到 ⇒ 验收 3 判不了「通过」。

三级各自的含义
--------------
1. **novel 级** —— ``novel.generation_prefs.ai_switches["<key>"]``
   （每本书**一个通用字段**承载所有开关 ⇒ 正是规格「不给每个条目单独造配置项」的意思）；
2. **env 级** —— ``PLOTPILOT_AI_<KEY>``（部署级回退，仿 ``PLOTPILOT_USE_STORY_PIPELINE``）；
3. **默认级** —— 调用方传进来的默认值（各模块从 ``config/performance.yaml`` 读的那份）。

⚠ **零行为变化**：novel 与 env 都没给时，返回的**就是**调用方传的默认值
  ⇒ 与改造前**逐位一致**（守卫 ``test_ai_switch_layer.py`` 钉住这条）。

⚠ **为什么用 contextvar 承载 novel 级**
  开关的读取点很深（管线步骤 / 规划器 / 服务），而「这本书的覆盖」只有一个来源
  ⇒ 用 **contextvar** 在**章级**设一次 ⇒ 深层读取不用改任何签名
  ⇒ 也天然**按章隔离**（章结束就还原 ⇒ 不会漏到下一章）。
"""
from __future__ import annotations

import os
from contextvars import ContextVar, Token
from typing import Any, Dict, Mapping, Optional

#: 开关 key → env 变量名。
#:
#: ⚠ 这张表是**登记在册**的开关清单（与 ``AI_PARTICIPATION_FEATURES`` 同一纪律）：
#:   未登记的 key 不许出现在 novel 级覆盖里（守卫会查）。
AI_SWITCH_KEYS: Dict[str, str] = {
    "chapter_mode.enabled": "PLOTPILOT_AI_CHAPTER_MODE",
    "chapter_mode.apply": "PLOTPILOT_AI_CHAPTER_MODE_APPLY",
    "context_weights.enabled": "PLOTPILOT_AI_CONTEXT_WEIGHTS",
    "context_weights.shadow": "PLOTPILOT_AI_CONTEXT_WEIGHTS_SHADOW",
    "voice_llm_mode": "PLOTPILOT_AI_VOICE_LLM_MODE",
}

#: 当前「这本书」的开关覆盖（``None`` = 没有 novel 级上下文）。
#:
#: ⚠ 默认 ``None`` 而不是 ``{}`` ⇒ 「没有上下文」与「上下文为空」可分辨
#:   （前者连 env 都不该被 novel 级挡掉）。
_NOVEL_SWITCHES: ContextVar[Optional[Mapping[str, Any]]] = ContextVar(
    "ai_novel_switches", default=None
)

#: env 值 → 布尔。⚠ **解析不出来就当没给**（不许猜）。
_TRUE_WORDS = frozenset({"1", "true", "yes", "on", "enable", "enabled"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off", "disable", "disabled"})


def env_switch_name(key: str) -> str:
    """开关 key → 它的 env 变量名（未登记的 key 按统一命名规则推导）。"""
    registered = AI_SWITCH_KEYS.get(key)
    if registered:
        return registered
    return "PLOTPILOT_AI_" + str(key or "").strip().upper().replace(".", "_").replace("-", "_")


def parse_env_bool(raw: Any) -> Optional[bool]:
    """env 文本 → 布尔；**认不出来返回 None**（⇒ 落回下一级，不猜）。"""
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return None


def set_novel_switches(switches: Optional[Mapping[str, Any]]) -> Token:
    """设置「当前这本书」的开关覆盖（章级调用 ⇒ 返回 token 供还原）。"""
    return _NOVEL_SWITCHES.set(dict(switches) if switches else switches)


def reset_novel_switches(token: Token) -> None:
    """还原「当前这本书」的开关覆盖（章级 finally ⇒ 不会漏到下一章）。"""
    _NOVEL_SWITCHES.reset(token)


def current_novel_switches() -> Optional[Mapping[str, Any]]:
    """当前 novel 级的开关覆盖（没有上下文 ⇒ ``None``）。"""
    return _NOVEL_SWITCHES.get()


def resolve_switch(
    key: str,
    *,
    default: bool,
    novel_switches: Optional[Mapping[str, Any]] = None,
) -> bool:
    """解析一个开关：**novel 级 > env 级 > 默认级**。

    Args:
        key: 开关 key（如 ``"chapter_mode.enabled"``）。
        default: 默认级的值（调用方从 ``config/performance.yaml`` 读的那份）。
        novel_switches: 显式给出 novel 级覆盖；不给就用 contextvar 里当前那本。

    Returns:
        解析后的布尔值。⚠ **novel / env 都没给时返回的就是 ``default``**
        ⇒ 与改造前逐位一致。
    """
    table = novel_switches if novel_switches is not None else current_novel_switches()
    if table:
        try:
            value = table.get(key)
        except AttributeError:
            value = None
        if value is not None:
            return bool(value)

    parsed = parse_env_bool(os.environ.get(env_switch_name(key)))
    if parsed is not None:
        return parsed

    return bool(default)
