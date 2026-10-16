"""PipelineContext / PipelineResult 的三态类型契约。

背景（登记表 F-03 下半）：`895803ce` 修掉了假分数，但三态仍只是注释约定——
`validation_state` 在三条写入路径上并存两种运行时类型（裸 str 与枚举成员）。
因为 AssessmentState 是 str, Enum，`== "unassessed"` 三条路径都真，测试看不出问题；
但 `is` 比较只有降级路径为真。这是静默地雷。

这组用例全部用 `is` 而非 `==`，正是为了把「类型不统一」暴露成红灯。
"""
import pytest

from domain.ai.value_objects.assessment_state import AssessmentState
from engine.pipeline.context import PipelineContext, PipelineResult


class TestValidationStateIsAlwaysTheEnumMember:
    def test_default_is_enum_member_not_raw_string(self):
        """从未赋值的默认值也必须是枚举成员，不能是裸 'unassessed'。"""
        ctx = PipelineContext(novel_id="n1", chapter_number=1)
        assert ctx.validation_state is AssessmentState.UNASSESSED

    def test_pipeline_result_default_is_enum_member(self):
        outcome = PipelineResult()
        assert outcome.validation_state is AssessmentState.UNASSESSED

    def test_assignment_normalises_raw_string(self):
        """外部赋裸字符串时应收敛回枚举，而不是把裸 str 存进去。"""
        ctx = PipelineContext(novel_id="n1", chapter_number=1)
        ctx.validation_state = "decided"          # 故意用裸串
        assert ctx.validation_state is AssessmentState.DECIDED

    def test_declared_type_is_the_enum(self):
        """声明类型本身必须是 AssessmentState，不能是 str。

        本模块有 `from __future__ import annotations`，所以
        `__dataclass_fields__[...].type` 拿到的是注解**字符串**而不是类对象，
        断言 `is AssessmentState` 恒假；能成立的是「不再是原声明的 "str"」。
        """
        hints = PipelineContext.__dataclass_fields__["validation_state"].type
        assert hints != "str", "validation_state 声明为 str 等于放弃类型保证"
        assert hints == "AssessmentState"

    def test_illegal_state_value_is_rejected(self):
        """非法三态值必须抛错，而不是把垃圾字符串存进字段。"""
        ctx = PipelineContext(novel_id="n1", chapter_number=1)
        with pytest.raises(ValueError):
            ctx.validation_state = "garbage"
