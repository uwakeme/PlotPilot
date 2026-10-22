"""H-06 子项 B：宏观诊断的「系统叙事校准」注入经**提案确认**。

改造前：宏观诊断跑完就把校准指令写进 ``macro_diagnosis_results.context_patch``，
由 ``context_budget_allocator`` 读出来**静默注入**生成上下文头部
⇒ 诊断结论直接改写后续章节怎么写，作者既看不到、也没机会拒绝。

现在：诊断结果带一个**注入决定**（pending / approved / rejected），
注入侧**只认 approved**。

为什么要新列而不是复用 ``resolved``
------------------------------------
``resolved`` 的既有语义是「已处理 ⇒ 停注入」（作者说这条我处理完了），
与「批准注入」方向**相反** ⇒ 复用会让两件事共用一个布尔，语义打架
（这正是登记表 H-06 点出的「``resolved`` 语义方向相反」）。

开关
----
``config/performance.yaml`` 的 ``macro_diagnosis_injection.enabled``
（**默认关闭**）⇒ 关闭时注入侧与改造前**逐位一致**（照旧静默注入）。

不变量
------
**注入 token 上限**：注入的校准文本一律截到 ``max_chars``
（登记表 H-06「代码保留的不变量」列）⇒ 一段诊断结论吃不掉整份上下文预算。
"""
from __future__ import annotations

from dataclasses import dataclass

from application.core.config.config_loader import get_config
from application.core.config.runtime_settings_utils import non_negative_int, section_value

#: 还没决定（默认状态）
DECISION_PENDING = "pending"
#: 批准 ⇒ 校准指令进生成上下文
DECISION_APPROVED = "approved"
#: 否决 ⇒ 不注入
DECISION_REJECTED = "rejected"

#: **可以由作者做出的决定**（pending 是初始状态，不是决定）
INJECTION_DECISIONS: tuple[str, ...] = (DECISION_APPROVED, DECISION_REJECTED)

#: 注入开关默认**关闭** ⇒ 行为与改造前逐位一致
DEFAULT_INJECTION_ENABLED = False
#: 注入文本上限（字符）。登记表 H-06 的不变量「注入 token 上限」。
DEFAULT_MAX_INJECTION_CHARS = 600

_TRUE_TOKENS = {"1", "true", "yes", "on", "y", "t"}
_FALSE_TOKENS = {"0", "false", "no", "off", "n", "f", ""}


@dataclass(frozen=True)
class MacroDiagnosisInjectionSettings:
    """宏观诊断注入的运行时设置。"""

    #: 是否要求「作者批准」才注入（关闭 = 静默注入，与改造前一致）
    enabled: bool = DEFAULT_INJECTION_ENABLED
    #: 注入文本上限（字符）
    max_chars: int = DEFAULT_MAX_INJECTION_CHARS


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_TOKENS:
            return True
        if text in _FALSE_TOKENS:
            return False
    return default


def get_macro_diagnosis_injection_settings() -> MacroDiagnosisInjectionSettings:
    """读 ``config/performance.yaml`` 的 ``macro_diagnosis_injection:`` 段。"""
    section = getattr(get_config(), "macro_diagnosis_injection", None)
    defaults = MacroDiagnosisInjectionSettings()
    return MacroDiagnosisInjectionSettings(
        enabled=_as_bool(
            section_value(section, "enabled", defaults.enabled),
            defaults.enabled,
        ),
        max_chars=non_negative_int(
            section_value(section, "max_chars", defaults.max_chars),
            defaults.max_chars,
        ),
    )


def macro_diagnosis_injection_enabled() -> bool:
    """注入是否必须等作者批准。"""
    return get_macro_diagnosis_injection_settings().enabled


def injection_max_chars() -> int:
    """注入文本上限（字符）。"""
    return get_macro_diagnosis_injection_settings().max_chars


def is_valid_decision(decision: object) -> bool:
    """这个决定值合法吗（只认 approved / rejected）。"""
    return isinstance(decision, str) and decision in INJECTION_DECISIONS


def decision_allows_injection(decision: object) -> bool:
    """这个决定允许注入吗（只有 approved）。"""
    return decision == DECISION_APPROVED
