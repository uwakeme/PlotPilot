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

第 76 批（S-5' / N-4 / N-3）的三处收口
-----------------------------------
1. **env 名按规则推导**（不再手写一张映射表）：
   规格写的是 ``PLOTPILOT_AI_<KEY>`` ⇒ 键里的点/横线统一成下划线。
   ⚠ 之前手写的那张表**与规格不符**（``chapter_mode.enabled`` 写成了
   ``PLOTPILOT_AI_CHAPTER_MODE`` ⇒ 按规格配**静默不生效**），
   而且那个名字**正好撞上**未登记键 ``chapter_mode`` 的推导名
   ⇒ 未登记的父键会把已登记的子键的 env 抢走。
   ⇒ 现在 env 名**由键推导** ⇒ 结构上不会漂、也不会撞名。
   ⇒ 旧名保留为**废弃别名**：仍然认，但会打 deprecation 警告（不静默失效）。
2. **未登记的 ``PLOTPILOT_AI_*`` 会出声**（``audit_env_switches``）：
   配了一个代码里根本没有的开关 ⇒ 之前**完全静默** ⇒ 现在启动时报警告。
3. **novel 级够不到的键会出声**（``NOVEL_UNREACHABLE_KEYS``）：
   有的开关是**进程级单例**（构造时冻结）⇒ 「这本书」的覆盖在那儿没有落点
   ⇒ 之前是**静默无效** ⇒ 现在设了会报警告。
"""
from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

def derive_env_name(key: str) -> str:
    """开关 key → env 变量名（**统一规则**：``PLOTPILOT_AI_<KEY>``）。

    规格 §3.2 写的就是 ``PLOTPILOT_AI_<KEY>`` ⇒ 键里的分隔符统一成下划线：

        chapter_mode.enabled → PLOTPILOT_AI_CHAPTER_MODE_ENABLED
        voice_llm_mode       → PLOTPILOT_AI_VOICE_LLM_MODE

    ⚠ 名字**由键推导** ⇒ 两个键推导到同一个名字只可能「键本身归一化后相同」
      ⇒ 那种情况由守卫（``test_the_env_names_are_unique``）挡住。
    """
    text = str(key or "").strip().upper()
    text = re.sub(r"[^A-Z0-9]+", "_", text)
    return "PLOTPILOT_AI_" + text


#: 登记在册的开关 key（**只有 key**；env 名由 key 推导 ⇒ 不可能各写一份）。
REGISTERED_SWITCH_KEYS: Tuple[str, ...] = (
    "chapter_mode.enabled",
    "chapter_mode.apply",
    "context_weights.enabled",
    "context_weights.shadow",
    "voice_llm_mode",
)

#: 开关 key → env 变量名（**由 key 推导**，不是手写 ⇒ 结构上不漂、不撞名）。
#:
#: ⚠ 这张表是**登记在册**的开关清单（与 ``AI_PARTICIPATION_FEATURES`` 同一纪律）：
#:   未登记的 key 不许出现在 novel 级覆盖里（守卫会查）。
AI_SWITCH_KEYS: Dict[str, str] = {
    key: derive_env_name(key) for key in REGISTERED_SWITCH_KEYS
}

#: 旧 env 名 → 新 env 名（**已废弃**，仍然认，但会打 deprecation 警告）。
#:
#: ⚠ 为什么要留：改名字会让**已经在部署环境里配好的**变量静默失效
#:   ⇒ 「配了不生效」是最坏的失败方式 ⇒ 旧的照样认，只是提醒改名。
LEGACY_ENV_NAMES: Dict[str, str] = {
    #: 旧名 = 父键名（``chapter_mode``）⇒ 正好和未登记父键的推导名撞车
    "PLOTPILOT_AI_CHAPTER_MODE": "PLOTPILOT_AI_CHAPTER_MODE_ENABLED",
    "PLOTPILOT_AI_CONTEXT_WEIGHTS": "PLOTPILOT_AI_CONTEXT_WEIGHTS_ENABLED",
}

#: 已知「**novel 级够不到**」的键 → 原因。
#:
#: ⚠ 这些开关的读取点在**进程级单例**里（构造时冻结）⇒ 「这本书」的覆盖
#:   在那儿没有落点 ⇒ 之前是**静默无效**（用户以为配了，其实在跑 env/默认级）。
#:   ⇒ 现在在设了它们的时候会**出声**（``warn_unreachable_novel_switches``）。
#:
#: ⚠ 第 79 批：``voice_llm_mode`` **已摘出** —— 文风服务改成「每次读都解析」
#:   ＋「LLM 文风服务懒创建」⇒ novel 级**真的生效**了
#:   ⇒ 继续留着会**误报**（本书设了它就会喊「无效」）。
NOVEL_UNREACHABLE_KEYS: Dict[str, str] = {}

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

#: 已经报过 warning 的东西（只报一次 ⇒ 不刷屏）。
_WARNED: set = set()


def env_switch_name(key: str) -> str:
    """开关 key → 它的 env 变量名（**一律按规则推导**）。

    ⚠ 不再查「手写映射表」⇒ 登记表的 env 名**只可能**是推导出来的那个
      ⇒ 规格与实现不会漂移。
    """
    return derive_env_name(key)


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


@contextmanager
def novel_switch_scope(switches: Optional[Mapping[str, Any]]) -> Iterator[None]:
    """把「这本书的开关覆盖」放进 contextvar（**用完自动还原**）。

    ⚠ 这是**唯一**该写 set/reset 的地方 ⇒ 不再各处手写 try/finally
      （第 76 批：同一逻辑写两份 ⇒ 合并）。
    """
    token = set_novel_switches(switches)
    try:
        yield
    finally:
        reset_novel_switches(token)


def warn_unreachable_novel_switches(
    switches: Optional[Mapping[str, Any]], *, novel_id: str = ""
) -> list:
    """「这本书设了**够不到**的开关」⇒ 报警告（返回报警的键，供守卫/测试用）。

    ⚠ **配了不生效必须出声**：这些键的读取点在进程级单例里
      ⇒ 「这本书」的覆盖在那儿没有落点 ⇒ 静默无效是最坏的失败方式。
    """
    warned: list = []
    if not switches:
        return warned
    for key, value in switches.items():
        reason = NOVEL_UNREACHABLE_KEYS.get(key)
        if not reason:
            continue
        marker = (key, str(novel_id))
        if marker not in _WARNED:
            _WARNED.add(marker)
            logger.warning(
                "[AI开关] 这本书把 %s 设成了 %r，但该项**novel 级够不到**（%s）"
                " ⇒ 本书的覆盖无效，实际生效的是 env/默认级",
                key,
                value,
                reason,
            )
        warned.append(key)
    return warned


def audit_env_switches(env: Optional[Mapping[str, str]] = None) -> list:
    """体检环境变量里的 ``PLOTPILOT_AI_*``（**配了不生效要出声**）。

    返回告警文本列表；每条都**同时**打日志。

    三类告警：

    1. **未登记的开关** —— 代码里根本没有这个键 ⇒ 配了不会生效；
    2. **废弃别名** —— 旧名仍然认，但建议改成新名；
    3. 登记在册且用新名 ⇒ 不告警。
    """
    source = os.environ if env is None else env
    known_new = set(AI_SWITCH_KEYS.values())
    legacy_to_new = {old: new for old, new in LEGACY_ENV_NAMES.items()}
    warnings: list = []
    for name in sorted(source):
        if not name.startswith("PLOTPILOT_AI_"):
            continue
        if name in known_new:
            continue
        if name in legacy_to_new:
            text = (
                "环境变量 %s 已废弃，请改用 %s（旧的仍然生效，但这是最后一次兼容）"
                % (name, legacy_to_new[name])
            )
        else:
            text = (
                "环境变量 %s 对应**未登记**的开关 ⇒ 不会生效"
                "（登记在册的开关：%s）"
                % (name, ", ".join(sorted(AI_SWITCH_KEYS)))
            )
        warnings.append(text)
        if name not in _WARNED:
            _WARNED.add(name)
            logger.warning("[AI开关] %s", text)
    return warnings


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

    canonical = env_switch_name(key)
    parsed = parse_env_bool(os.environ.get(canonical))
    if parsed is not None:
        return parsed

    # ⚠ 废弃别名：旧名仍然认（不静默失效），但会提醒改名。
    legacy = _legacy_name_for(key)
    if legacy:
        parsed = parse_env_bool(os.environ.get(legacy))
        if parsed is not None:
            if legacy not in _WARNED:
                _WARNED.add(legacy)
                logger.warning(
                    "[AI开关] 环境变量 %s 已废弃，请改用 %s（旧的仍然生效）",
                    legacy,
                    canonical,
                )
            return parsed

    return bool(default)


def _legacy_name_for(key: str) -> Optional[str]:
    """这个键的废弃旧 env 名（没有 ⇒ None）。"""
    canonical = env_switch_name(key)
    for old, new in LEGACY_ENV_NAMES.items():
        if new == canonical:
            return old
    return None
