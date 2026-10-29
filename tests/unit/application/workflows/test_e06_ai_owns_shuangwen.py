"""E-06（第 53 批）：AI 说了算「本章怎么写」⇒ 不再叠加代码写死的前三章爽文原则。

背景
----
E-06 的「节奏配额」那一半早就由 AI 接管
（``RhythmQuotaPlanner`` ＋ CPMS 节点 ``planning-rhythm-quota``），
AI 给的 ``instruction``（「这一章具体怎么写」）**已经**在正文提示词的
``rhythm_quota`` 段里。

⇒ 但 ``_build_shuangwen_directive`` 还会把**代码写死的**
「前三章爽文五原则」叠加上去 ⇒ **两个互相打架的指令**
⇒ 模型同时看到「AI 说本章这样写」＋「代码说本章必须那样写」
⇒ 等于**AI 没决定**（这正是登记表 E-06 说的「节奏观被代码代答」）。

本批的取舍
----------
AI 的配额**可用**（``source == "ai"``）⇒ **不再叠加**代码块；
AI 不可用（默认：``rhythm_quota.enabled`` 关着 ⇒ 回落代码表）
⇒ **仍用代码块** ⇒ **行为逐字一致**。

本文件钉住
----------
1. AI 的指令在场 ⇒ 爽文约束块**不含**「首章爽文原则」；
2. AI 不可用 ⇒ 爽文约束块**仍含**它（回落逐位一致）；
3. 只有 ``source == "ai"`` 才算「AI 说了怎么写」（代码表回落 / 未评估 ⇒ 空串）；
4. **接线守卫**：payload 那一步真的把 AI 的指令记下来了（否则 1/2 会一起失效）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.workflows.auto_novel_generation_workflow import (
    AutoNovelGenerationWorkflow,
)


def _workflow(ai_instruction: str = "") -> AutoNovelGenerationWorkflow:
    """只造出 ``_build_shuangwen_directive`` 需要的最小对象。"""
    wf = AutoNovelGenerationWorkflow.__new__(AutoNovelGenerationWorkflow)
    wf._ai_quota_instruction = ai_instruction
    return wf


def _directive(wf: AutoNovelGenerationWorkflow, chapter_number: int) -> str:
    return wf._build_shuangwen_directive(
        chapter_number=chapter_number,
        beat_mode=False,
        beat_index=None,
        total_beats=None,
        beat_prompt="",
        outline="",
    )


# ─────────────────────── 1/2) 谁说了算 ───────────────────────


def test_the_ai_instruction_replaces_the_code_written_early_chapter_rules():
    """AI 说了「本章怎么写」⇒ 代码写死的前三章爽文原则**不许**再叠加。"""
    from application.engine.services.rhythm_quota import SOURCE_AI

    wf = _workflow()
    # ⚠ 走**真实记录路径**（不是手填属性）⇒ 记录器与爽文块一起被测到。
    wf._ai_quota_instruction = wf._record_ai_quota_instruction(
        SimpleNamespace(
            source=SOURCE_AI,
            instruction="综合张力目标：≥70。本章让对手先当众轻视主角，主角用一次实力验证反打。",
        )
    )

    text = _directive(wf, 1)

    assert "首章爽文原则" not in text, (
        "AI 已经给了「本章怎么写」，代码写死的前三章原则还在 ⇒ 两个打架的指令"
    )


def test_without_the_ai_instruction_the_code_block_stays():
    """AI 不可用（默认）⇒ **仍用**代码块 ⇒ 行为逐字一致。"""
    wf = _workflow()
    wf._ai_quota_instruction = wf._record_ai_quota_instruction(
        SimpleNamespace(source="rule", instruction="代码表说的怎么写")
    )

    text = _directive(wf, 1)

    assert "首章爽文原则" in text, "AI 不可用时必须回落代码写死的原则"


# ─────────────────────── 3) 只有 AI 来源才算 ───────────────────────


def test_only_the_ai_source_counts_as_the_ai_saying_it():
    """代码表回落（rule）与未评估（""）都**不算**「AI 说了怎么写」。"""
    from application.engine.services.rhythm_quota import (
        SOURCE_AI,
        SOURCE_NONE,
        SOURCE_RULE,
    )

    record = AutoNovelGenerationWorkflow._record_ai_quota_instruction

    ai_quota = SimpleNamespace(source=SOURCE_AI, instruction="AI 说的怎么写")
    rule_quota = SimpleNamespace(source=SOURCE_RULE, instruction="代码表说的怎么写")
    none_quota = SimpleNamespace(source=SOURCE_NONE, instruction="")

    assert record(ai_quota) == "AI 说的怎么写"
    assert record(rule_quota) == "", "代码表回落被当成 AI 说的 ⇒ AI 没说话却让 AI 背锅"
    assert record(none_quota) == ""


# ─────────────────────── 4) 接线 ───────────────────────


def test_the_payload_step_records_the_ai_instruction():
    """payload 那一步必须真的把**生效配额**的指令记下来。

    ⚠ 这条第一次写成「源码里出现过 ``_ai_quota_instruction``」⇒ **太弱**：
    变异把赋值改成 ``= ""``（接线断掉）它照样绿 ⇒ 变异验证报 MISS。
    ⇒ 改成钉住**确切的赋值语句**（含实参 ``effective_quota``）。
    """
    import inspect

    from application.workflows.auto_novel_generation_workflow import (
        AutoNovelGenerationWorkflow as W,
    )

    src = inspect.getsource(W.prepare_chapter_generation)
    assert (
        "self._ai_quota_instruction = self._record_ai_quota_instruction(effective_quota)"
        in src
    ), "payload 那一步没有把**生效配额**的指令记下来 ⇒ 接线断了"
