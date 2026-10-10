"""角色一致性/命名校验必须拿到真实角色数据。

背景：base.py 的 _get_character_masks/_get_character_names 恒返回 {} / []，
六维中三维短路为常量，加权后 28.59% 的总分是写死的。
"""
from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext

MODULE = "application.engine.services.guardrail_execution"


def _ctx(content: str = "") -> PipelineContext:
    ctx = PipelineContext(novel_id="novel-1", chapter_number=1)
    ctx.chapter_content = content
    return ctx


class TestGetCharacterNames:
    def test_returns_characters_appearing_in_text(self, monkeypatch):
        monkeypatch.setattr(
            f"{MODULE}._character_masks_for_novel",
            lambda _nid: {"林渊": object(), "陈长老": object(), "路人甲": object()},
        )
        pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)

        names = pipeline._get_character_names(_ctx("林渊握紧了剑，陈长老看着他。"))

        assert "林渊" in names
        assert "陈长老" in names
        assert "路人甲" not in names, "未登场角色不应送进命名校验"

    def test_returns_empty_when_cast_unavailable(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}._character_masks_for_novel", lambda _nid: {})
        pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)

        assert pipeline._get_character_names(_ctx("正文")) == []

    def test_empty_when_no_novel_id(self):
        pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)
        ctx = PipelineContext(novel_id="", chapter_number=1)
        assert pipeline._get_character_names(ctx) == []


class TestGetCharacterMasks:
    def test_delegates_to_existing_adapter(self, monkeypatch):
        sentinel = {"林渊": object()}
        monkeypatch.setattr(
            f"{MODULE}._character_masks_for_novel", lambda _nid: sentinel
        )
        pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)

        assert pipeline._get_character_masks(_ctx()) is sentinel

    def test_empty_when_no_novel_id(self):
        pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)
        ctx = PipelineContext(novel_id="", chapter_number=1)
        assert pipeline._get_character_masks(ctx) == {}
