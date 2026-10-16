# domain/ai/value_objects/assessment_state.py
"""评估状态 —— 区分「判过了」「没判」「被拦」。

本项目曾长期存在一类缺陷：降级路径返回看起来正常的值，使下游把
「根本没跑成」当成「检查通过」。本类型是决策点登记表 L 类的统一收口。

参见 docs/superpowers/specs/2026-10-16-ai-participation-decision-design.md §3.1
"""
from __future__ import annotations

from enum import Enum


class AssessmentState(str, Enum):
    """评估结果的三态。

    DECIDED
        真实跑过且得出结论，下游可依赖其数值。
    UNASSESSED
        没跑成 / 没数据 / 解析失败。**下游不得当作 0 或「干净」使用。**
    BLOCKED
        不变量被破，结果已拒收。
    """

    DECIDED = "decided"
    UNASSESSED = "unassessed"
    BLOCKED = "blocked"
