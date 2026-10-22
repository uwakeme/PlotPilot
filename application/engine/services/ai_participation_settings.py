"""X-02：AI 参与能力的**总闸**（模型异常 / 成本失控时的回退路径）。

背景
----
全仓只有少数几项「AI 参与」能力各自带开关
（``chapter_mode`` / ``chapter_target`` / ``context_weights`` / 声音分析的 LLM 模式），
而上一批新增的 AI 调用点**一个开关都没有**
⇒ 模型异常或成本失控时**没有回退路径**（只能回滚代码）。

总闸语义（刻意保守，**默认不改变今天的行为**）
------------------------------------------------
- ``master_enabled`` 默认 **True** ⇒ 默认状态下与现在逐位一致；
- 置 False ⇒ 登记在册的「AI 参与」能力全部回到改造前行为（不发起模型调用），
  **并且**逐条开关也被强制关闭（**总闸优先于逐条开关** —— 这才是回退路径的保证）；
- ``disabled_features`` ⇒ 逐条点名要关的能力（总闸开着时也能单独关）。

⚠ **新增「AI 参与」能力时必须登记进 ``AI_PARTICIPATION_FEATURES``** ——
不登记 ⇒ 总闸管不到它（守卫 ``test_ai_participation_master_switch.py`` 盯这张表）。

配置位置：``config/performance.yaml`` 的 ``ai_participation:`` 段。
"""
from __future__ import annotations

from dataclasses import dataclass

from application.core.config.config_loader import get_config
from application.core.config.runtime_settings_utils import section_value

#: 登记在册的「AI 参与」能力（feature id）。
#: 总闸关闭 ⇒ 下面每一项都回到改造前行为（不发起模型调用）。
AI_PARTICIPATION_FEATURES: tuple[str, ...] = (
    "chapter_mode",  # D-01 余韵章判定
    "chapter_target",  # D-05 每章字数目标
    "context_weights",  # D-02 上下文槽位权重
    "voice_llm_mode",  # F-04 声音分析的 LLM 模式
    "continuation_strategy",  # D-08 续写策略
)


@dataclass(frozen=True)
class AiParticipationRuntimeSettings:
    #: 总闸。False ⇒ 所有登记能力强制关闭（逐条开关也失效）。
    master_enabled: bool = True
    #: 逐条点名关闭的能力（总闸开着时生效）。
    disabled_features: tuple[str, ...] = ()


def get_ai_participation_runtime_settings() -> AiParticipationRuntimeSettings:
    section = getattr(get_config(), "ai_participation", None)
    defaults = AiParticipationRuntimeSettings()
    raw_disabled = section_value(section, "disabled_features", ())
    if isinstance(raw_disabled, str):
        raw_disabled = [raw_disabled]
    disabled = tuple(
        str(item).strip() for item in (raw_disabled or ()) if str(item).strip()
    )
    return AiParticipationRuntimeSettings(
        master_enabled=bool(
            section_value(section, "master_enabled", defaults.master_enabled)
        ),
        disabled_features=disabled,
    )


def ai_participation_enabled(feature: str) -> bool:
    """该「AI 参与」能力此刻是否允许发起模型调用。

    总闸优先于逐条开关：总闸关闭 ⇒ 一律 False。
    未登记在 ``AI_PARTICIPATION_FEATURES`` 的 feature id 一律**拒绝**
    （不认识的 id 绝不默认放行 —— 那是「登记漏了 ⇒ 总闸失效」的入口）。
    """
    feature_id = str(feature or "").strip()
    if feature_id not in AI_PARTICIPATION_FEATURES:
        return False
    settings = get_ai_participation_runtime_settings()
    if not settings.master_enabled:
        return False
    return feature_id not in settings.disabled_features
