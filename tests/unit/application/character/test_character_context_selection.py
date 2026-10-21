"""D-04：角色上下文「按章取材」交回 AI。

红线（本文件负责钉死）：

1. **兜底路径与改造前逐位相同** —— AI 不可用时行为不得变化（不许"顺手优化"）。
2. **AI 决定选什么**：AI 挑中的候选进上下文，没挑的不进。
3. **不越界**：候选 id 不在清单里的一律丢弃（AI 不得凭空造记忆）。
4. **角色锚点必进**：名字 / 公开面 / 核心信念 / 禁忌 / 当前状态，AI 动不了。
5. **T2 前缀由代码拼**：AI 只写后半句 ⇒ 锚点不会被 AI 挤掉。
6. **三态可分辨**（ai / fallback / 未评估），且来源标记**不进 prompt 文本**。
7. **转发链不断**：async 层算好 → workflow → context_builder → allocator → 渲染方。
8. **依赖没装配 ⇒ 未评估**（不是"静默半生效"）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.character.services.character_context_selection import (
    CharacterContextSelectionService,
    CharacterContextSelector,
)
from application.character.services.character_context_selection_schema import (
    SOURCE_AI,
    SOURCE_FALLBACK,
    SOURCE_NONE,
    CharacterContextSelection,
    normalize_selection,
    selection_from_payload,
)
from application.engine.services.context_budget_allocator import ContextBudgetAllocator
from application.memory.services.character_context_compiler import (
    CharacterContextCompiler,
    default_t2_instruction,
)


# ── 夹具 ────────────────────────────────────────────────────────────


def _projection():
    return {
        "name": "沈砚",
        "entity_id": "c1",
        "constitution": {
            "public_profile": "刑部主事",
            "hidden_profile": "其实是细作",
            "reveal_chapter": 20,
            "core_belief": "只有力量能保护自己",
            "moral_taboos": ["杀手无寸铁之人"],
        },
        "current_state": {"summary": "旧伤未愈"},
        "active_scars": [
            {"impact": "排斥左后方", "source_event": "左肩被刺"},
            {"impact": "听见雷声会手抖", "source_event": "幼年雷灾"},
            {"impact": "第三条伤", "source_event": "第三条事件"},
        ],
        "active_motivations": [
            {"description": "查清师兄死因"},
            {"description": "护住陆深"},
            {"description": "第三条动机"},
        ],
        "voice_fingerprint": {
            "style": "惜字如金",
            "sentence_pattern": "短句",
            "speech_tempo": "slow",
            "catchphrases": ["嗯", "别废话", "第三条", "第四条"],
        },
        "knowledge_boundary": {"known": [], "unknown": ["不知道师兄还活着"]},
        "relationships": [
            {"description": "与陆深互为对手"},
            {"description": "欠顾白一条命"},
            {"description": "第三条关系"},
        ],
    }


#: 改造前（固定切片）路径的**逐位**期望值
EXPECTED_T0 = (
    "- 沈砚"
    " | 公开面:刑部主事"
    " | 核心信念:只有力量能保护自己"
    " | 禁忌:杀手无寸铁之人"
    " | 当前状态:旧伤未愈"
    " | 创伤:左肩被刺->排斥左后方"
    " | 创伤:幼年雷灾->听见雷声会手抖"
    " | 驱动力:查清师兄死因"
    " | 驱动力:护住陆深"
    " | 声线:惜字如金 / 短句 / slow"
    " | 口癖:嗯、别废话、第三条"
    " | 知识边界:不知道师兄还活着"
    " | 禁止漂移:不得无因违背核心信念：只有力量能保护自己"
)
EXPECTED_T1 = "- 沈砚 | 状态:旧伤未愈 | 关系张力:与陆深互为对手；欠顾白一条命"
EXPECTED_T2 = "- 沈砚：允许过场/提及，禁止抢走主线焦点"


def _cached_service(projections):
    class _Service:
        def __init__(self):
            self.calls = 0

        def get_projection(self, novel_id, character_id):
            self.calls += 1
            return projections.get(character_id, {})

    return _Service()


def _plan(*slots):
    return SimpleNamespace(
        slots=[SimpleNamespace(character_id=cid, importance=imp) for cid, imp in slots]
    )


# ── 1. 兜底路径逐位不变 ─────────────────────────────────────────────


def test_fallback_output_is_byte_identical_to_pre_d04():
    """AI 不可用 ⇒ 行为与改造前**逐位相同**。"""
    locks = CharacterContextCompiler().compile(_projection())

    assert locks["t0"] == EXPECTED_T0
    assert locks["t1"] == EXPECTED_T1
    assert locks["t2"] == EXPECTED_T2


def test_fallback_slices_still_cap_each_category():
    """切片口径不变：伤/执念各 2、口癖 3、关系 2。"""
    t0 = CharacterContextCompiler().compile(_projection())["t0"]

    assert "第三条伤" not in t0
    assert "第三条动机" not in t0
    assert "第四条" not in t0          # 口癖取前 3
    assert "第三条关系" not in CharacterContextCompiler().compile(_projection())["t1"]


# ── 2. AI 决定选什么 ───────────────────────────────────────────────


def test_ai_selection_changes_which_facts_enter_the_context():
    locks = CharacterContextCompiler().compile(
        _projection(), selection={"fact_ids": ["s1", "m0"], "t2": ""}
    )

    assert "创伤:幼年雷灾->听见雷声会手抖" in locks["t0"]
    assert "创伤:左肩被刺->排斥左后方" not in locks["t0"]
    assert "驱动力:查清师兄死因" in locks["t0"]
    assert "驱动力:护住陆深" not in locks["t0"]


def test_ai_selection_changes_t2_wording():
    locks = CharacterContextCompiler().compile(
        _projection(), selection={"fact_ids": [], "t2": "只让他在场，别抢沈砚的戏"}
    )

    assert locks["t2"] == "- 沈砚：只让他在场，别抢沈砚的戏"


# ── 3. 角色锚点必进 + T2 前缀由代码拼 ───────────────────────────────


def test_anchor_survives_an_ai_selection_that_picks_nothing():
    """AI 什么都没挑 ⇒ 锚点仍在（名字/公开面/信念/禁忌/状态）。"""
    locks = CharacterContextCompiler().compile(
        _projection(), selection={"fact_ids": [], "t2": ""}
    )

    for anchor in ("- 沈砚", "公开面:刑部主事", "核心信念:只有力量能保护自己",
                   "禁忌:杀手无寸铁之人", "当前状态:旧伤未愈"):
        assert anchor in locks["t0"]
    assert locks["t2"] == EXPECTED_T2


def test_t2_prefix_is_written_by_code_not_by_ai():
    """AI 只写后半句 ⇒ 锚点（名字）不会被 AI 挤掉。"""
    locks = CharacterContextCompiler().compile(
        _projection(), selection={"fact_ids": [], "t2": "过场提及"}
    )

    assert locks["t2"].startswith("- 沈砚：")
    assert locks["t2"] == "- 沈砚：过场提及"


# ── 4. 不越界：AI 不得凭空造记忆 ────────────────────────────────────


def test_fact_ids_outside_the_candidate_list_are_dropped():
    result = normalize_selection(
        {"characters": [{"character_id": "c1", "selected_fact_ids": ["s0", "zz9"]}]},
        valid_fact_ids={"c1": {"s0", "s1"}},
    )

    assert result.characters["c1"].fact_ids == ("s0",)


def test_unknown_character_is_dropped():
    result = normalize_selection(
        {"characters": [{"character_id": "ghost", "selected_fact_ids": ["s0"]}]},
        valid_fact_ids={"c1": {"s0"}},
    )

    assert "ghost" not in result.characters
    assert result.source == SOURCE_NONE


def test_character_with_nothing_to_say_falls_back():
    result = normalize_selection(
        {"characters": [{"character_id": "c1", "selected_fact_ids": []}]},
        valid_fact_ids={"c1": {"s0"}},
    )

    assert result.source == SOURCE_NONE


# ── 5. 三态可分辨 + 来源不进 prompt ────────────────────────────────


def test_selection_source_is_three_state():
    ai = normalize_selection(
        {"characters": [{"character_id": "c1", "selected_fact_ids": ["s0"]}]},
        valid_fact_ids={"c1": {"s0"}},
    )
    assert ai.source == SOURCE_AI

    assert CharacterContextSelection({}, SOURCE_FALLBACK).source == SOURCE_FALLBACK
    assert CharacterContextSelection({}, SOURCE_NONE).source == SOURCE_NONE
    assert CharacterContextSelection({}, SOURCE_FALLBACK).is_usable is False
    assert ai.is_usable is True


def test_payload_round_trip_and_rejects_non_ai_source():
    ai = normalize_selection(
        {"characters": [{"character_id": "c1", "selected_fact_ids": ["s0"], "t2_instruction": "x"}]},
        valid_fact_ids={"c1": {"s0"}},
    )
    payload = ai.to_payload()

    assert payload["source"] == SOURCE_AI
    assert payload["characters"]["c1"]["fact_ids"] == ["s0"]

    restored = selection_from_payload(payload)
    assert restored is not None and restored.is_usable
    assert restored.for_character("c1").fact_ids == ("s0",)

    assert selection_from_payload({"source": SOURCE_FALLBACK, "characters": {}}) is None
    assert selection_from_payload(None) is None


def test_source_marker_never_enters_the_prompt_text():
    """来源标记是程序层面的事，不能污染写给模型的提示词。"""
    locks = CharacterContextCompiler().compile(
        _projection(), selection={"fact_ids": ["s0"], "t2": "只让他在场"}
    )

    for text in (locks["t0"], locks["t1"], locks["t2"]):
        assert "fallback" not in text
        assert '"source"' not in text
        assert "source=" not in text


# ── 6. 两份实现的 T2 措辞同源 ───────────────────────────────────────


def test_kernel_t2_uses_the_same_shared_implementation():
    from application.character.services.character_narrative_kernel import (
        CharacterNarrativeKernel,
    )

    kernel = CharacterNarrativeKernel.__new__(CharacterNarrativeKernel)
    char = SimpleNamespace(name="沈砚")
    slot = SimpleNamespace(character_id="c1", name="沈砚")

    assert kernel._format_t2_permission(char, slot) == default_t2_instruction("沈砚")
    assert kernel._format_t2_permission(char, slot) == EXPECTED_T2


def test_kernel_t2_honours_ai_wording_but_keeps_the_prefix():
    from application.character.services.character_narrative_kernel import (
        CharacterNarrativeKernel,
    )

    kernel = CharacterNarrativeKernel.__new__(CharacterNarrativeKernel)
    char = SimpleNamespace(name="沈砚")
    slot = SimpleNamespace(character_id="c1", name="沈砚")
    selection = {"characters": {"c1": {"t2": "别让他说话"}}}

    assert kernel._format_t2_permission(char, slot, selection=selection) == "- 沈砚：别让他说话"


# ── 7. 渲染方按选择渲染（端到端到上下文文本）──────────────────────


def test_renderer_applies_the_ai_selection():
    projection = _projection()
    projection["context_locks"] = CharacterContextCompiler().compile(projection)
    allocator = ContextBudgetAllocator(
        character_projection_service=_cached_service({"c1": projection})
    )

    locks = allocator._projection_locks_for_plan(
        "n1", _plan(("c1", "major")), tier="t0", chapter_number=3,
        selection={"source": "ai", "characters": {"c1": {"fact_ids": ["s1"], "t2": "只让他在场"}}},
    )

    assert "创伤:幼年雷灾->听见雷声会手抖" in locks
    assert "创伤:左肩被刺->排斥左后方" not in locks


def test_renderer_without_selection_is_unchanged():
    projection = _projection()
    projection["context_locks"] = CharacterContextCompiler().compile(projection)
    allocator = ContextBudgetAllocator(
        character_projection_service=_cached_service({"c1": projection})
    )

    locks = allocator._projection_locks_for_plan(
        "n1", _plan(("c1", "major")), tier="t0", chapter_number=3
    )

    assert locks == EXPECTED_T0


def test_renderer_ignores_a_selection_for_another_character():
    projection = _projection()
    projection["context_locks"] = CharacterContextCompiler().compile(projection)
    allocator = ContextBudgetAllocator(
        character_projection_service=_cached_service({"c1": projection})
    )

    locks = allocator._projection_locks_for_plan(
        "n1", _plan(("c1", "major")), tier="t0", chapter_number=3,
        selection={"source": "ai", "characters": {"other": {"fact_ids": ["s1"], "t2": "x"}}},
    )

    assert locks == EXPECTED_T0


# ── 8. 转发链不断 ──────────────────────────────────────────────────


def test_context_builder_forwards_the_selection_to_the_allocator():
    from application.engine.services.context_builder import ContextBuilder

    builder = ContextBuilder.__new__(ContextBuilder)
    builder._apply_slot_weights = MagicMock()
    builder._get_bible_dto = MagicMock(return_value=None)
    builder.budget_allocator = MagicMock()
    builder.budget_allocator.allocate = MagicMock(
        return_value=SimpleNamespace(slots={}, used_tokens=0)
    )

    selection = {"source": "ai", "characters": {"c1": {"fact_ids": ["s0"], "t2": "x"}}}
    builder.build_structured_context("n1", 3, "大纲", character_context_selection=selection)

    assert builder.budget_allocator.allocate.call_args.kwargs["character_context_selection"] is selection


def test_workflow_forwards_the_selection_to_the_builder():
    from application.workflows.auto_novel_generation_workflow import (
        AutoNovelGenerationWorkflow,
    )

    workflow = AutoNovelGenerationWorkflow.__new__(AutoNovelGenerationWorkflow)
    workflow.context_builder = MagicMock()
    workflow.context_builder.build_structured_context = MagicMock(
        return_value={
            "token_usage": {"total": 0},
            "layer1_text": "",
            "layer2_text": "",
            "layer3_text": "",
        }
    )
    workflow._theme_integrator = None
    workflow._evolution_gate_service = None
    workflow._get_style_summary = MagicMock(return_value="")

    selection = {"source": "ai", "characters": {"c1": {"fact_ids": ["s0"], "t2": "x"}}}
    workflow.prepare_chapter_generation(
        "n1", 3, "大纲", character_context_selection=selection
    )

    assert (
        workflow.context_builder.build_structured_context.call_args.kwargs[
            "character_context_selection"
        ]
        is selection
    )


@pytest.mark.asyncio
async def test_pipeline_forwards_the_selection_to_the_workflow():
    from engine.pipeline.base import BaseStoryPipeline
    from engine.pipeline.context import PipelineContext

    pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)
    pipeline._log_step = lambda *a, **k: None

    selection = {"source": "ai", "characters": {"c1": {"fact_ids": ["s0"], "t2": "x"}}}
    ctx = PipelineContext(novel_id="n1", chapter_number=3, outline="大纲")
    ctx.chapter_workflow = MagicMock()
    ctx.chapter_workflow.prepare_chapter_generation = MagicMock(
        return_value={"context": "CTX", "context_tokens": 1, "bundle": {}}
    )
    pipeline._plan_slot_weights = AsyncMock(return_value=None)
    pipeline._plan_character_context = AsyncMock(return_value=selection)

    await pipeline._step_build_context(ctx)

    assert (
        ctx.chapter_workflow.prepare_chapter_generation.call_args.kwargs[
            "character_context_selection"
        ]
        is selection
    )


@pytest.mark.asyncio
async def test_pipeline_without_the_service_is_unassessed():
    """依赖没装配 ⇒ 未评估（None），不是"静默半生效"。"""
    from engine.pipeline.base import BaseStoryPipeline
    from engine.pipeline.context import PipelineContext

    pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)
    ctx = PipelineContext(novel_id="n1", chapter_number=3, outline="大纲")

    assert await pipeline._plan_character_context(ctx) is None


@pytest.mark.asyncio
async def test_pipeline_calls_the_ai_service_and_returns_its_payload():
    """async 层真的调了 AI，并把结果转成可下传的 payload。"""
    from engine.pipeline.base import BaseStoryPipeline
    from engine.pipeline.context import PipelineContext
    from application.character.services.character_context_selection_schema import (
        CharacterFactSelection,
    )

    pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)
    ctx = PipelineContext(novel_id="n1", chapter_number=3, outline="大纲")
    service = MagicMock()
    service.select_for_chapter = AsyncMock(return_value=CharacterContextSelection(
        {"c1": CharacterFactSelection("c1", ("s0",), "只让他在场", SOURCE_AI)}, SOURCE_AI
    ))
    ctx.inject(character_context_selection_service=service)

    payload = await pipeline._plan_character_context(ctx)

    assert payload["source"] == SOURCE_AI
    assert payload["characters"]["c1"]["fact_ids"] == ["s0"]
    assert payload["characters"]["c1"]["t2"] == "只让他在场"


def test_runner_registers_the_selection_service():
    """依赖必须在 runner 装配 —— 漏了，取材整条链就是死的（AI 永远不被调用）。"""
    from engine.runtime.runner import StoryPipelineRunner

    runner = StoryPipelineRunner.__new__(StoryPipelineRunner)
    runner.context_builder = SimpleNamespace(budget_allocator=object())
    runner.llm_service = MagicMock()

    assert runner._character_context_selection_service() is not None


# ── 9. 选择器：失败即未评估 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_selector_returns_unassessed_when_the_llm_fails():
    class _Boom:
        async def generate(self, prompt, config=None):
            raise RuntimeError("endpoint down")

    result = await CharacterContextSelector(_Boom()).select(
        chapter_number=3,
        candidates=[{"character_id": "c1", "fact_ids": ["s0"], "facts": []}],
    )

    assert result.source == SOURCE_NONE
    assert result.characters == {}


@pytest.mark.asyncio
async def test_selector_drops_ids_outside_the_candidate_list():
    captured = {}

    class _LLM:
        async def generate(self, prompt, config=None):
            captured["prompt"] = prompt
            return SimpleNamespace(content=json.dumps({
                "characters": [
                    {"character_id": "c1", "selected_fact_ids": ["s0", "ghost"], "t2_instruction": "x"},
                ]
            }))

    result = await CharacterContextSelector(_LLM()).select(
        chapter_number=3,
        candidates=[{
            "character_id": "c1",
            "name": "沈砚",
            "fact_ids": ["s0"],
            "facts": [{"id": "s0", "category": "创伤", "text": "创伤:旧伤"}],
        }],
    )

    assert result.characters["c1"].fact_ids == ("s0",)
    assert "沈砚" in captured["prompt"].user


@pytest.mark.asyncio
async def test_service_without_candidates_is_unassessed():
    service = CharacterContextSelectionService(
        llm_service=MagicMock(),
        allocator=MagicMock(),
    )
    service._allocator.build_character_context_candidates = MagicMock(return_value=[])

    result = await service.select_for_chapter(novel_id="n1", chapter_number=3)

    assert result.source == SOURCE_NONE


# ── 复审 §3.4a：条数上限（代码保证的上下文预算） ─────────────────────


def test_the_ai_cannot_exceed_the_per_category_and_total_limits():
    """AI 决定「带**哪**几条」，不能决定「带**多少**条」。

    复审 §3.4a：候选清单里可能有几十条，全带进去会把 T0 撑爆；
    而 T0 的截断是**与角色无关**的 ⇒ 后面的角色锚点会被整行切掉
    （「锚点必进」是逐条成立，不是逐槽位成立）。
    """
    from application.character.services.character_context_selection_schema import (
        CATEGORY_LIMIT,
        CATEGORY_PREFIX,
        MAX_FACTS_PER_CHARACTER,
        normalize_selection,
    )

    ids = (
        [f"s{i}" for i in range(20)]
        + [f"m{i}" for i in range(20)]
        + [f"c{i}" for i in range(20)]
    )
    result = normalize_selection(
        {"characters": [{"character_id": "c1", "selected_fact_ids": ids}]},
        valid_fact_ids={"c1": set(ids)},
    )

    kept = result.characters["c1"].fact_ids
    assert len(kept) <= MAX_FACTS_PER_CHARACTER, f"AI 选了 {len(kept)} 条，超出总量上限"
    for category, limit in CATEGORY_LIMIT.items():
        prefix = CATEGORY_PREFIX[category]
        picked = sum(1 for fid in kept if fid.startswith(prefix))
        assert picked <= limit, f"{category} 选了 {picked} 条，超出该类上限 {limit}"


def test_the_category_spec_is_the_single_source():
    """类别 / 前缀 / 每类条数上限只此一份（compiler 的 CATEGORY_SPEC）。

    复审 §3.2 的第 5 处「同一逻辑写两份」：取材 schema 原先又抄了一份
    类别与前缀。
    """
    from application.character.services.character_context_selection_schema import (
        CATEGORY_LIMIT,
        CATEGORY_PREFIX,
        FACT_CATEGORIES,
    )
    from application.memory.services.character_context_compiler import CATEGORY_SPEC

    assert set(FACT_CATEGORIES) == set(CATEGORY_SPEC)
    assert CATEGORY_PREFIX == {c: s["prefix"] for c, s in CATEGORY_SPEC.items()}
    assert CATEGORY_LIMIT == {c: s["limit"] for c, s in CATEGORY_SPEC.items()}
