"""_step_validate_content 必须把 scene_info / foreshadows 喂给校验器。

背景：quality_guardrail.py:213 的 `if scene_info:` 分支在生产中永不进入，
伏笔绑定层级校验（viewpoint_guardrail._check_foreshadow_binding）因此从未跑过。

本测试用真实的 ForeshadowingRegistry / Foreshadowing（而非 SimpleNamespace 桩），
以便同时覆盖「只取未回收伏笔」这条过滤规则。
"""
import logging
from types import SimpleNamespace

from domain.novel.entities.foreshadowing_registry import ForeshadowingRegistry
from domain.novel.value_objects.foreshadowing import (
    Foreshadowing,
    ForeshadowingStatus,
    ImportanceLevel,
)
from domain.novel.value_objects.novel_id import NovelId
from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext

OUTLINE = "林渊在断魂崖遭遇陈长老伏击"
TEXT = "正文内容，长度需超过节奏检查的最小阈值。" * 5


class RecordingValidator:
    def __init__(self):
        self.calls = []

    def advise(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            passed=True, overall_score=0.9, all_violations=[],
            language_style_score=0.9, character_consistency_score=0.9,
            plot_density_score=0.9, naming_score=0.9,
            viewpoint_score=0.9, rhythm_score=0.9,
        )


def _pipeline() -> BaseStoryPipeline:
    """绕过 __init__（需要完整依赖注入），只测接线本身。"""
    pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)
    pipeline._step_log = []
    return pipeline


def _registry() -> ForeshadowingRegistry:
    registry = ForeshadowingRegistry(id="reg1", novel_id=NovelId("novel-1"))
    registry.register(Foreshadowing(
        id="f-planted",
        planted_in_chapter=3,
        description="断魂崖下的残剑",
        importance=ImportanceLevel.HIGH,
        status=ForeshadowingStatus.PLANTED,
        emotional_weight=0.92,
        binding_level=1,
    ))
    registry.register(Foreshadowing(
        id="f-resolved",
        planted_in_chapter=2,
        description="已回收的旧线索",
        importance=ImportanceLevel.MEDIUM,
        status=ForeshadowingStatus.RESOLVED,
        resolved_in_chapter=5,
    ))
    return registry


def _ctx() -> PipelineContext:
    ctx = PipelineContext(novel_id="novel-1", chapter_number=7)
    ctx.outline = OUTLINE
    ctx.chapter_content = TEXT
    ctx.era = "ancient"
    registry = _registry()
    ctx.foreshadowing_repository = SimpleNamespace(
        get_by_novel_id=lambda _nid: registry
    )
    return ctx


class TestValidateContentFeedsGuardrails:
    async def test_passes_core_foreshadow(self):
        pipeline = _pipeline()
        validator = RecordingValidator()
        ctx = _ctx()
        ctx.policy_validator = validator

        await pipeline._step_validate_content(ctx)

        assert len(validator.calls) == 1
        call = validator.calls[0]
        assert call["foreshadows"], "foreshadows 不得为空"
        assert len(call["foreshadows"]) == 1, "已回收伏笔不应再送进绑定校验"
        assert call["foreshadows"][0]["description"] == "断魂崖下的残剑"
        assert call["foreshadows"][0]["emotional_weight"] == 0.92
        assert call["foreshadows"][0]["binding_level"] == 1
        assert call["scene_info"], "scene_info 不得为空"
        assert call["scene_info"]["key_event"]

    async def test_degrades_when_repo_absent(self):
        pipeline = _pipeline()
        validator = RecordingValidator()
        ctx = _ctx()
        ctx.foreshadowing_repository = None
        ctx.policy_validator = validator

        result = await pipeline._step_validate_content(ctx)

        assert result.passed is True
        assert validator.calls[0]["foreshadows"] == []

    async def test_repo_failure_degrades_and_warns(self, caplog):
        """取数失败不能无声无息——否则护栏静默失效且无人察觉。

        这条覆盖的是吞掉"桩/实现不匹配"的那条 except 分支：仓储接口一变
        （例如 get_by_novel_id 被改名），若只降级不告警，绑定校验会静默消失。
        """
        pipeline = _pipeline()
        validator = RecordingValidator()
        ctx = _ctx()

        def _boom(_nid):
            raise RuntimeError("仓储炸了")

        ctx.foreshadowing_repository = SimpleNamespace(get_by_novel_id=_boom)
        ctx.policy_validator = validator

        with caplog.at_level(logging.WARNING):
            result = await pipeline._step_validate_content(ctx)

        assert result.passed is True, "取数失败不应让整章失败"
        assert validator.calls[0]["foreshadows"] == []
        assert any(r.levelno >= logging.WARNING for r in caplog.records), "取数失败必须告警"
        assert "novel-1" in caplog.text
