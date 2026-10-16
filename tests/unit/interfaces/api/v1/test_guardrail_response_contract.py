"""护栏 API 契约：可空，但必填。

背景（登记表 F-03 / F-09）：
v3 起护栏用 ``None`` 表达「该维度未评估」，``overall_score`` / ``passed``
也可以是 ``None``。但**可空不等于可省略**——如果给这些字段配上默认值
（``Optional[float] = None``），上游漏填时会静默序列化成 ``null``，
那正是本改造要消灭的「缺数据伪装成值」，只是从数值换成了 null。

因此：字段可空、但必须出现在 payload 里；漏填应当 422。

唯一例外是 ``state``——它是本轮后加的字段，历史快照里根本没有，
保持默认值否则旧快照读不回来（见 ``chapters.py`` 的 guardrail-snapshot 端点）。
"""
import pytest
from pydantic import ValidationError

from interfaces.api.v1.engine.checkpoint_routes import (
    GuardrailCheckResponse,
    GuardrailDimensionScore,
)


def _payload(**overrides):
    base = {
        "overall_score": None,
        "passed": None,
        "state": "unassessed",
        "dimensions": [
            {"name": "语言风格", "key": "language_style", "score": 0.83, "weight": 0.23},
            {"name": "角色一致性", "key": "character_consistency", "score": None, "weight": 0.23},
        ],
        "violations": [],
    }
    base.update(overrides)
    return base


def test_null_scores_are_accepted():
    """可空：未评估维度传 None 必须能通过校验（否则单角色章节直接 422）。"""
    dto = GuardrailCheckResponse.model_validate(_payload())

    assert dto.overall_score is None
    assert dto.passed is None
    assert dto.state == "unassessed"
    assert dto.dimensions[1].score is None


def test_missing_overall_score_is_rejected():
    """必填：整个字段缺失必须 422，而不是静默变 null。"""
    payload = _payload()
    payload.pop("overall_score")

    with pytest.raises(ValidationError):
        GuardrailCheckResponse.model_validate(payload)


def test_missing_passed_is_rejected():
    payload = _payload()
    payload.pop("passed")

    with pytest.raises(ValidationError):
        GuardrailCheckResponse.model_validate(payload)


def test_missing_dimension_score_is_rejected():
    """维度分同样必填：漏填不得静默变 null（null 只表示"未评估"）。"""
    with pytest.raises(ValidationError):
        GuardrailDimensionScore.model_validate(
            {"name": "角色一致性", "key": "character_consistency", "weight": 0.23}
        )


def test_state_may_be_absent_for_legacy_snapshots():
    """state 是后加字段，历史快照没有它——必须仍能读回来。"""
    payload = _payload()
    payload.pop("state")

    dto = GuardrailCheckResponse.model_validate(payload)

    assert dto.state is None
    assert dto.overall_score is None


def test_snapshot_dict_from_unassessed_report_is_accepted():
    """端到端契约：护栏未评估时产出的 dict 能直接过 response 校验。"""
    from application.engine.services.guardrail_execution import (
        quality_report_to_snapshot_dict,
    )
    from engine.runtime.quality_guardrails.quality_guardrail import QualityReport

    report = QualityReport(
        overall_score=None,
        language_style_score=None,
        all_violations=[],
        passed=None,
        state=None,
    )

    dto = GuardrailCheckResponse.model_validate(quality_report_to_snapshot_dict(report))

    assert dto.overall_score is None
    assert dto.passed is None
    assert dto.state == "unassessed"
