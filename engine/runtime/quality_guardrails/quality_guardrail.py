"""QualityGuardrail总控 — 六维度质量检查

核心设计：
- enforce模式：Checkpoint保存前拦截，不达标则阻止保存
- advise模式：提供建议但不阻止
- 六维度检查：语言风格/角色一致性/情节密度/命名/视角/节奏
- QualityViolationError：质量违规异常

集成流程：
1. advance_plot() 生成章节内容
2. QualityGuardrail.enforce() 检查
3. 通过 → 创建Checkpoint
4. 不通过 → 抛出QualityViolationError，返回修正建议
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple

from domain.ai.value_objects.assessment_state import AssessmentState
from engine.core.value_objects.character_mask import CharacterMask
from engine.runtime.quality_guardrails.language_style_guardrail import (
    LanguageStyleGuardrail, StyleViolation,
)
from engine.runtime.quality_guardrails.character_consistency_guardrail import (
    CharacterConsistencyGuardrail, ConsistencyViolation,
)
from engine.runtime.quality_guardrails.plot_density_guardrail import (
    PlotDensityGuardrail, DensityViolation,
)
from engine.runtime.quality_guardrails.naming_guardrail import (
    NamingGuardrail, NamingViolation,
)
from engine.runtime.quality_guardrails.viewpoint_guardrail import (
    ViewpointGuardrail, ViewpointViolation,
)
from engine.runtime.quality_guardrails.rhythm_guardrail import (
    RhythmGuardrail, RhythmViolation,
)
from engine.runtime.quality_guardrails.macro_pacing_guardrail import (
    MacroPacingGuardrail,
)

logger = logging.getLogger(__name__)

# 打分语义版本边界。
#
# v1（旧）：角色一致性 / 命名 / 视角三个维度在"取不到数据"时直接返回常量
#     （0.73 / 0.82 / 0.77），无论本章内容如何。
# v2：取得到数据就真判、判得到违规就扣分；只有"确实无法评估"
#     （缺 cast、同框不足两人）才保留 0.77 这个保守基准。
#     v1→v2 实测：同一章 overall +0.0616、单角色章节 +0.0230；综合分原本落在
#     [0.506, 0.600) 的章节会零内容变化地从 fail 翻成 pass（MIN_PASS_SCORE=0.6）。
# v3（本分支，登记表 F-09）：v1/v2 的 0.73 / 0.82 仍然是把"从未审评"
#     伪装成"审过了"。现在缺输入的维度记 None 且**不参与加权平均**，
#     overall 按剩余权重归一化；一个维度都评不出来时整体 UNASSESSED。
#
# 0.77 只在"有伏笔、无 cast"这一种**确实评估过绑定判据**的场景保留：
# 绑定违规会从它上面扣分，因此它是保守基准而非"没审评"的占位数。
#
# 跨版本的分不可比：同一章在 v3 下的分数与 v1/v2 不同是预期的——被删掉的
# 是从未审评却照样计入权重的分。历史数据的处理办法是重新基线化，
# 不是把常量往回调。
QUALITY_SCORE_SEMANTICS_VERSION = 3

# 各维度在整体分中的权重，合计 1.0。缺输入的维度会被剔除并重新归一化，
# 因此"少评一维"不会让整体分被稀释成看起来更保守的样子。
DIMENSION_WEIGHTS: Dict[str, float] = {
    "language_style": 0.23,
    "character_consistency": 0.23,
    "plot_density": 0.18,
    "naming": 0.05,
    "viewpoint": 0.10,
    "rhythm": 0.13,
    "macro_pacing": 0.08,
}


def _chapter_goal_is_weak_signal(goal: str) -> bool:
    """章节意图是否过于笼统（自动保存管线常见），情节密度对齐不可信时需折价。"""
    g = (goal or "").strip()
    if len(g) < 6:
        return True
    if "保存后自动" in g or "自动生成" in g:
        return True
    if g.startswith("第") and len(g) <= 28:
        return True
    return False


class QualityViolationError(Exception):
    """质量违规异常 — Checkpoint保存前拦截"""

    def __init__(self, violations: List[Dict[str, Any]], overall_score: float):
        self.violations = violations
        self.overall_score = overall_score
        message = f"质量检查不通过(评分{overall_score:.2f})，共{len(violations)}项违规"
        super().__init__(message)


@dataclass
class QualityReport:
    """质量检查报告

    三态语义（见 ``AssessmentState``）与逐维覆盖率：

    - ``state``：DECIDED 表示至少有一维真的评过；UNASSESSED 表示一维都没评出来，
      此时 ``overall_score`` 与 ``passed`` 均为 ``None``。
    - 可评估的维度分是 float，**不可评估的是 None**（不是 0，也不是常量）。
    - ``unevaluated_dimensions`` 必须显式列出缺口：state 为 DECIDED 时它可能非空，
      下游据此判断置信度，不得把部分覆盖当成完整覆盖。
    """
    overall_score: Optional[float] = None
    language_style_score: Optional[float] = None
    character_consistency_score: Optional[float] = None
    plot_density_score: Optional[float] = None
    naming_score: Optional[float] = None
    viewpoint_score: Optional[float] = None
    rhythm_score: Optional[float] = None
    macro_pacing_score: Optional[float] = None
    all_violations: List[Dict[str, Any]] = field(default_factory=list)
    passed: Optional[bool] = None
    state: AssessmentState = AssessmentState.UNASSESSED
    evaluated_dimensions: List[str] = field(default_factory=list)
    unevaluated_dimensions: List[str] = field(default_factory=list)

    @staticmethod
    def _round3(value: Optional[float]) -> Optional[float]:
        """未评估的维度是 None，落库前不得对它 round()——那会 TypeError。"""
        return None if value is None else round(value, 3)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall_score": self._round3(self.overall_score),
            "scores": {
                "language_style": self._round3(self.language_style_score),
                "character_consistency": self._round3(self.character_consistency_score),
                "plot_density": self._round3(self.plot_density_score),
                "naming": self._round3(self.naming_score),
                "viewpoint": self._round3(self.viewpoint_score),
                "rhythm": self._round3(self.rhythm_score),
                "macro_pacing": self._round3(self.macro_pacing_score),
            },
            "violation_count": len(self.all_violations),
            "violations": self.all_violations,
            "passed": self.passed,
            "state": self.state.value,
            "evaluated_dimensions": list(self.evaluated_dimensions),
            "unevaluated_dimensions": list(self.unevaluated_dimensions),
        }


class QualityGuardrail:
    """质量守门人总控

    使用方式：
    ```python
    guardrail = QualityGuardrail()

    # enforce模式：Checkpoint保存前拦截
    try:
        report = guardrail.enforce(text, character_masks, ...)
        # 通过 → 创建Checkpoint
    except QualityViolationError as e:
        # 不通过 → 修正后重写

    # advise模式：仅提供建议
    report = guardrail.advise(text, character_masks, ...)
    ```
    """

    # 最低通过分数
    MIN_PASS_SCORE = 0.6

    def __init__(self):
        self._language_style = LanguageStyleGuardrail()
        self._character_consistency = CharacterConsistencyGuardrail()
        self._plot_density = PlotDensityGuardrail()
        self._naming = NamingGuardrail()
        self._viewpoint = ViewpointGuardrail()
        self._rhythm = RhythmGuardrail()
        self._macro_pacing = MacroPacingGuardrail()

    def check(
        self,
        text: str,
        character_masks: Dict[str, CharacterMask] = None,
        chapter_goal: str = "",
        character_names: List[str] = None,
        scene_info: Dict[str, Any] = None,
        foreshadows: List[Dict[str, Any]] = None,
        era: str = "ancient",
        scene_type: str = "auto",
    ) -> QualityReport:
        """执行六维度质量检查

        Args:
            text: 待检查文本
            character_masks: 角色面具字典
            chapter_goal: 章节目标
            character_names: 角色名列表
            scene_info: 场景信息
            foreshadows: 活跃伏笔列表
            era: 时代背景
            scene_type: 场景类型

        Returns:
            QualityReport
        """
        violations: List[Dict[str, Any]] = []

        # 1. 语言风格
        ls_score, ls_violations = self._language_style.check(text)
        for v in ls_violations:
            violations.append({
                "dimension": "language_style",
                "type": v.type_name,
                "severity": v.severity,
                "original": v.original_text,
                "suggestion": v.suggestion,
            })

        # 2. 角色一致性：缺 cast 则该维度不可评估（记 None，不写死常量）
        cc_score: Optional[float] = None
        if character_masks:
            cc_score, cc_violations = self._character_consistency.check(text, character_masks)
            for v in cc_violations:
                violations.append({
                    "dimension": "character_consistency",
                    "type": v.type_name,
                    "character": v.character_name,
                    "severity": v.severity,
                    "description": v.description,
                    "suggestion": v.suggestion,
                })

        # 3. 情节密度
        pd_score, pd_violations = self._plot_density.check(text, chapter_goal)
        for v in pd_violations:
            violations.append({
                "dimension": "plot_density",
                "type": v.violation_type,
                "severity": v.severity,
                "description": v.description,
                "suggestion": v.suggestion,
            })
        if pd_score is not None and _chapter_goal_is_weak_signal(chapter_goal):
            pd_score = max(0.0, pd_score * 0.92 - 0.045)

        # 4. 命名：缺角色名则该维度不可评估（记 None，不写死常量）
        n_score: Optional[float] = None
        if character_names:
            n_score, n_violations = self._naming.check(character_names, era)
            for v in n_violations:
                violations.append({
                    "dimension": "naming",
                    "type": v.type_name,
                    "character": v.character_name,
                    "severity": v.severity,
                    "suggestion": v.suggestion,
                })

        # 5. 视角
        #    两类判据的前置条件不同，必须分开门控：
        #    - 视角选择/泄露依赖 scene_info（cast 推断），拿不到就保持保守分 0.77
        #    - 伏笔绑定层级只依赖 foreshadows，与视角语境无关
        #    历史上两者被同一个 `if scene_info:` 门控连带，导致没有 cast 的书
        #    会连带跳过伏笔绑定校验——那条校验就此静默失效。
        vp_score: Optional[float] = None
        vp_violations: List[Any] = []
        if scene_info:
            _, vp_violations = self._viewpoint.check(text, scene_info)
        if foreshadows:
            _, binding_violations = self._viewpoint.check(text, None, foreshadows)
            vp_violations = vp_violations + binding_violations
        if scene_info or foreshadows:
            # 有 scene_info 才能真正评估视角，基准 1.0；只有伏笔时以保守基准 0.77
            # 起算，只让已查出的违规扣分——避免"没数据"反而比"有数据且干净"更高分。
            # 两者都缺时该维度不可评估，保持 None（v3：不再用 0.77 占位）。
            base = 1.0 if scene_info else 0.77
            vp_score = max(0.0, base - self._viewpoint.severity_penalty(vp_violations))
        for v in vp_violations:
            violations.append({
                "dimension": "viewpoint",
                "type": v.type_name,
                "severity": v.severity,
                "description": v.description,
                "suggestion": v.suggestion,
            })

        # 6. 节奏
        r_score, r_violations = self._rhythm.check(text, scene_type)
        for v in r_violations:
            violations.append({
                "dimension": "rhythm",
                "type": v.type_name,
                "severity": v.severity,
                "description": v.description,
                "suggestion": v.suggestion,
            })

        # 7. 宏观节奏：补足“信息过载/过早结清”的反向检查
        mp_score, mp_violations = self._macro_pacing.check(text, chapter_goal, scene_info)
        for v in mp_violations:
            violations.append({
                "dimension": "macro_pacing",
                "type": v.violation_type,
                "severity": v.severity,
                "description": v.description,
                "suggestion": v.suggestion,
            })

        # 加权计算总分：只吃真正评估过的维度，并按剩余权重归一化。
        # 不可评估的维度记 None、不参与加权——写死常量会让「什么都没评」
        # 依然凑出高于 MIN_PASS_SCORE 的总分（登记表 F-09）。
        dimension_scores: Dict[str, Optional[float]] = {
            "language_style": ls_score,
            "character_consistency": cc_score,
            "plot_density": pd_score,
            "naming": n_score,
            "viewpoint": vp_score,
            "rhythm": r_score,
            "macro_pacing": mp_score,
        }
        evaluated = [k for k, v in dimension_scores.items() if v is not None]
        unevaluated = [k for k, v in dimension_scores.items() if v is None]

        if evaluated:
            total_weight = sum(DIMENSION_WEIGHTS[k] for k in evaluated)
            overall = (
                sum(dimension_scores[k] * DIMENSION_WEIGHTS[k] for k in evaluated)
                / total_weight
            )
            state = AssessmentState.DECIDED
            passed = overall >= self.MIN_PASS_SCORE
        else:
            overall = None
            state = AssessmentState.UNASSESSED
            passed = None

        return QualityReport(
            overall_score=overall,
            language_style_score=ls_score,
            character_consistency_score=cc_score,
            plot_density_score=pd_score,
            naming_score=n_score,
            viewpoint_score=vp_score,
            rhythm_score=r_score,
            macro_pacing_score=mp_score,
            all_violations=violations,
            passed=passed,
            state=state,
            evaluated_dimensions=evaluated,
            unevaluated_dimensions=unevaluated,
        )

    def enforce(
        self,
        text: str,
        character_masks: Dict[str, CharacterMask] = None,
        chapter_goal: str = "",
        character_names: List[str] = None,
        scene_info: Dict[str, Any] = None,
        foreshadows: List[Dict[str, Any]] = None,
        era: str = "ancient",
        scene_type: str = "auto",
        min_score: float = None,
    ) -> QualityReport:
        """强制执行质量检查（Checkpoint保存前拦截）

        不达标则抛出QualityViolationError

        Args:
            同check()

        Returns:
            QualityReport（通过时）

        Raises:
            QualityViolationError: 质量不达标时
        """
        report = self.check(
            text=text,
            character_masks=character_masks,
            chapter_goal=chapter_goal,
            character_names=character_names,
            scene_info=scene_info,
            foreshadows=foreshadows,
            era=era,
            scene_type=scene_type,
        )

        threshold = min_score or self.MIN_PASS_SCORE

        if report.overall_score is None:
            # 一个维度都没评出来：无从比较阈值。返回带 UNASSESSED 的报告由
            # 调用方决定，不得用常量分顶替——那正是 F-09 的成因。
            logger.warning("质量检查未评估（UNASSESSED），跳过阈值比较")
            return report

        if report.overall_score < threshold:
            raise QualityViolationError(
                violations=report.all_violations,
                overall_score=report.overall_score,
            )

        return report

    def advise(
        self,
        text: str,
        character_masks: Dict[str, CharacterMask] = None,
        chapter_goal: str = "",
        character_names: List[str] = None,
        scene_info: Dict[str, Any] = None,
        foreshadows: List[Dict[str, Any]] = None,
        era: str = "ancient",
        scene_type: str = "auto",
    ) -> QualityReport:
        """建议模式（不拦截，仅提供建议）"""
        return self.check(
            text=text,
            character_masks=character_masks,
            chapter_goal=chapter_goal,
            character_names=character_names,
            scene_info=scene_info,
            foreshadows=foreshadows,
            era=era,
            scene_type=scene_type,
        )
