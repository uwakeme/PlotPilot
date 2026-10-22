"""E-07：正文生成的采样参数（**单一来源**）。

改造前 ``temperature=0.85`` 在 **5 个正文生成点**写死：

- ``engine/pipeline/prose_composer.py`` ×2（StoryPipeline 生产路径）
- ``engine/runtime/daemon_host.py`` ×1
- ``engine/runtime/legacy_writing_delegate.py`` ×2

⇒ 改一次要改 5 处，漏一处就会出现两条路径温度不一致。

现在唯一来源是 ``config/performance.yaml`` 的 ``prose_sampling:`` 段；
默认值 **0.85** ⇒ 行为与改造前**逐位一致**。

⚠ **已知的不一致（不在本笔范围内）**
--------------------------------
``application/workflows/auto_novel_generation_workflow.py`` 里的
``GenerationConfig()`` 用的是 **domain 那个类**的默认
``temperature=1.0``（domain 与 ``engine.core.ports`` 两个 ``GenerationConfig``
类的默认值本来就不同：1.0 与 0.85）。
那是**改造前就有**的路径差异（剧本→正文那条路径一直是 1.0），
把它改成 0.85 就是**行为变更** ⇒ 本笔只把「显式 0.85」的那 5 处收口，
并把这条差异记进 ledger，不在这里悄悄统一。
"""
from __future__ import annotations

from dataclasses import dataclass

from application.core.config.config_loader import get_config
from application.core.config.runtime_settings_utils import (
    non_negative_float,
    section_value,
)

#: 改造前正文生成的温度（保持逐位一致）
DEFAULT_PROSE_TEMPERATURE = 0.85
#: 改造前正文生成的 top_p（``engine.core.ports.GenerationConfig`` 的默认值）
DEFAULT_PROSE_TOP_P = 0.95


@dataclass(frozen=True)
class ProseSamplingSettings:
    """正文生成的采样参数。"""

    temperature: float = DEFAULT_PROSE_TEMPERATURE
    top_p: float = DEFAULT_PROSE_TOP_P


def get_prose_sampling_settings() -> ProseSamplingSettings:
    """读 ``config/performance.yaml`` 的 ``prose_sampling:`` 段（读不到用默认值）。"""
    section = getattr(get_config(), "prose_sampling", None)
    defaults = ProseSamplingSettings()
    return ProseSamplingSettings(
        temperature=non_negative_float(
            section_value(section, "temperature", defaults.temperature),
            defaults.temperature,
        ),
        top_p=non_negative_float(
            section_value(section, "top_p", defaults.top_p),
            defaults.top_p,
        ),
    )


def prose_temperature() -> float:
    """正文生成的温度（单一来源）。"""
    return get_prose_sampling_settings().temperature
