"""public_stage_name 行为测试。

映射表与提取前的 _public_stage 完全一致；本测试锁定该一致性。
"""
from application.core.dtos.novel_dto import public_stage_name


def test_planning_family_maps_to_planning():
    assert public_stage_name("planning") == "planning"
    assert public_stage_name("macro_planning") == "planning"
    assert public_stage_name("act_planning") == "planning"


def test_writing_and_auditing_map_to_public_values():
    assert public_stage_name("writing") == "writing"
    assert public_stage_name("auditing") == "reviewing"
    assert public_stage_name("reviewing") == "reviewing"
    assert public_stage_name("paused_for_review") == "reviewing"
    assert public_stage_name("completed") == "completed"


def test_unknown_current_stage_falls_back_to_planning():
    assert public_stage_name("something_new") == "planning"
    assert public_stage_name(None) == "planning"
    assert public_stage_name("") == "planning"


def test_explicit_stage_wins_when_current_is_empty_or_planning():
    assert public_stage_name("planning", "writing") == "writing"
    assert public_stage_name("", "writing") == "writing"
    # current 非空且非 planning 时不被覆盖：与提取前的 _public_stage 一致，映射为 reviewing。
    assert public_stage_name("auditing", "writing") == "reviewing"


def test_explicit_planning_does_not_override_current():
    assert public_stage_name("auditing", "planning") == "reviewing"


def test_accepts_enum_like_objects_with_value():
    class _Stage:
        value = "auditing"

    assert public_stage_name(_Stage()) == "reviewing"
