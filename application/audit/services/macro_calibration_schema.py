"""H-06：宏观叙事校准的 LLM 输出契约与归一化。

单独成模块（不放在 planner 里）是为了**避免循环导入**：
``prompt_contracts.macro_calibration`` 要拿这里的 payload，
而 planner 又要用那个契约 —— 两者必须分开，形态照 D-01 的
``chapter_mode_schema``。

三态
----
- 正常 ⇒ 返回 LLM 写的校准文本
- LLM 没给 / 调用失败 / 输出不合契约 ⇒ 空串 ⇒ **不注入任何东西**。

绝不回落到代码代写的模板句 —— 那正是要消灭的东西。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

#: 校准指令长度上限
MAX_CALIBRATION_CHARS = 1200


class MacroCalibrationLlmPayload(BaseModel):
    """planning-macro-calibration 节点的 LLM 输出。"""

    calibration: str = Field(
        default="",
        description="给后续生成的叙事校准指令；判不出来时留空（不注入）",
    )
    reason: str = Field(
        default="",
        description="判断依据；判不出来时说明为什么",
    )


def normalize_calibration(calibration: Any, reason: Any) -> str:
    """取校准文本；没有就是没有（**不补默认句**）。"""
    if not isinstance(calibration, str):
        return ""
    text = calibration.strip()[:MAX_CALIBRATION_CHARS]
    return text
