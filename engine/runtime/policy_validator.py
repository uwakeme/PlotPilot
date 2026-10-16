"""PolicyValidator — 策略验证器（适配 QualityGuardrail）

对 QualityGuardrail 的薄封装，提供与 Pipeline 兼容的接口。
保持 QualityGuardrail 的全部六维度检查能力不变，
只调整了接口命名和返回值格式。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from domain.ai.value_objects.assessment_state import AssessmentState

logger = logging.getLogger(__name__)


class PolicyReport:
    """策略验证报告（对应 QualityReport）

    三态语义（见 ``AssessmentState``）：

    - ``DECIDED``：真的跑过，数值可依赖；
    - ``UNASSESSED``：没跑成 / 没数据，``overall_score`` 与 ``passed`` 均为 ``None``。
      **下游不得把它当成 0 分或「干净」**，也不得拿它去比较阈值。

    构造时省略 ``state`` 会按 ``passed`` 推断：``passed is None`` → UNASSESSED。
    """

    def __init__(
        self,
        overall_score: Optional[float] = None,
        dimensions: Optional[Dict[str, Optional[float]]] = None,
        violations: Optional[List[Dict[str, Any]]] = None,
        passed: Optional[bool] = None,
        state: Optional[AssessmentState] = None,
    ):
        self.overall_score = overall_score
        self.state = state or (
            AssessmentState.DECIDED if passed is not None else AssessmentState.UNASSESSED
        )
        self.language_style_score = (dimensions or {}).get("language_style")
        self.character_consistency_score = (dimensions or {}).get("character_consistency")
        self.plot_density_score = (dimensions or {}).get("plot_density")
        self.naming_score = (dimensions or {}).get("naming")
        self.viewpoint_score = (dimensions or {}).get("viewpoint")
        self.rhythm_score = (dimensions or {}).get("rhythm")
        self.all_violations = violations or []
        self.passed = passed

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
            },
            "violation_count": len(self.all_violations),
            "violations": self.all_violations,
            "passed": self.passed,
            "state": self.state.value,
        }


class PolicyViolationError(Exception):
    """策略违规异常（对应 QualityViolationError）"""

    def __init__(self, violations: List[Dict[str, Any]], overall_score: float):
        self.violations = violations
        self.overall_score = overall_score
        message = f"策略验证不通过(评分{overall_score:.2f})，共{len(violations)}项违规"
        super().__init__(message)


class PolicyValidator:
    """策略验证器 — 对 QualityGuardrail 的薄封装

    保留 QualityGuardrail 的全部六维度检查能力，
    只调整接口命名：
    - enforce → enforce（不变，强制执行）
    - advise → advise（不变，建议模式）
    - check → check（不变，执行检查）

    使用方式：
        validator = PolicyValidator()
        report = validator.advise(text=content, character_masks=masks)
        if report.state is AssessmentState.UNASSESSED:
            print("护栏没跑成，本次没有结论")
        elif not report.passed:
            print(f"验证未通过: {report.all_violations}")
    """

    MIN_PASS_SCORE: float = 0.6

    def __init__(self, min_pass_score: float = 0.6):
        self._min_pass_score = min_pass_score
        self._guardrail = None

        # 尝试导入 QualityGuardrail
        try:
            from engine.runtime.quality_guardrails.quality_guardrail import QualityGuardrail
            self._guardrail = QualityGuardrail()
        except ImportError:
            logger.debug("QualityGuardrail 不可用，PolicyValidator 将返回 UNASSESSED 报告")

    def check(
        self,
        text: str,
        character_masks: Optional[Dict[str, Any]] = None,
        chapter_goal: str = "",
        character_names: Optional[List[str]] = None,
        scene_info: Optional[Dict[str, Any]] = None,
        foreshadows: Optional[List[Dict[str, Any]]] = None,
        era: str = "ancient",
        scene_type: str = "auto",
    ) -> PolicyReport:
        """执行六维度策略检查

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
            PolicyReport
        """
        if self._guardrail is not None:
            try:
                report = self._guardrail.check(
                    text=text,
                    character_masks=character_masks,
                    chapter_goal=chapter_goal,
                    character_names=character_names,
                    scene_info=scene_info,
                    foreshadows=foreshadows,
                    era=era,
                    scene_type=scene_type,
                )
                return PolicyReport(
                    overall_score=report.overall_score,
                    dimensions={
                        "language_style": report.language_style_score,
                        "character_consistency": report.character_consistency_score,
                        "plot_density": report.plot_density_score,
                        "naming": report.naming_score,
                        "viewpoint": report.viewpoint_score,
                        "rhythm": report.rhythm_score,
                    },
                    violations=report.all_violations,
                    passed=report.passed,
                    state=getattr(report, "state", None),
                )
            except Exception as e:
                logger.warning(f"QualityGuardrail.check 失败: {e}")

        # 降级：显式未评估，不得伪造 0.85 通过分
        return PolicyReport(state=AssessmentState.UNASSESSED)

    def enforce(
        self,
        text: str,
        character_masks: Optional[Dict[str, Any]] = None,
        chapter_goal: str = "",
        character_names: Optional[List[str]] = None,
        scene_info: Optional[Dict[str, Any]] = None,
        foreshadows: Optional[List[Dict[str, Any]]] = None,
        era: str = "ancient",
        scene_type: str = "auto",
        min_score: Optional[float] = None,
    ) -> PolicyReport:
        """强制执行策略检查（Checkpoint 保存前拦截）

        不达标则抛出 PolicyViolationError

        Returns:
            PolicyReport（通过时）

        Raises:
            PolicyViolationError: 策略不达标时
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

        threshold = min_score or self._min_pass_score
        if report.overall_score is None:
            # 未评估：无从比较阈值。返回带 UNASSESSED 的报告由调用方决定，
            # 不得用常量分顶替——那正是 F-03 的成因。
            logger.warning("策略验证未评估（UNASSESSED），跳过阈值比较")
            return report

        if report.overall_score < threshold:
            raise PolicyViolationError(
                violations=report.all_violations,
                overall_score=report.overall_score,
            )

        return report

    def advise(
        self,
        text: str,
        character_masks: Optional[Dict[str, Any]] = None,
        chapter_goal: str = "",
        character_names: Optional[List[str]] = None,
        scene_info: Optional[Dict[str, Any]] = None,
        foreshadows: Optional[List[Dict[str, Any]]] = None,
        era: str = "ancient",
        scene_type: str = "auto",
    ) -> PolicyReport:
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
