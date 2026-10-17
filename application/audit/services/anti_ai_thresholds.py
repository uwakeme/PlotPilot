"""Anti-AI 评分的**单一事实源**。

背景
----
``AntiAIMetricsService``（``anti_ai_metrics.py``）与
``AntiAIAuditor``（``anti_ai_audit.py``）各定义了一份**逐键完全相同**的
``SEVERITY_WEIGHTS`` 与 ``ASSESSMENT_THRESHOLDS``，归一化公式也是数学等价的
两行写法（``(raw/(chars/1000))*10`` ≡ ``(raw/chars)*1000*10`` ≡ ``raw*10000/chars``）。

两份定义今天一致，但没有任何机制保证它们**继续**一致 —— 改一边忘了另一边，
就会让「章指标快照」与「章节审计报告」对同一段文字给出不同的分数与评级，
而且不会有任何报错。

本模块把权重、阈值、归一化、分级四件事集中到一处。
两个服务类继续把常量暴露为类属性（外部代码可能读），但它们**指向同一对象**，
由 ``test_anti_ai_thresholds_single_source.py`` 的身份断言守住。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

#: 严重性权重。命中项按此加权求和得到 raw_score。
SEVERITY_WEIGHTS: Dict[str, float] = {
    "critical": 3.0,
    "warning": 1.0,
    "info": 0.3,
}

#: 未知严重性时的兜底权重。**刻意不等于 0** ——
#: 0 会让「严重性读不出来」被算成「没问题」，那正是本项目反复清理的那类谎报。
DEFAULT_SEVERITY_WEIGHT: float = 0.5

#: 评级阈值，按分数从高到低，首次命中即返回。
ASSESSMENT_THRESHOLDS: List[Tuple[float, str]] = [
    (5.0, "严重"),
    (3.0, "中等"),
    (1.0, "轻微"),
    (0.0, "纯净"),
]

#: 无命中时的评级（阈值表最后一项兜底）
DEFAULT_ASSESSMENT: str = "纯净"

#: 归一化上限
MAX_SEVERITY_SCORE: float = 100.0


def raw_severity_score(severities) -> float:
    """按权重把一组严重性标签加总成 raw_score。"""
    total = 0.0
    for severity in severities:
        total += SEVERITY_WEIGHTS.get(severity, DEFAULT_SEVERITY_WEIGHT)
    return total


def normalize_severity_score(raw_score: float, char_count: int) -> float:
    """把 raw_score 归一化到 0-100 的「每万字加权命中数」。

    等价式（原先两处写法不同但数学一致）::

        (raw / (chars / 1000)) * 10  ==  (raw / chars) * 1000 * 10  ==  raw * 10000 / chars
    """
    chars = max(int(char_count), 1)
    return min(MAX_SEVERITY_SCORE, (raw_score / chars) * 1000 * 10)


def classify_assessment(score: float) -> str:
    """按阈值表把分数映射成评级文本。"""
    for threshold, label in ASSESSMENT_THRESHOLDS:
        if score >= threshold:
            return label
    return DEFAULT_ASSESSMENT
