"""禁忌检测不得是空壳；护栏分数必须真的被看见。

登记表 F-02 / F-01。

F-02（``engine/runtime/quality_guardrails/character_consistency_guardrail.py``）：
``_detect_taboo_violation`` 曾是 ``return False`` 的静默空壳，
而 ``moral_taboos`` 早已作为指引塞进提示词 —— 于是「禁忌到了模型、
事后没人验证」这条链上唯一的执法点从不执法，``severity=0.9``
（本护栏最高档）的禁忌违规一次都没产生过。
作者「宁可漏报不误报」的取舍本身合理：要修的是**静默空壳**，
不是把它改成乱报的语义判断器。

F-01（``engine/pipeline/base.py``）：``validation_score`` 只写不读，
一章得 0.1 与得 1.0 待遇完全相同。按本轮原则
「AI 决定『是什么』，代码只保证『不越界』」，护栏分数不该自动决定章节生死；
它该被**记录并暴露**出来，消费点在 /status 与审计侧。

本文件刻意**不使用** ``inspect.getsource`` 做源码断言 —— 那种断言只能
证明源码里没这几个字符，证明不了行为。全部为行为测试。

F-02 第 ④ 条断言 docstring 是例外：它锁的是**文档契约**
（已知局限必须写在代码里），不是源码文本的字符长相。
"""
import logging

import pytest

from domain.ai.value_objects.assessment_state import AssessmentState
from engine.core.value_objects.character_mask import CharacterMask
from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext
from engine.runtime.quality_guardrails.character_consistency_guardrail import (
    CharacterConsistencyGuardrail,
)
from engine.runtime.quality_guardrails.quality_guardrail import QualityReport


# ═══════════════════════════════════════════════════════════════
# F-02：禁忌检测
# ═══════════════════════════════════════════════════════════════

TABOO_NAME = "林渊"
TABOO_TEXT_HIT = "林渊提剑冲入村寨，滥杀无辜者数十人，血流漂橹。"
TABOO_TEXT_GENERIC = "他在乱军中杀了很多人，又死于一场大火，死无全尸。"
TABOO_TEXT_UNRELATED = "林渊在山巅打坐，晨雾漫过松林，露水打湿了衣袖。"


def _taboo_mask(taboo: str = "滥杀无辜") -> CharacterMask:
    return CharacterMask(
        character_id="id-linyu",
        name=TABOO_NAME,
        core_belief="",
        moral_taboos=[taboo],
    )


class TestTabooDetectionIsNotAStub:
    """F-02：命中 / 不误报 / 泛化词不触发。"""

    def test_shared_discriminating_root_is_detected(self):
        """① 禁忌与正文共享有区分度的词根时，真被检出。

        红的原因必须是业务事实（禁忌未被检出），不能是异常。
        """
        assert CharacterConsistencyGuardrail()._detect_taboo_violation(
            TABOO_TEXT_HIT, TABOO_NAME, "滥杀无辜"
        ) is True

    def test_unrelated_text_is_not_a_violation(self):
        """② 完全无关的正文不误报 —— 锁住「宁可漏报不误报」这条取舍。"""
        assert CharacterConsistencyGuardrail()._detect_taboo_violation(
            TABOO_TEXT_UNRELATED, TABOO_NAME, "滥杀无辜"
        ) is False

    def test_generic_single_chars_do_not_trigger(self):
        """③ 泛化词「杀」「死」不得让「滥杀无辜」命中。

        这条防「为了提高召回去乱匹配」。它同时锁住实现不许退化成
        单字包含判断，也不许靠泛化动词凑阈值。
        """
        assert CharacterConsistencyGuardrail()._detect_taboo_violation(
            TABOO_TEXT_GENERIC, TABOO_NAME, "滥杀无辜"
        ) is False

    def test_known_limitation_is_declared_in_docstring(self):
        """④ 局限必须写在代码里：这是文档契约，不是源码文本断言。

        未命中 ≠ 没有违规，只等于没有检测到词面证据。
        """
        doc = CharacterConsistencyGuardrail._detect_taboo_violation.__doc__ or ""

        assert "词面" in doc, "必须声明本检查是表层词面匹配"
        assert "未命中" in doc and "不等于" in doc, (
            "必须写明「未命中不等于没有违规」"
        )
        assert "没有违规" in doc


class TestTabooViolationReachesTheReport:
    """⑤ 端到端：``_check_ooc`` 命中时真的 append severity=0.9 的违规。"""

    def test_check_ooc_appends_severity_09_violation(self):
        guardrail = CharacterConsistencyGuardrail()

        violations = guardrail._check_ooc(TABOO_TEXT_HIT, _taboo_mask())

        taboo_hits = [v for v in violations if "绝对禁忌" in v.description]
        assert taboo_hits, "命中时必须产出禁忌违规"
        assert taboo_hits[0].severity == pytest.approx(0.9)
        assert taboo_hits[0].violation_type == "ooc"
        assert taboo_hits[0].character_name == TABOO_NAME

    def test_check_score_drops_when_taboo_hit(self):
        """分数必须真的因为禁忌被检出而变化（否则执法点等于没接）。"""
        guardrail = CharacterConsistencyGuardrail()

        hit_score, hit_violations = guardrail.check(
            TABOO_TEXT_HIT, {TABOO_NAME: _taboo_mask()}
        )
        clean_score, clean_violations = guardrail.check(
            TABOO_TEXT_UNRELATED, {TABOO_NAME: _taboo_mask()}
        )

        assert any("绝对禁忌" in v.description for v in hit_violations)
        assert hit_score < clean_score


# ═══════════════════════════════════════════════════════════════
# F-01：护栏分数被消费
# ═══════════════════════════════════════════════════════════════

LOW_SCORE_TEXT = "林渊握紧了剑，剑锋在雨里闪了一下。* 40"


def _pipeline() -> BaseStoryPipeline:
    """绕过 __init__（需要完整依赖注入），只测接线本身。"""
    pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)
    pipeline._step_log = []
    return pipeline


def _ctx(content: str = LOW_SCORE_TEXT) -> PipelineContext:
    ctx = PipelineContext(novel_id="novel-1", chapter_number=7)
    ctx.chapter_content = content
    return ctx


class _FixedValidator:
    """返回固定分数的校验器桩。"""

    def __init__(self, score: float, passed: bool = True):
        self._score = score
        self._passed = passed

    def advise(self, **kwargs):
        return QualityReport(
            overall_score=self._score,
            passed=self._passed,
            all_violations=[],
        )


class _RecordingSink:
    """``writing_progress_sink`` 的记录桩：管线既有的可观测通道。"""

    def __init__(self):
        self.calls = []

    def __call__(self, substep, label, extra):
        self.calls.append((substep, label, dict(extra)))

    def payloads(self):
        return [extra for _, _, extra in self.calls]

    def with_validation_fields(self):
        return [p for p in self.payloads() if "validation_state" in p]

    def last_validation_payload(self) -> dict:
        """取最后一次校验回执；没有就报业务原因，而不是 IndexError。"""
        reported = self.with_validation_fields()
        assert reported, (
            "校验结果必须推到既有可观测通道（writing_progress_sink），"
            "否则 validation_score 只写不读，无人消费"
        )
        return reported[-1]


class TestGuardrailScoreIsConsumed:
    """⑥ 分数被消费：断言承载点里能读到分数与状态。"""

    async def test_score_reaches_writing_progress_channel(self):
        sink = _RecordingSink()
        ctx = _ctx()
        ctx.writing_progress_sink = sink
        ctx.policy_validator = _FixedValidator(0.42)

        await _pipeline()._step_validate_content(ctx)

        payload = sink.last_validation_payload()
        assert payload["validation_state"] == "decided"
        assert payload["validation_score"] == pytest.approx(0.42)

    async def test_low_score_is_visible_in_the_human_readable_label(self):
        """分数必须真的**看得见**，而不只是躺在某个没人读的字典里。

        label 是 /status 已透出的字段（``writing_substep_label``），
        所以它出现在 label 里 == 前端与运维直接能读到。
        """
        sink = _RecordingSink()
        ctx = _ctx()
        ctx.writing_progress_sink = sink
        ctx.policy_validator = _FixedValidator(0.42, passed=False)

        await _pipeline()._step_validate_content(ctx)

        labels = [label for _, label, _ in sink.calls]
        assert any("0.42" in label for label in labels), (
            f"低分必须出现在人可读标签里，实际收到: {labels}"
        )


class TestGuardrailScoreDoesNotAdjudicate:
    """⑦ 反向守卫：分数不得变成自动判决。"""

    async def test_healthy_decided_path_produces_no_extra_action(self, caplog):
        """正常 DECIDED 路径除了「记录」之外不产生任何额外动作。

        防的是把 F-01 修成「低分章节自动失败」——那是越权判决。
        """
        sink = _RecordingSink()
        ctx = _ctx()
        ctx.writing_progress_sink = sink
        ctx.policy_validator = _FixedValidator(0.93)

        with caplog.at_level(logging.WARNING):
            result = await _pipeline()._step_validate_content(ctx)

        assert result.passed is True, "护栏不该阻断章节"
        assert result.violations == []
        assert ctx.validation_passed is True
        assert ctx.generation_interrupted is False
        assert ctx.chapter_content == LOW_SCORE_TEXT, "正文不得被改动"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
            "正常路径不得告警"
        )
        # 分数仍然被记录下来 —— 只是不再额外做任何事
        assert sink.last_validation_payload()["validation_score"] == pytest.approx(0.93)

    async def test_low_score_still_does_not_fail_the_chapter(self):
        """低分只被暴露，不被判死：AI 决定『是什么』，代码只保证不越界。"""
        ctx = _ctx()
        ctx.writing_progress_sink = _RecordingSink()
        ctx.policy_validator = _FixedValidator(0.11, passed=False)

        result = await _pipeline()._step_validate_content(ctx)

        assert result.passed is True, "低分不得让章节失败"
        assert ctx.validation_passed is False, "但必须诚实记下『未通过』"
        assert ctx.validation_score == pytest.approx(0.11)


class TestUnassessedStillCarriesNoScore:
    """未评估不得顺手带出一个数字（与 F-03 三态保持一致）。"""

    async def test_unassessed_publishes_state_without_score(self):
        class _NoConclusion:
            def advise(self, **kwargs):
                return QualityReport(
                    overall_score=None,
                    passed=None,
                    all_violations=[],
                    state=AssessmentState.UNASSESSED,
                )

        sink = _RecordingSink()
        ctx = _ctx()
        ctx.writing_progress_sink = sink
        ctx.policy_validator = _NoConclusion()

        await _pipeline()._step_validate_content(ctx)

        payload = sink.last_validation_payload()
        assert payload["validation_state"] == "unassessed"
        assert "validation_score" not in payload, (
            "未评估不得顺手带出分数 —— None 不是 0，但也不能假装有"
        )
