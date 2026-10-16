"""角色一致性/命名/视角校验必须拿到真实角色数据。

背景：base.py 的 _get_character_masks / _get_character_names / _get_scene_context
曾恒返回空值，quality_guardrail.check() 对应的三处 if 守卫全部落空
（cc_score=0.73 / n_score=0.82 / vp_score=0.77），总分的这几项是写死的常量。

本测试覆盖四件事：
1. 三个取数方法确实委托给 guardrail_execution 里已有的 adapter，并在异常时降级；
2. adapter 能区分「取数失败」(None) 与「cast 确实为空」({})，且失败会记 warning；
3. base.py 的 advise() 调用点真的把 scene_info 送了出去；
4. 取到数据后，真护栏的分数确实会离开常量（否则接线等于没接）。
"""
import logging

import pytest

from application.engine.services.guardrail_execution import _character_masks_for_novel
from engine.core.value_objects.character_mask import CharacterMask
from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext
from engine.runtime.quality_guardrails.quality_guardrail import (
    QualityGuardrail,
    QualityReport,
)

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

    def test_missing_inputs_yield_none_not_constants(self):
        """对照组：不传三项，那三维必须是无结论（None），不是写死的常量。

        登记表 F-09：常量分让「什么都没评」照样凑得出高于 MIN_PASS_SCORE 的
        总分，下游因此分不清「检查通过」与「根本没跑成」。
        """
        report = QualityGuardrail().advise(text=TWO_CHARACTER_TEXT, era="ancient")

        assert report.character_consistency_score is None
        assert report.naming_score is None
        assert report.viewpoint_score is None
        assert report.unevaluated_dimensions == [
            "character_consistency", "naming", "viewpoint",
        ]

    def test_scores_move_once_pipeline_supplies_data(self, monkeypatch):
        """接线后三维分数的具体期望值。

        断言具体值而非 ``!=``：若有人把 0.73 换成另一个硬编码常量，
        ``!=`` 仍然会通过，这里会红。
        """
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

        assert report.character_consistency_score == pytest.approx(0.876)
        assert report.naming_score == pytest.approx(0.92)
        assert report.viewpoint_score == pytest.approx(1.0)

    def test_masks_actually_reach_guardrail(self, monkeypatch):
        """语义证据：只有拿到 masks，护栏才会产出角色一致性维度的违规。

        比分数断言更耐调参——分数怎么调这条都成立；而它只在 masks 真的送到
        护栏手里时才成立，删掉接线即红。用 dimension 判定而不依赖违规文案，
        避免护栏改名导致误红。
        """
        monkeypatch.setattr(f"{MODULE}._character_masks_for_novel", _two_character_cast)
        pipeline = _pipeline()
        ctx = _ctx(TWO_CHARACTER_TEXT)

        with_masks = QualityGuardrail().advise(
            text=ctx.chapter_content,
            character_masks=pipeline._get_character_masks(ctx),
            era=ctx.era,
        )
        without_masks = QualityGuardrail().advise(text=ctx.chapter_content, era=ctx.era)

        def _cc(report):
            return [v for v in report.all_violations if v["dimension"] == "character_consistency"]

        assert _cc(with_masks), "送入 masks 后仍无角色一致性违规，说明没真正生效"
        assert not _cc(without_masks), "未送入 masks 时不应有该维度违规（对照组）"


class TestAdapterDistinguishesFailureFromEmptyCast:
    """Finding 1：真实取数失败必须可观测，且不再与「本书无 cast」同形。"""

    def test_returns_none_and_warns_when_cast_service_breaks(self, monkeypatch, caplog):
        import interfaces.api.dependencies as deps

        def _broken_cast_graph(_novel_id):
            raise RuntimeError("cast 服务炸了")

        monkeypatch.setattr(deps, "get_cast_service", lambda: type(
            "S", (), {"get_cast_graph": staticmethod(_broken_cast_graph)}
        )())

        with caplog.at_level(logging.WARNING):
            result = _character_masks_for_novel("novel-1")

        assert result is None, "取数失败必须返回 None，不能伪装成空 cast"
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert "novel-1" in caplog.text

    def test_returns_empty_dict_when_cast_is_genuinely_empty(self, monkeypatch, caplog):
        import interfaces.api.dependencies as deps

        monkeypatch.setattr(deps, "get_cast_service", lambda: type(
            "S", (), {"get_cast_graph": staticmethod(
                lambda _nid: type("G", (), {"characters": []})()
            )}
        )())

        with caplog.at_level(logging.WARNING):
            result = _character_masks_for_novel("novel-1")

        assert result == {}, "cast 确实为空是成功路径，返回 {} 且不应告警"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_api_entry_still_normalizes_to_none(self, monkeypatch):
        """API 路径 run_guardrail_sync 的 `or None` 归一化不受返回类型变化影响。"""
        import interfaces.api.dependencies as deps

        monkeypatch.setattr(deps, "get_cast_service", lambda: type(
            "S", (), {"get_cast_graph": staticmethod(
                lambda _nid: type("G", (), {"characters": []})()
            )}
        )())

        # 空 cast -> _character_masks_for_novel 返回 {} -> `or None` -> None 传入护栏
        assert (_character_masks_for_novel("novel-1") or None) is None


class TestValidateContentPassesSceneInfo:
    """Issue 2：base.py 的 advise() 调用点必须把 scene_info 送出去。

    删掉 `scene_info=self._get_scene_context(ctx)` 这行，本测试即红。
    """

    async def test_advise_receives_scene_info(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}._character_masks_for_novel", _two_character_cast)
        pipeline = _pipeline()
        pipeline._step_log = []
        ctx = _ctx(TWO_CHARACTER_TEXT)
        captured = {}

        class _CapturingValidator:
            def advise(self, **kwargs):
                captured.update(kwargs)
                return QualityReport(overall_score=0.9, passed=True, all_violations=[])

        ctx.policy_validator = _CapturingValidator()

        await pipeline._step_validate_content(ctx)

        assert captured.get("scene_info") is not None, "scene_info 未送达 advise()"
        assert set(captured["scene_info"]["characters_present"]) == {"林渊", "陈长老"}
        assert captured["scene_info"]["viewpoint_character"] in {"林渊", "陈长老"}
        assert set(captured["character_masks"]) == {"林渊", "陈长老"}
        assert set(captured["character_names"]) == {"林渊", "陈长老"}

