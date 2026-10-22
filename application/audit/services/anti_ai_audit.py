"""Anti-AI 指标与审计服务 — Layer 7: 章后审计与指标追踪。

核心功能：
- 对生成的章节进行 50+ 模式 AI 味扫描
- 计算综合 AI 味评分
- 生成结构化审计报告
- 追踪章节间的 AI 味趋势
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from application.audit.services.cliche_scanner import ClicheHit, ClicheScanner

from application.audit.services.anti_ai_thresholds import (  # noqa: E402
    ASSESSMENT_THRESHOLDS as _ASSESSMENT_THRESHOLDS,
    SEVERITY_WEIGHTS as _SEVERITY_WEIGHTS,
    classify_assessment,
    normalize_severity_score,
    raw_severity_score,
)

logger = logging.getLogger(__name__)


@dataclass
class AntiAIMetrics:
    """Anti-AI 指标快照。"""
    chapter_id: str = ""
    total_hits: int = 0
    critical_hits: int = 0
    warning_hits: int = 0
    info_hits: int = 0
    severity_score: float = 0.0  # 0-100，越高AI味越重
    category_distribution: Dict[str, int] = field(default_factory=dict)
    top_patterns: List[str] = field(default_factory=list)
    overall_assessment: str = "未检测"  # 纯净/轻微/中等/严重


@dataclass
class AntiAIAuditReport:
    """Anti-AI 审计报告。"""
    chapter_id: str = ""
    metrics: AntiAIMetrics = field(default_factory=AntiAIMetrics)
    hits: List[ClicheHit] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    improvement_suggestions: List[str] = field(default_factory=list)


class AntiAIAuditor:
    """Anti-AI 审计服务。"""

    # 单一事实源（anti_ai_thresholds）——
    # 本类属性**指向同一对象**，不是副本；改一处即两处生效。
    # 身份断言见 tests/unit/application/audit/test_anti_ai_thresholds_single_source.py
    SEVERITY_WEIGHTS = _SEVERITY_WEIGHTS
    ASSESSMENT_THRESHOLDS = _ASSESSMENT_THRESHOLDS

    def scan_chapter(
        self, chapter_id: str, content: str, novel_id: str = ""
    ) -> AntiAIAuditReport:
        """对章节进行 Anti-AI 审计。

        ``novel_id`` 给了 ⇒ 把该小说**已批准的已学规则**（F-06：AI 归纳、落库、
        版本化）一起喂给扫描器 ⇒ 归纳出来的模式**真的会被检测到**（闭环）。
        """
        scanner = ClicheScanner(
            use_enhanced=True, extra_patterns=self._load_learned_patterns(novel_id)
        )
        hits = scanner.scan_cliches(content)
        metrics = self._calculate_metrics(chapter_id, hits, content)
        recommendations = self._generate_recommendations(metrics, hits)
        improvement_suggestions = self._generate_improvement_suggestions(hits)
        return AntiAIAuditReport(
            chapter_id=chapter_id,
            metrics=metrics,
            hits=hits,
            recommendations=recommendations,
            improvement_suggestions=improvement_suggestions,
        )

    @staticmethod
    def _load_learned_patterns(novel_id: str) -> List[Tuple[str, str, str, str, str]]:
        """该小说已批准的已学规则（F-06）。取不到 ⇒ 空列表（不阻断审计）。"""
        if not novel_id:
            return []
        try:
            from application.audit.services.anti_ai_learning import (
                get_anti_ai_learning_service,
            )

            return get_anti_ai_learning_service().get_approved_patterns(novel_id)
        except Exception:
            return []

    def _calculate_metrics(self, chapter_id: str, hits: List[ClicheHit], content: str) -> AntiAIMetrics:
        """计算 Anti-AI 指标。"""
        critical_count = sum(1 for h in hits if h.severity == "critical")
        warning_count = sum(1 for h in hits if h.severity == "warning")
        info_count = sum(1 for h in hits if h.severity == "info")

        char_count = max(len(content), 1)
        raw_score = raw_severity_score(h.severity for h in hits)
        normalized_score = normalize_severity_score(raw_score, char_count)

        category_dist: Dict[str, int] = {}
        for h in hits:
            cat = h.category or "其他"
            category_dist[cat] = category_dist.get(cat, 0) + 1

        pattern_counts: Dict[str, int] = {}
        for h in hits:
            pattern_counts[h.pattern] = pattern_counts.get(h.pattern, 0) + 1
        top_patterns = sorted(pattern_counts, key=pattern_counts.get, reverse=True)[:5]

        assessment = classify_assessment(normalized_score)

        return AntiAIMetrics(
            chapter_id=chapter_id, total_hits=len(hits),
            critical_hits=critical_count, warning_hits=warning_count, info_hits=info_count,
            severity_score=round(normalized_score, 1),
            category_distribution=category_dist, top_patterns=top_patterns,
            overall_assessment=assessment,
        )

    def _generate_recommendations(self, metrics: AntiAIMetrics, hits: List[ClicheHit]) -> List[str]:
        """生成修改建议。"""
        recs = []
        if metrics.critical_hits > 0:
            critical_patterns = [h.pattern for h in hits if h.severity == "critical"]
            unique = list(dict.fromkeys(critical_patterns))[:3]
            recs.append(f"紧急：发现 {metrics.critical_hits} 处严重AI味模式（{', '.join(unique)}），必须修改")
        if metrics.category_distribution.get("微表情", 0) > 3:
            recs.append("微表情过多：建议用完整姿态变化或对白本身替代")
        if metrics.category_distribution.get("比喻", 0) > 2:
            recs.append("比喻过多：建议用感官细节替代比喻")
        if metrics.category_distribution.get("声线", 0) > 2:
            recs.append("声线标签过多：建议用对白的标点和断句表现语气")
        if metrics.category_distribution.get("情绪", 0) > 2:
            recs.append("情绪标签过多：建议通过动作和环境暗示传递情绪")
        return recs

    def _generate_improvement_suggestions(self, hits: List[ClicheHit]) -> List[str]:
        """生成具体改进建议。"""
        suggestions = []
        seen_patterns = set()
        for h in hits:
            if h.pattern in seen_patterns:
                continue
            seen_patterns.add(h.pattern)
            if h.replacement_hint:
                suggestions.append(f"[{h.category}] {h.pattern} → {h.replacement_hint}")
            if len(suggestions) >= 5:
                break
        return suggestions


_auditor: Optional[AntiAIAuditor] = None

def get_anti_ai_auditor() -> AntiAIAuditor:
    global _auditor
    if _auditor is None:
        _auditor = AntiAIAuditor()
    return _auditor
