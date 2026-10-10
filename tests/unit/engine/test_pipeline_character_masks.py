"""角色一致性/命名/视角校验必须拿到真实角色数据。

背景：base.py 的 _get_character_masks / _get_character_names / _get_scene_context
曾恒返回空值，quality_guardrail.check() 对应的三处 if 守卫全部落空
（cc_score=0.73 / n_score=0.82 / vp_score=0.77），总分的这几项是写死的常量。

本测试覆盖两件事：
1. 三个取数方法确实委托给 guardrail_execution 里已有的 adapter，并在异常时降级；
2. 取到数据后，真护栏的分数确实会离开常量（否则接线等于没接）。
"""
from engine.core.value_objects.character_mask import CharacterMask
from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext
from engine.runtime.quality_guardrails.quality_guardrail import QualityGuardrail

MODULE = "application.engine.services.guardrail_execution"

TWO_CHARACTER_TEXT = "林渊握紧了剑，陈长老看着他。剑锋在雨里闪了一下，林渊退了半步。"


def _ctx(content: str = "") -> PipelineContext:
    ctx = PipelineContext(novel_id="novel-1", chapter_number=1)
    ctx.chapter_content = content
    return ctx


def _pipeline() -> BaseStoryPipeline:
    """绕过 __init__（需要完整依赖注入），只测这三个纯取数方法。"""
    return BaseStoryPipeline.__new__(BaseStoryPipeline)


def _mask(name: str) -> CharacterMask:
    return CharacterMask(character_id=f"id-{name}", name=name, core_belief="")


def _two_character_cast(_novel_id: str):
    return {"林渊": _mask("林渊"), "陈长老": _mask("陈长老")}


def _boom(_novel_id: str):
    raise RuntimeError("取数炸了")


class TestGetCharacterNames:
    def test_returns_characters_appearing_in_text(self, monkeypatch):
        monkeypatch.setattr(
            f"{MODULE}._character_masks_for_novel",
            lambda _nid: {"林渊": object(), "陈长老": object(), "路人甲": object()},
        )

        names = _pipeline()._get_character_names(_ctx("林渊握紧了剑，陈长老看着他。"))

        assert "林渊" in names
        assert "陈长老" in names
        assert "路人甲" not in names, "未登场角色不应送进命名校验"

    def test_returns_empty_when_cast_unavailable(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}._character_masks_for_novel", lambda _nid: {})

        assert _pipeline()._get_character_names(_ctx("正文")) == []

    def test_empty_when_no_novel_id(self):
        ctx = PipelineContext(novel_id="", chapter_number=1)

        assert _pipeline()._get_character_names(ctx) == []

    def test_adapter_error_degrades_without_raising(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}._character_names_fallback_from_text", _boom)

        assert _pipeline()._get_character_names(_ctx("正文")) == []


class TestGetCharacterMasks:
    def test_delegates_to_existing_adapter(self, monkeypatch):
        sentinel = {"林渊": object()}
        monkeypatch.setattr(
            f"{MODULE}._character_masks_for_novel", lambda _nid: sentinel
        )

        assert _pipeline()._get_character_masks(_ctx()) is sentinel

    def test_empty_when_no_novel_id(self):
        ctx = PipelineContext(novel_id="", chapter_number=1)

        assert _pipeline()._get_character_masks(ctx) == {}

    def test_adapter_error_degrades_without_raising(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}._character_masks_for_novel", _boom)

        assert _pipeline()._get_character_masks(_ctx()) == {}


class TestGetSceneContext:
    def test_builds_context_when_two_characters_co_appear(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}._character_masks_for_novel", _two_character_cast)

        scene = _pipeline()._get_scene_context(_ctx(TWO_CHARACTER_TEXT))

        assert scene is not None
        assert set(scene["characters_present"]) == {"林渊", "陈长老"}
        assert scene["viewpoint_character"] in scene["characters_present"]

    def test_none_when_only_one_character_appears(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}._character_masks_for_novel", _two_character_cast)

        assert _pipeline()._get_scene_context(_ctx("林渊独自走在雨里。")) is None

    def test_none_when_no_novel_id(self):
        ctx = PipelineContext(novel_id="", chapter_number=1)

        assert _pipeline()._get_scene_context(ctx) is None

    def test_adapter_error_degrades_without_raising(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}._minimal_scene_context", _boom)

        assert _pipeline()._get_scene_context(_ctx(TWO_CHARACTER_TEXT)) is None


class TestGuardrailScoresLeaveConstants:
    """接线是否真的有意义：拿到数据后分数必须离开常量。"""

    def test_constants_when_nothing_supplied(self):
        """对照组：不传三项，三维仍是写死的常量。"""
        report = QualityGuardrail().advise(text=TWO_CHARACTER_TEXT, era="ancient")

        assert report.character_consistency_score == 0.73
        assert report.naming_score == 0.82
        assert report.viewpoint_score == 0.77

    def test_scores_move_once_pipeline_supplies_data(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}._character_masks_for_novel", _two_character_cast)
        pipeline = _pipeline()
        ctx = _ctx(TWO_CHARACTER_TEXT)

        report = QualityGuardrail().advise(
            text=ctx.chapter_content,
            character_masks=pipeline._get_character_masks(ctx),
            character_names=pipeline._get_character_names(ctx),
            scene_info=pipeline._get_scene_context(ctx),
            era=ctx.era,
        )

        assert report.character_consistency_score != 0.73, "角色一致性仍在吃常量"
        assert report.naming_score != 0.82, "命名维度仍在吃常量"
        assert report.viewpoint_score != 0.77, "视角维度仍在吃常量"
