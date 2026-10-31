"""Anti-AI 评分的**单一事实源**。

背景
----
``AntiAIMetricsService``（``anti_ai_metrics.py``）与
``AntiAIAuditor``（``anti_ai_audit.py``）各定义了一份**逐键完全相同**的
``SEVERITY_WEIGHTS`` 与 ``ASSESSMENT_THRESHOLDS``，归一化公式也是数学等价的
两行写法。

两份定义今天一致，但没有任何机制保证它们**继续**一致 —— 改一边忘了另一边，
就会让「章指标快照」与「章节审计报告」对同一段文字给出不同的分数与评级，
而且不会有任何报错。

本模块把权重、阈值、归一化、分级四件事集中到一处。
两个服务类继续把常量暴露为类属性（外部代码可能读），但它们**指向同一对象**，
由 ``test_anti_ai_thresholds_single_source.py`` 的身份断言守住。

⚠ **口径已换过一次**（第 16 批，生产库实测后）：
归一化从「每万字」改成「**每千字**」，阈值表按千字尺度重标定。
原因：旧口径对**章节**这种 2000 字量级的文本惩罚极重（5 个命中即「严重」），
且好章节与 AI 味章节的分数挤在一起 ⇒ **分档没有区分力**。
详见 :data:`ASSESSMENT_THRESHOLDS` 的实测表。
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
#:
#: ⚠ **口径换成了「每千字加权命中数」**（见 :func:`normalize_severity_score`）。
#: 旧口径是「每万字」，阈值也是按万字尺度定的 ⇒ 对**章节**这种 2000 字量级的文本
#: 惩罚极重：**5 个命中就判「严重」**。
#:
#: 实测（生产库真实三章 + 一章 AI 味重的对照）：
#:
#: | 章节 | raw | 字数 | 旧口径 | 新口径 | 新评级 |
#: |---|---|---|---|---|---|
#: | 人设一致、推理扎实的一章 | 1.2（4 个破折号） | 2030 | 5.9 **严重** | 0.59 | **纯净** |
#: | 含 1 处「指尖泛白」的一章 | 3.9 | 2476 | 15.8 **严重** | 1.57 | 轻微 |
#: | AI 味重的一章（5 处情绪套路） | 5.0 | 2160 | 23.1 严重 | 2.31 | **中等** |
#:
#: ⇒ 旧口径**分不出好坏**（好章节 5.9 vs AI 味章节 23.1，都叫「严重」）；
#: 新口径能分（0.59 vs 2.31），且「严重」重新变成「满篇 AI 味」的意思
#: （2000 字章要 8 个内容级命中才到「严重」）。
ASSESSMENT_THRESHOLDS: List[Tuple[float, str]] = [
    (4.0, "严重"),
    (2.0, "中等"),
    (0.8, "轻微"),
    (0.0, "纯净"),
]

#: 无命中时的评级（阈值表最后一项兜底）
DEFAULT_ASSESSMENT: str = "纯净"

#: 归一化上限
MAX_SEVERITY_SCORE: float = 100.0

#: 反 AI 评分的**口径版本**（第 67 批 · B-5）。
#:
#: ⚠ 为什么需要版本号：第 16 批把归一化从「每万字」改成「每千字」，
#:   阈值表也跟着换了（5.0/3.0/1.0 → 4.0/2.0/0.8），
#:   但 ``anti_ai_audits`` 的**历史行没重算、也没记自己用的是哪套刻度**
#:   ⇒ 生产库一度出现「同一本书里既有旧刻度又有新刻度」
#:   ⇒ 趋势序列读起来像「AI 味暴跌 93%」，纯属刻度切换的假象。
#:
#: 版本历史：
#: - ``1`` = 每万字加权命中数（第 16 批**之前**）
#: - ``2`` = 每千字加权命中数（**当前**）
#:
#: ⚠ 落库时必须写当前版本 ⇒ 读侧才有办法判断「这一行能不能跟别的行比」。
SCORE_SCALE_VERSION: int = 2

#: 「严重」线（第 69 批 · R-3 补的**具名**常量）。
#:
#: ⚠ 为什么要有：DAG 节点与前端此前各自写了一份阈值（`> 50` / `> 5`），
#:   而后端评级用的是这里的表 ⇒ 三处不一致（DAG 那个线高 12.5 倍 ⇒ 节点**永不报警**）。
#: ⇒ 现在：谁要判「严重」都从这里取 ⇒ **一份**。
SEVERE_ASSESSMENT_THRESHOLD: float = ASSESSMENT_THRESHOLDS[0][0]


def raw_severity_score(severities) -> float:
    """按权重把一组严重性标签加总成 raw_score。"""
    total = 0.0
    for severity in severities:
        total += SEVERITY_WEIGHTS.get(severity, DEFAULT_SEVERITY_WEIGHT)
    return total


def normalize_severity_score(raw_score: float, char_count: int) -> float:
    """把 raw_score 归一化到 0-100 的「**每千字**加权命中数」。

    ⚠ 口径从「每万字」改成「每千字」。
    旧口径 ``raw * 10000 / chars`` 配上万字尺度的阈值 ⇒ 对**章节**这种
    2000 字量级的文本惩罚极重（**5 个命中就判「严重」**），
    而且好章节与 AI 味章节的分数挤在一起（实测 5.9 vs 23.1，**都叫「严重」**）
    ⇒ 分档**没有区分力**。
    新口径 ``raw * 1000 / chars`` 与「每千字命中数」这个直觉量纲一致
    （阈值表也是按千字标定的）⇒ 分数可读、评级能分出好坏。
    """
    chars = max(int(char_count), 1)
    return min(MAX_SEVERITY_SCORE, (raw_score / chars) * 1000)


def classify_assessment(score: float) -> str:
    """按阈值表把分数映射成评级文本。"""
    for threshold, label in ASSESSMENT_THRESHOLDS:
        if score >= threshold:
            return label
    return DEFAULT_ASSESSMENT
