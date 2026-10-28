"""A1 设定漂移审计的运行时开关（第 33 批）。

**为什么需要**（生产实测，第 31/32 批登记）
----------------------------------------
审计是**核对型**任务（拿正文逐条比对基准事实），不需要「长思考」；
而 provider 级的参数（``thinking`` / ``reasoning_effort`` …）以前**只来自激活画像**
⇒ **单次调用改不了** ⇒ 审计只能陪着一起深思。

实测（同一份 prompt、生产同口径、``glm-5.3-flash``）：

* 思考照开：``394 秒`` / ``out=15709`` / 报出 **4** 条 issues
* 关思考：  ``36 秒``  / ``out=1206``  / 报出 **5** 条 issues（**检出没减**）

⇒ **快 11 倍，检出没减** ⇒ 值得开。

⚠ **provider 特定**：``thinking.type=disabled`` 是 GLM / 智谱的写法
⇒ 值放在 ``config/performance.yaml`` 的 ``audit.setting_drift.extra_body``
（**换 provider 时改配置即可**，不必改代码）。
⇒ 机制本身与 provider 无关：这份 ``extra_body`` 会作为**单次调用覆盖**送到请求体
（同名键以本值为准 ⇒ 画像里的 ``thinking`` 会被这里**整键替换**）。

配置位置：``config/performance.yaml`` 的 ``audit.setting_drift:`` 段。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from application.core.config.config_loader import get_config
from application.core.config.runtime_settings_utils import section_value


@dataclass(frozen=True)
class SettingDriftRuntimeSettings:
    #: 单次调用的 provider 参数覆盖（``extra_body``）。
    #: ``None`` = 不覆盖 ⇒ **行为与改造前完全一致**（画像的 extra_body 原样生效）。
    extra_body: Optional[Dict[str, Any]] = None


def get_setting_drift_runtime_settings() -> SettingDriftRuntimeSettings:
    """读 ``config/performance.yaml`` 的 ``audit.setting_drift:`` 段。"""
    audit = getattr(get_config(), "audit", None)
    section = getattr(audit, "setting_drift", None)
    raw = section_value(section, "extra_body", None)
    # ⚠ 只接受**非空 dict**；其它一律当作「不覆盖」⇒ 零行为变化
    #   （配置写错时宁可慢，也不能把审计改坏）。
    if not isinstance(raw, dict) or not raw:
        return SettingDriftRuntimeSettings(extra_body=None)
    return SettingDriftRuntimeSettings(extra_body=dict(raw))
