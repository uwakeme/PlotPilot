# tests/unit/domain/ai/test_assessment_state.py
"""三态值对象的契约。

背景：本项目多处降级路径返回「看起来正常」的值（假 0.85 分、真力 50 分、
假 0.0 干净、假「未提取」落库），下游无法区分「检查通过」与「根本没跑成」。
本类型是收口这些路径的共享词汇。
"""
from domain.ai.value_objects.assessment_state import AssessmentState


def test_three_states_exist():
    assert {s.name for s in AssessmentState} == {"DECIDED", "UNASSESSED", "BLOCKED"}


def test_wire_values_are_stable():
    # 值会进 JSON 落库与日志，改名等于改数据契约
    assert AssessmentState.DECIDED.value == "decided"
    assert AssessmentState.UNASSESSED.value == "unassessed"
    assert AssessmentState.BLOCKED.value == "blocked"


def test_compares_equal_to_bare_string():
    # 下游常写 `state == "unassessed"`；str 混入必须成立
    assert AssessmentState.UNASSESSED == "unassessed"
    assert AssessmentState.UNASSESSED.value == "unassessed"
