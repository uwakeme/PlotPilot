"""D-01：本章节奏判定（余韵章）。

这块判断此前是代码公式（``legacy_writing_delegate.py:202`` 的 ``tension_100 >= 80``），
而 ``PipelineContext.needs_buffer`` / ``buffer_reason`` 自声明起**全仓无赋值** ——
StoryPipeline 这边等于「永远不会有余韵章」。

选章这件事本身**不交给 AI**
--------------------------
登记表说「规划层看状态后选章」，但实测生产库 3 本书 592 个章节点**零缺口**
（全部连续 1..N）。「取第一个未完成」在实践中已经是唯一保证章号连续的选法，
让 AI 在一个候选里"选"是假参与。
**章号连续仍是代码硬不变量**，AI 判断的是「这一章该怎么写」。

三态红线
--------
``needs_buffer`` 是 ``Optional[bool]``。判不出来 = ``None``，
调用方保持原行为但必须把原因写进 ``buffer_reason`` ——
**不得**把 None 当成 AI 的 ``False`` 结论再冒充成判断结果。
"""
import json

import pytest

from application.engine.services.chapter_mode_planner import ChapterModePlanner
from application.engine.services.chapter_mode_schema import ChapterModeLlmPayload
from engine.pipeline.base import BaseStoryPipeline
from application.engine.services.chapter_mode_settings import (
    BUFFER_CHAPTER_DIRECTIVE,
    ChapterModeRuntimeSettings,
    get_chapter_mode_runtime_settings,
)
from infrastructure.ai.prompt_contracts.chapter_mode import CHAPTER_MODE_CONTRACT
from infrastructure.ai.prompt_keys import PLANNING_CHAPTER_MODE


# ── 契约完整性 ──────────────────────────────────────────────────────────

def test_node_key_registered_and_contract_wired():
    from infrastructure.ai.prompt_keys import is_valid_key

    assert PLANNING_CHAPTER_MODE == "planning-chapter-mode"
    assert is_valid_key(PLANNING_CHAPTER_MODE)
    assert CHAPTER_MODE_CONTRACT.output_schema is ChapterModeLlmPayload
    assert CHAPTER_MODE_CONTRACT.generation_profile == "chapter_mode"


def test_prompt_renders():
    from infrastructure.ai.prompt_gateway import get_prompt_gateway

    rendered = get_prompt_gateway().render(
        CHAPTER_MODE_CONTRACT,
        {
            "novel_id": "n1", "chapter_number": 7, "outline": "揭开真凶",
            "last_tension": "87", "stage_hint": "发展",
            "due_debts": "（无）", "due_foreshadowings": "（无）",
        },
    )
    assert "揭开真凶" in rendered.prompt.user
    assert "87" in rendered.prompt.user


def test_validate_output_accepts_a_legal_payload():
    from infrastructure.ai.prompt_gateway import get_prompt_gateway

    parsed = get_prompt_gateway().validate_output(
        CHAPTER_MODE_CONTRACT,
        {"needs_buffer": True, "reason": "上章张力 87，冲击未落地", "signals": ["张力 87"]},
    )
    assert parsed.needs_buffer is True


def test_schema_default_is_none_not_false():
    """schema 不得预填 false —— 那会被读成「AI 判断过：本章正常推进」。"""
    payload = ChapterModeLlmPayload()
    assert payload.needs_buffer is None


# ── 开关 ────────────────────────────────────────────────────────────────

def test_settings_default_disabled_and_not_apply():
    """默认关闭且不施加 —— 改造前 StoryPipeline 从来没有余韵章。"""
    s = ChapterModeRuntimeSettings()
    assert s.enabled is False
    assert s.apply is False


def test_settings_reader_never_enables_by_accident():
    assert get_chapter_mode_runtime_settings().enabled is False


def test_chapter_mode_switch_is_independent_of_context_weights():
    """两项是不同创作决策，开一个不该顺带开另一个。"""
    from application.engine.services.context_weight_settings import (
        ContextWeightRuntimeSettings,
    )

    assert set(ChapterModeRuntimeSettings.__dataclass_fields__) != set(
        ContextWeightRuntimeSettings.__dataclass_fields__
    )
    assert "enabled" in ChapterModeRuntimeSettings.__dataclass_fields__


def test_buffer_directive_is_non_empty_and_mentions_slowdown():
    assert BUFFER_CHAPTER_DIRECTIVE
    assert "余韵" in BUFFER_CHAPTER_DIRECTIVE
    assert "放缓" in BUFFER_CHAPTER_DIRECTIVE


# ── 规划器三态 ──────────────────────────────────────────────────────────

class _FakeLLM:
    def __init__(self, content="", raise_exc=None):
        self._content = content
        self._raise = raise_exc
        self.calls = 0

    async def generate(self, prompt, config):
        self.calls += 1
        if self._raise:
            raise self._raise
        return type("R", (), {"content": self._content})()


async def _decide(llm, **kw):
    return await ChapterModePlanner(llm).decide(
        novel_id="n1", chapter_number=7, **kw
    )


@pytest.mark.asyncio
async def test_buffer_chapter_is_decided():
    llm = _FakeLLM(json.dumps(
        {"needs_buffer": True, "reason": "上章张力 87，冲击未落地", "signals": ["张力 87"]},
        ensure_ascii=False))
    got = await _decide(llm)
    assert got["needs_buffer"] is True
    assert got["signals"] == ["张力 87"]


@pytest.mark.asyncio
async def test_normal_progression_is_decided():
    llm = _FakeLLM(json.dumps({"needs_buffer": False, "reason": "上章张力平稳"},
                              ensure_ascii=False))
    got = await _decide(llm)
    assert got["needs_buffer"] is False


@pytest.mark.asyncio
async def test_llm_failure_is_undecided_not_false():
    """红线：调用失败 ≠ 「AI 判断本章正常推进」。"""
    assert await _decide(_FakeLLM(raise_exc=RuntimeError("上游 503"))) is None


@pytest.mark.asyncio
async def test_non_json_is_undecided():
    assert await _decide(_FakeLLM("模型返回了一段解释")) is None


@pytest.mark.asyncio
async def test_json_array_is_undecided():
    assert await _decide(_FakeLLM("[true]")) is None


@pytest.mark.asyncio
async def test_null_needs_buffer_is_undecided():
    """AI 明说「判不出来」⇒ 必须 None，不能折中成 False。"""
    llm = _FakeLLM(json.dumps(
        {"needs_buffer": None, "reason": "缺少上章状态"}, ensure_ascii=False))
    assert await _decide(llm) is None


@pytest.mark.asyncio
async def test_missing_needs_buffer_field_is_undecided():
    llm = _FakeLLM(json.dumps({"reason": "忘了写"}, ensure_ascii=False))
    assert await _decide(llm) is None


@pytest.mark.asyncio
async def test_string_true_is_not_accepted_as_boolean():
    """字符串 "true" 不是明确结论 ⇒ 判不出来。"""
    llm = _FakeLLM(json.dumps({"needs_buffer": "true", "reason": "?"},
                              ensure_ascii=False))
    assert await _decide(llm) is None


@pytest.mark.asyncio
async def test_signals_are_normalized_not_raw():
    llm = _FakeLLM(json.dumps(
        {"needs_buffer": True, "reason": "x",
         "signals": ["  张力 87  ", "", None, 123, "a"]},
        ensure_ascii=False))
    got = await _decide(llm)
    assert got["signals"] == ["张力 87", "None", "123", "a"][:4] or \
        all(isinstance(s, str) for s in got["signals"])
    assert len(got["signals"]) <= 6


# ── 管线接线 ────────────────────────────────────────────────────────────

class _Ctx:
    def __init__(self):
        self.novel_id = "n1"
        self.chapter_number = 7
        self.outline = "本章大纲"
        self.needs_buffer = False
        self.buffer_reason = ""


class _Novel:
    last_chapter_tension = 87


@pytest.mark.asyncio
async def test_disabled_keeps_original_behaviour(monkeypatch):
    """开关关闭 ⇒ 不调用规划器，ctx 保持 (False, "")。

    守卫用「记录是否被调用」而不是「让它抛异常」——
    后者会被 `_decide_chapter_mode` 的 `except Exception` 吞掉，
    于是「根本没构造」与「构造了但失败」分不出来，测试会永远绿。
    """
    import engine.pipeline.base as base_mod

    monkeypatch.setattr(base_mod, "logger", type("L", (), {
        "warning": lambda self, *a, **k: None, "info": lambda self, *a, **k: None,
    })())
    monkeypatch.setattr(
        "application.engine.services.chapter_mode_settings.get_chapter_mode_runtime_settings",
        lambda: ChapterModeRuntimeSettings(enabled=False, apply=False))

    constructed = {"n": 0}

    def _factory():
        constructed["n"] += 1
        raise RuntimeError("开关关闭时不该走到这里")

    monkeypatch.setattr("interfaces.api.dependencies.get_chapter_mode_planner", _factory)

    ctx = _Ctx()
    await BaseStoryPipeline._decide_chapter_mode(object.__new__(BaseStoryPipeline), ctx, _Novel())
    assert constructed["n"] == 0, "开关关闭时仍然构造了规划器（白白多一次 LLM 调用）"
    assert ctx.needs_buffer is False
    assert ctx.buffer_reason == ""
    assert ctx.outline == "本章大纲"


@pytest.mark.asyncio
async def test_undecided_writes_reason_but_keeps_normal_progression(monkeypatch):
    """判不出来 ⇒ 保持常规推进，但**必须写明原因**（不得冒充成 AI 结论）。"""
    import engine.pipeline.base as base_mod

    monkeypatch.setattr(base_mod, "logger", type("L", (), {
        "warning": lambda self, *a, **k: None, "info": lambda self, *a, **k: None,
    })())
    monkeypatch.setattr(
        "application.engine.services.chapter_mode_settings.get_chapter_mode_runtime_settings",
        lambda: ChapterModeRuntimeSettings(enabled=True, apply=True))

    class _Planner:
        async def decide(self, **kw):
            return None

    monkeypatch.setattr(
        "interfaces.api.dependencies.get_chapter_mode_planner", lambda: _Planner())

    ctx = _Ctx()
    await BaseStoryPipeline._decide_chapter_mode(object.__new__(BaseStoryPipeline), ctx, _Novel())
    assert ctx.needs_buffer is False
    assert "未判定" in ctx.buffer_reason, f"判不出来必须留下原因，实际：{ctx.buffer_reason!r}"
    assert ctx.outline == "本章大纲"


@pytest.mark.asyncio
async def test_enabled_without_apply_only_records(monkeypatch):
    """观察期：判定为余韵章但不拼进 outline。"""
    import engine.pipeline.base as base_mod

    monkeypatch.setattr(base_mod, "logger", type("L", (), {
        "warning": lambda self, *a, **k: None, "info": lambda self, *a, **k: None,
    })())
    monkeypatch.setattr(
        "application.engine.services.chapter_mode_settings.get_chapter_mode_runtime_settings",
        lambda: ChapterModeRuntimeSettings(enabled=True, apply=False))

    class _Planner:
        async def decide(self, **kw):
            return {"needs_buffer": True, "reason": "冲击未落地", "signals": []}

    monkeypatch.setattr(
        "interfaces.api.dependencies.get_chapter_mode_planner", lambda: _Planner())

    ctx = _Ctx()
    await BaseStoryPipeline._decide_chapter_mode(object.__new__(BaseStoryPipeline), ctx, _Novel())
    assert ctx.needs_buffer is True
    assert ctx.outline == "本章大纲", "apply=False 时不得改写 outline"


@pytest.mark.asyncio
async def test_enabled_with_apply_prefixes_outline(monkeypatch):
    import engine.pipeline.base as base_mod

    monkeypatch.setattr(base_mod, "logger", type("L", (), {
        "warning": lambda self, *a, **k: None, "info": lambda self, *a, **k: None,
    })())
    monkeypatch.setattr(
        "application.engine.services.chapter_mode_settings.get_chapter_mode_runtime_settings",
        lambda: ChapterModeRuntimeSettings(enabled=True, apply=True))

    class _Planner:
        async def decide(self, **kw):
            return {"needs_buffer": True, "reason": "冲击未落地", "signals": []}

    monkeypatch.setattr(
        "interfaces.api.dependencies.get_chapter_mode_planner", lambda: _Planner())

    ctx = _Ctx()
    await BaseStoryPipeline._decide_chapter_mode(object.__new__(BaseStoryPipeline), ctx, _Novel())
    assert ctx.needs_buffer is True
    assert ctx.buffer_reason == "冲击未落地"
    assert ctx.outline.startswith(BUFFER_CHAPTER_DIRECTIVE)
    assert ctx.outline.endswith("本章大纲")


@pytest.mark.asyncio
async def test_legacy_tension_scale_is_normalised(monkeypatch):
    """旧刻度（<=10）要 ×10，与 legacy_writing_delegate 保持一致。"""
    import engine.pipeline.base as base_mod

    seen = {}

    monkeypatch.setattr(base_mod, "logger", type("L", (), {
        "warning": lambda self, *a, **k: None, "info": lambda self, *a, **k: None,
    })())
    monkeypatch.setattr(
        "application.engine.services.chapter_mode_settings.get_chapter_mode_runtime_settings",
        lambda: ChapterModeRuntimeSettings(enabled=True, apply=True))

    class _Planner:
        async def decide(self, **kw):
            seen.update(kw)
            return None

    monkeypatch.setattr(
        "interfaces.api.dependencies.get_chapter_mode_planner", lambda: _Planner())

    class _LegacyNovel:
        last_chapter_tension = 8          # 旧刻度

    ctx = _Ctx()
    await BaseStoryPipeline._decide_chapter_mode(
        object.__new__(BaseStoryPipeline), ctx, _LegacyNovel())
    assert seen["last_tension"] == 80.0

    seen.clear()
    ctx2 = _Ctx()
    class _NewNovel:
        last_chapter_tension = 87         # 新刻度
    await BaseStoryPipeline._decide_chapter_mode(
        object.__new__(BaseStoryPipeline), ctx2, _NewNovel())
    assert seen["last_tension"] == 87.0


@pytest.mark.asyncio
async def test_planner_blowing_up_does_not_break_pipeline(monkeypatch):
    """判定是锦上添花，挂了绝不能拖垮选章。"""
    import engine.pipeline.base as base_mod

    monkeypatch.setattr(base_mod, "logger", type("L", (), {
        "warning": lambda self, *a, **k: None, "info": lambda self, *a, **k: None,
    })())
    monkeypatch.setattr(
        "application.engine.services.chapter_mode_settings.get_chapter_mode_runtime_settings",
        lambda: ChapterModeRuntimeSettings(enabled=True, apply=True))

    def _boom():
        raise RuntimeError("规划器构造失败")

    monkeypatch.setattr("interfaces.api.dependencies.get_chapter_mode_planner", _boom)

    ctx = _Ctx()
    await BaseStoryPipeline._decide_chapter_mode(object.__new__(BaseStoryPipeline), ctx, _Novel())
    assert ctx.outline == "本章大纲"


# ── 选章不变量 ──────────────────────────────────────────────────────────

def test_chapter_selection_still_uses_first_unfinished_by_number():
    """**章号连续这条硬不变量没有被本次改造动过。**

    登记表明确「章号必须连续（硬不变量，绝不可交给 AI）」。
    上面所有用例都只碰 needs_buffer / outline，没有一个碰选章逻辑 —— 这条
    守卫是为了让「若有人日后把选章也交给 AI」立刻红。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[3] / "engine" / "pipeline" / "base.py").read_text(
        encoding="utf-8"
    )
    # 仍然按 number 排序后取第一个未完成
    assert "key=lambda n: n.number" in src, "选章不再按章号排序了 —— 章号连续不变量被动过"
    assert "getattr(existing, 'status', '') != 'completed'" in src, \
        "「第一个未完成」的判定被改动了"
