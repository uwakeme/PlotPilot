"""Foreshadowing 必须承载 viewpoint_guardrail._check_foreshadow_binding 需要的字段。

背景：校验条件是 emotional_weight > 0.8 and binding_level < 3，
而生产域模型原先只有 importance，导致接线后校验永远返回空。
"""
import pytest

from domain.novel.value_objects.foreshadowing import (
    Foreshadowing,
    ForeshadowingStatus,
    ImportanceLevel,
)


def _make(**overrides) -> Foreshadowing:
    base = dict(
        id="f1",
        planted_in_chapter=3,
        description="断魂崖下的残剑",
        importance=ImportanceLevel.HIGH,
        status=ForeshadowingStatus.PLANTED,
    )
    base.update(overrides)
    return Foreshadowing(**base)


def _make_registry():
    from domain.novel.entities.foreshadowing_registry import ForeshadowingRegistry
    from domain.novel.value_objects.novel_id import NovelId

    return ForeshadowingRegistry(id="reg1", novel_id=NovelId("novel-1"))


class TestForeshadowingBindingFields:
    def test_defaults_keep_backward_compat(self):
        f = _make()
        assert f.emotional_weight == 0.5
        assert f.binding_level == 1

    def test_accepts_core_foreshadow(self):
        f = _make(emotional_weight=0.95, binding_level=2)
        assert f.emotional_weight == 0.95
        assert f.binding_level == 2

    def test_satisfies_guardrail_core_condition(self):
        """构造一条能真正触发 weak_binding 违规的数据。"""
        f = _make(emotional_weight=0.92, binding_level=1)
        assert f.emotional_weight > 0.8 and f.binding_level < 3

    def test_rejects_out_of_range_weight(self):
        with pytest.raises(ValueError):
            _make(emotional_weight=1.5)

    def test_rejects_negative_weight(self):
        with pytest.raises(ValueError):
            _make(emotional_weight=-0.1)


class TestMapperRoundTrip:
    def test_binding_fields_survive_roundtrip(self):
        from infrastructure.persistence.mappers.foreshadowing_mapper import (
            ForeshadowingMapper,
        )

        registry = _make_registry()
        registry.register(_make(emotional_weight=0.92, binding_level=2))

        restored = ForeshadowingMapper.from_dict(ForeshadowingMapper.to_dict(registry))

        got = restored.foreshadowings[0]
        assert got.emotional_weight == 0.92
        assert got.binding_level == 2

    def test_legacy_payload_without_fields_gets_defaults(self):
        """旧库 JSON 没有这两个字段时必须回落默认值，不能抛错。"""
        from infrastructure.persistence.mappers.foreshadowing_mapper import (
            ForeshadowingMapper,
        )

        payload = {
            "id": "reg1",
            "novel_id": "novel-1",
            "foreshadowings": [
                {
                    "id": "f1",
                    "planted_in_chapter": 3,
                    "description": "旧伏笔",
                    "importance": 3,
                    "status": "planted",
                }
            ],
        }
        restored = ForeshadowingMapper.from_dict(payload)
        got = restored.foreshadowings[0]
        assert got.emotional_weight == 0.5
        assert got.binding_level == 1
