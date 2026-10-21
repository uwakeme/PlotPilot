"""D-03 P1：伏笔「按章取材」交回 AI + 注入时机前移到七段剧本之前。

红线（本文件负责钉死）：

1. **时机** —— 取材必须发生在**七段执行剧本生成之前**。旧实现里取材在上下文
   组装阶段，而剧本在那之前就定稿 ⇒ 伏笔永远影响不了剧本。
2. **一份选择两处使用** —— 剧本与正文上下文用的是**同一份**伏笔文本
   （各算一次就是两套口径）。
3. **不越界** —— 候选 id 不在清单里的一律丢弃（AI 不得凭空造伏笔）。
4. **核心伏笔不得遗忘** —— AI 漏选的核心伏笔由代码补回并**标记**。
5. **三态可分辨** —— ``ai`` / ``fallback``（AI 不可用时的领域层合格集）/
   未评估（``""``）。兜底不能读成「AI 选过」。
6. **兜底渲染逐位不变** —— AI 不可用时的文本与改造前完全一致。
"""
from __future__ import annotations

import ast
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.world.services.foreshadow_selection import ForeshadowSelectionPlanner
from application.world.services.foreshadow_selection_schema import (
    SOURCE_AI,
    SOURCE_FALLBACK,
    SOURCE_NONE,
    build_foreshadow_context,
    collect_candidates,
    fallback_selection,
    normalize_selection,
)
from domain.novel.value_objects.foreshadowing import (
    Foreshadowing,
    ForeshadowingStatus,
    ImportanceLevel,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
BASE_PATH = REPO_ROOT / "engine/pipeline/base.py"
ALLOC_PATH = REPO_ROOT / "application/engine/services/context_budget_allocator.py"


def _fs(fid, desc, *, chapter=1, importance=ImportanceLevel.MEDIUM, suggested=None, core=False):
    return Foreshadowing(
        id=fid,
        planted_in_chapter=chapter,
        description=desc,
        importance=importance,
        status=ForeshadowingStatus.PLANTED,
        suggested_resolve_chapter=suggested,
        emotional_weight=0.9 if core else 0.5,
    )


class _Registry:
    def __init__(self, items, subtexts=()):
        self._items = list(items)
        self._subtexts = list(subtexts)

    def get_t0_eligible_foreshadowings(self, current_chapter, max_items=6):
        return self._items[:max_items]

    def get_pending_subtext_entries(self):
        return list(self._subtexts)


def _registry():
    return _Registry(
        [
            _fs("fs-core", "左肩旧伤", chapter=3, importance=ImportanceLevel.CRITICAL,
                suggested=8, core=True),
            _fs("fs-a", "师兄的死因", chapter=1, importance=ImportanceLevel.HIGH, suggested=12),
        ],
        subtexts=[SimpleNamespace(chapter=5, character_id="c1",
                                  question="谁在堂上动了手", importance="high",
                                  suggested_resolve_chapter=8,
                                  is_priority_for_chapter=True)],
    )


# ── 1. 兜底渲染逐位不变 ───────────────────────────────────────────


def test_fallback_rendering_is_byte_identical_to_before():
    """AI 不可用时 ⇒ 文本与改造前**逐位相同**（标题/条目格式都不动）。"""
    registry = _registry()
    candidates = collect_candidates(registry, 6)

    text = build_foreshadow_context(registry, fallback_selection(candidates), 6)

    assert text.startswith("【待回收伏笔（爽文GC精选）】")
    assert "- Ch3 重要 即将到期: 左肩旧伤" in text
    assert "- Ch1 重要 预期Ch12: 师兄的死因" in text
    assert "【伏笔手账本·待兑现疑问】" in text
    assert "- Ch5 [c1] 即将到期: 谁在堂上动了手" in text


# ── 2. 不越界：AI 不得凭空造伏笔 ──────────────────────────────────


def test_unknown_fact_ids_are_dropped():
    candidates = collect_candidates(_registry(), 6)

    selection = normalize_selection(
        {"selected": [{"id": "fs-a", "note": "推进"}, {"id": "不存在的伏笔", "note": "编的"}]},
        candidates,
    )

    ids = [c.id for c in selection.candidates]
    assert "不存在的伏笔" not in ids
    assert "fs-a" in ids
    # 核心伏笔不在 AI 的选择里 ⇒ 代码补回（不静默改写）
    assert selection.restored_core_ids == ("fs-core",)


def test_duplicate_ids_are_collapsed():
    candidates = collect_candidates(_registry(), 6)

    selection = normalize_selection(
        {"selected": [{"id": "fs-a", "note": "a"}, {"id": "fs-a", "note": "b"}]},
        candidates,
    )

    assert [c.id for c in selection.candidates].count("fs-a") == 1


# ── 3. 核心伏笔不得遗忘 ───────────────────────────────────────────


def test_core_foreshadowing_is_restored_when_ai_omits_it():
    candidates = collect_candidates(_registry(), 6)

    selection = normalize_selection({"selected": [{"id": "fs-a", "note": "推进"}]}, candidates)

    assert "fs-core" in [c.id for c in selection.candidates]
    assert selection.restored_core_ids == ("fs-core",)


def test_core_foreshadowing_is_not_restored_twice():
    candidates = collect_candidates(_registry(), 6)

    selection = normalize_selection(
        {"selected": [{"id": "fs-core", "note": "推进"}]}, candidates
    )

    assert [c.id for c in selection.candidates].count("fs-core") == 1
    assert selection.restored_core_ids == ()


# ── 4. 三态可分辨 ─────────────────────────────────────────────────


def test_unusable_shapes_are_unassessed():
    candidates = collect_candidates(_registry(), 6)

    assert normalize_selection(None, candidates).source == SOURCE_NONE
    assert normalize_selection({"selected": "不是列表"}, candidates).source == SOURCE_NONE
    assert normalize_selection({"selected": [{"id": "fs-a"}]}, candidates).source == SOURCE_AI


def test_fallback_source_is_distinguishable_from_ai():
    candidates = collect_candidates(_registry(), 6)

    fallback = fallback_selection(candidates)
    ai = normalize_selection({"selected": [{"id": "fs-a"}]}, candidates)

    assert fallback.source == SOURCE_FALLBACK
    assert fallback.is_usable is False
    assert ai.source == SOURCE_AI
    assert ai.is_usable is True


@pytest.mark.asyncio
async def test_planner_returns_unassessed_when_the_llm_fails():
    class _Boom:
        async def generate(self, prompt, config=None):
            raise RuntimeError("endpoint down")

    candidates = collect_candidates(_registry(), 6)
    selection = await ForeshadowSelectionPlanner(_Boom()).select(
        chapter_number=6, candidates=candidates
    )

    assert selection.source == SOURCE_NONE
    assert selection.candidates == ()


@pytest.mark.asyncio
async def test_planner_passes_the_candidates_to_the_model():
    captured = {}

    class _LLM:
        async def generate(self, prompt, config=None):
            captured["prompt"] = prompt
            return SimpleNamespace(content=json.dumps({
                "selected": [{"id": "fs-a", "note": "让他想起师兄"}]
            }))

    candidates = collect_candidates(_registry(), 6)
    selection = await ForeshadowSelectionPlanner(_LLM()).select(
        chapter_number=6, candidates=candidates, chapter_outline="沈砚当堂翻案"
    )

    assert selection.source == SOURCE_AI
    assert selection.notes["fs-a"] == "让他想起师兄"
    text = captured["prompt"].user
    assert "沈砚当堂翻案" in text
    assert "fs-a" in text
    assert "左肩旧伤" in text


# ── 5. 时机：取材必须在七段剧本生成之前 ───────────────────────────


def test_foreshadow_selection_happens_before_the_preplan_is_generated():
    """旧实现里取材在上下文组装阶段 ⇒ 剧本早已定稿，伏笔影响不了剧本。"""
    source = BASE_PATH.read_bytes().decode("utf-8")
    assert "await self._plan_foreshadows(ctx)" in source
    assert source.index("await self._plan_foreshadows(ctx)") < source.index(
        "ensure_execution_plan("
    ), "伏笔取材必须早于七段剧本生成"


def test_the_preplan_prompt_receives_the_foreshadow_context():
    from application.blueprint.services.chapter_preplanning_service import (
        ChapterPreplanningService,
    )
    import inspect

    src = inspect.getsource(ChapterPreplanningService.ensure_execution_plan)
    assert "foreshadow_context" in src
    assert '"foreshadow_context": foreshadow_context or ""' in src


# ── 6. 一份选择两处使用 ───────────────────────────────────────────


def test_the_context_slot_uses_the_provided_text_instead_of_its_own():
    """渲染方拿得到管线算好的那份 ⇒ 两处不是各算一次。"""
    source = ALLOC_PATH.read_bytes().decode("utf-8")
    assert "if foreshadow_context is not None" in source
    assert "else self._get_pending_foreshadowings(novel_id, chapter_number)" in source


def test_the_legacy_renderer_is_replaced_by_the_shared_implementation():
    """渲染逻辑只有一份实现（剧本与正文共用）。"""
    source = ALLOC_PATH.read_bytes().decode("utf-8")
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_get_pending_foreshadowings"
    )
    body = ast.dump(fn)
    assert "build_foreshadow_context" in body
    # 旧的「二次截断」与旧的模板拼接必须不在了
    assert "爽文GC精选】\")" not in body


def test_pipeline_carries_the_foreshadow_context_on_the_context():
    from engine.pipeline.context import PipelineContext

    ctx = PipelineContext(novel_id="n1", chapter_number=3)
    assert ctx.foreshadow_context == ""
