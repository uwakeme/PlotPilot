"""D-02 接线：权重如何流到生产主路径，以及为什么不跨章泄漏。

生产链路（本文件用测试钉住，避免只凭口述）

    StoryPipeline._step_build_context          （engine/pipeline/base.py，async）
      └─ _plan_slot_weights()                  开关 / shadow / 降级链
      └─ chapter_workflow.prepare_chapter_generation(..., slot_weights=)
           └─ ContextBuilder.build_structured_context(..., slot_weights=)
                └─ _apply_slot_weights()
                     └─ budget_allocator.allocate()
                          └─ allocate_tier(slot_weights=...)

三条必须钉住的行为
------------------
1. **开关默认关** ⇒ 一行权重都不施加，生产行为与改造前逐位一致。
2. **shadow 默认开**（即便开关打开）⇒ 只记录不施加。
3. **不跨章泄漏** —— 分配器是长生命周期对象，若只在「有权重时」赋值，
   上一章的权重会漏到下一章。这是最难查的一类串味。
"""
import pytest

from application.engine.services.context_builder import ContextBuilder
from application.engine.services.context_weight_settings import (
    ContextWeightRuntimeSettings,
    get_context_weight_runtime_settings,
)
from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext


# ── 开关默认关 ──────────────────────────────────────────────────────────

def test_runtime_settings_default_to_disabled_and_shadow():
    """默认值必须是最保守的：关 + shadow。"""
    s = ContextWeightRuntimeSettings()
    assert s.enabled is False, "开关默认必须关闭，否则上线即改变生产行为"
    assert s.shadow is True, "默认必须是 shadow 观察期"
    assert s.cache_ttl_seconds > 0
    assert s.failure_backoff_seconds > 0


def test_settings_reader_never_enables_by_accident():
    """配置文件里没写 context_weights 段时，读出来仍是关闭。"""
    assert get_context_weight_runtime_settings().enabled is False


# ── 不跨章泄漏 ──────────────────────────────────────────────────────────

class _RecordingAllocator:
    def __init__(self):
        self.slot_weights = None
        self.calls = []

    def allocate(self, **kwargs):
        self.calls.append(dict(self.slot_weights or {}))
        return type("A", (), {
            "slots": {},          # build_structured_context 遍历 allocation.slots
            "used_tokens": 0,
            "get_final_context": lambda self: "ctx",
        })()

    def estimate_tokens(self, text):
        return 0


def _builder():
    cb = ContextBuilder.__new__(ContextBuilder)
    cb.budget_allocator = _RecordingAllocator()
    # build_structured_context 还会调它；与权重无关，直接短路
    cb._build_layer2_smart_retrieval = lambda **kwargs: ""
    return cb


def test_weights_reach_the_allocator_when_supplied():
    cb = _builder()
    cb.build_context("n1", 1, "大纲", slot_weights={"recent_chapters": 0.9})

    assert cb.budget_allocator.calls[-1] == {"recent_chapters": 0.9}


def test_no_weights_means_allocator_gets_none():
    cb = _builder()
    cb.build_context("n1", 1, "大纲")

    assert cb.budget_allocator.calls[-1] == {}


def test_weights_do_not_leak_into_the_next_chapter():
    """**核心回归**：本章有权重、下一章没有 ⇒ 下一章必须拿到 None。

    分配器是长生命周期对象。只在「有权重时」赋值会让上一章的权重漏到下一章，
    而且漏得很隐蔽 —— 上下文看起来完全正常，只是配比悄悄错了。
    """
    cb = _builder()
    cb.build_context("n1", 10, "大纲", slot_weights={"recent_chapters": 0.9})
    cb.build_context("n1", 11, "大纲")           # 下一章：没有权重

    assert cb.budget_allocator.calls[0] == {"recent_chapters": 0.9}
    assert cb.budget_allocator.calls[1] == {}, "上一章的权重泄漏到了下一章"


def test_empty_weights_also_clear():
    cb = _builder()
    cb.build_context("n1", 10, "大纲", slot_weights={"recent_chapters": 0.9})
    cb.build_context("n1", 11, "大纲", slot_weights={})

    assert cb.budget_allocator.calls[-1] == {}


def test_structured_context_clears_weights_too():
    """结构化入口是生产主路径，同样必须显式清空。"""
    cb = _builder()
    cb.build_context("n1", 10, "大纲", slot_weights={"recent_chapters": 0.9})
    cb.build_structured_context("n1", 11, "大纲")

    assert cb.budget_allocator.calls[-1] == {}


# ── 开关与 shadow 语义 ──────────────────────────────────────────────────

class _FakeCtx:
    def __init__(self, novel_id="n1", chapter_number=1, outline="大纲"):
        self.novel_id = novel_id
        self.chapter_number = chapter_number
        self.outline = outline


@pytest.mark.asyncio
async def test_disabled_means_no_planning_call_at_all(monkeypatch):
    """开关关闭时**连规划器都不该构造** —— 省一次 LLM 调用。"""
    import engine.pipeline.base as base_mod

    called = {"n": 0}

    def _boom():
        called["n"] += 1
        raise AssertionError("开关关闭时不应构造规划器")

    monkeypatch.setattr(base_mod, "logger", type("L", (), {
        "warning": lambda self, *a, **k: None,
        "info": lambda self, *a, **k: None,
    })())
    monkeypatch.setattr(
        "application.engine.services.context_weight_settings.get_context_weight_runtime_settings",
        lambda: ContextWeightRuntimeSettings(enabled=False, shadow=False),
    )
    monkeypatch.setattr("interfaces.api.dependencies.get_context_weight_planner", _boom)

    assert await BaseStoryPipeline._plan_slot_weights(object.__new__(BaseStoryPipeline), _FakeCtx()) is None
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_shadow_mode_computes_but_does_not_apply(monkeypatch):
    """shadow 观察期：要拿到权重（才能记录对比），但**不施加**。"""
    import engine.pipeline.base as base_mod

    monkeypatch.setattr(base_mod, "logger", type("L", (), {
        "warning": lambda self, *a, **k: None,
        "info": lambda self, *a, **k: None,
    })())
    monkeypatch.setattr(
        "application.engine.services.context_weight_settings.get_context_weight_runtime_settings",
        lambda: ContextWeightRuntimeSettings(enabled=True, shadow=True),
    )

    class _Planner:
        async def plan_weights(self, **kwargs):
            return {"recent_chapters": 0.9}

    monkeypatch.setattr(
        "interfaces.api.dependencies.get_context_weight_planner", lambda: _Planner()
    )

    result = await BaseStoryPipeline._plan_slot_weights(
        object.__new__(BaseStoryPipeline), _FakeCtx()
    )
    assert result is None, "shadow 模式不得返回权重"


@pytest.mark.asyncio
async def test_enabled_and_not_shadow_returns_weights(monkeypatch):
    import engine.pipeline.base as base_mod

    monkeypatch.setattr(base_mod, "logger", type("L", (), {
        "warning": lambda self, *a, **k: None,
        "info": lambda self, *a, **k: None,
    })())
    monkeypatch.setattr(
        "application.engine.services.context_weight_settings.get_context_weight_runtime_settings",
        lambda: ContextWeightRuntimeSettings(enabled=True, shadow=False),
    )

    class _Planner:
        async def plan_weights(self, **kwargs):
            return {"recent_chapters": 0.9}

    monkeypatch.setattr(
        "interfaces.api.dependencies.get_context_weight_planner", lambda: _Planner()
    )

    result = await BaseStoryPipeline._plan_slot_weights(
        object.__new__(BaseStoryPipeline), _FakeCtx()
    )
    assert result == {"recent_chapters": 0.9}


@pytest.mark.asyncio
async def test_unevaluated_means_no_weights_applied(monkeypatch):
    """规划器返回 None（没判出来）⇒ 不得施加任何默认权重。"""
    import engine.pipeline.base as base_mod

    monkeypatch.setattr(base_mod, "logger", type("L", (), {
        "warning": lambda self, *a, **k: None,
        "info": lambda self, *a, **k: None,
    })())
    monkeypatch.setattr(
        "application.engine.services.context_weight_settings.get_context_weight_runtime_settings",
        lambda: ContextWeightRuntimeSettings(enabled=True, shadow=False),
    )

    class _Planner:
        async def plan_weights(self, **kwargs):
            return None

    monkeypatch.setattr(
        "interfaces.api.dependencies.get_context_weight_planner", lambda: _Planner()
    )

    assert await BaseStoryPipeline._plan_slot_weights(
        object.__new__(BaseStoryPipeline), _FakeCtx()
    ) is None


@pytest.mark.asyncio
async def test_planner_blowing_up_does_not_break_context_building(monkeypatch):
    """规划是「锦上添花」，它挂了绝不能拖垮上下文组装。"""
    import engine.pipeline.base as base_mod

    monkeypatch.setattr(base_mod, "logger", type("L", (), {
        "warning": lambda self, *a, **k: None,
        "info": lambda self, *a, **k: None,
    })())
    monkeypatch.setattr(
        "application.engine.services.context_weight_settings.get_context_weight_runtime_settings",
        lambda: ContextWeightRuntimeSettings(enabled=True, shadow=False),
    )

    def _boom():
        raise RuntimeError("规划器构造失败")

    monkeypatch.setattr("interfaces.api.dependencies.get_context_weight_planner", _boom)

    assert await BaseStoryPipeline._plan_slot_weights(
        object.__new__(BaseStoryPipeline), _FakeCtx()
    ) is None
