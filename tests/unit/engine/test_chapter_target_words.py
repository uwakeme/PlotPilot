"""D-05：每章字数目标 —— AI 有了输出通道，但改不了用户的节奏规则。

背景
----
登记表 D-05 的核心指控核实成立：**全链路没有任何 AI 输出通道**能设定本章字数。
``PlanningEnvelope.target_chapter_words`` 是**输入**（来自
``novel.target_words_per_chapter``），``PlanAtomSpec.weight`` 只是相对权重。
用户在向导选的章长一路进正文 prompt，但 AI 完全无权参与。

三条代码不变量（登记表「代码保留的不变量」列）
----------------------------------------------
1. 500-20000 钳制 —— 复用既有 ``clamp_chapter_target_words``，不另立上限
2. 偏离用户设定不得超过上限（默认 ±30%）—— 用户明确选过章长
3. 卷/部总量由上层规划负责，本模块不越权

三态红线
--------
AI 没给（缺字段 / null / 非数值）⇒ 沿用用户设定，**且必须说明这是「AI 没说话」**
而不是「AI 认为就该用设定」。
"""
import pytest

from application.engine.services.chapter_target_resolver import (
    DEFAULT_MAX_DEVIATION_RATIO,
    resolve_chapter_target_words,
)
from application.engine.services.chapter_target_settings import (
    ChapterTargetRuntimeSettings,
    get_chapter_target_runtime_settings,
)
from application.blueprint.services.chapter_preplanning_service import (
    ChapterPreplanningService,
    ChapterPreplanResult,
)


# ── 三条不变量 ──────────────────────────────────────────────────────────

def test_proposal_inside_band_is_accepted():
    """用户设 2500，AI 提议 2600（+4%）⇒ 采纳。"""
    resolved, note = resolve_chapter_target_words(2500, 2600)
    assert resolved == 2600
    assert "AI 提议" in note


def test_proposal_far_outside_band_is_rejected():
    """**关键护栏**：用户设 2500，AI 提议 8000（+220%）⇒ 必须拒绝。

    单章字数由 AI 定是可以的，但让它把整本书的节奏规则改掉不行。
    """
    resolved, note = resolve_chapter_target_words(2500, 8000)
    assert resolved == 2500, "AI 越界改了用户的节奏设定"
    assert "超过" in note and "拒绝" in note or "沿用" in note


def test_downward_deviation_is_also_guarded():
    """下调同样受护栏 —— 否则 AI 可以把爆点章写成 300 字。"""
    resolved, _ = resolve_chapter_target_words(2500, 500)
    assert resolved == 2500


def test_hard_cap_is_enforced_even_inside_band():
    """500-20000 是绝对红线，**即使落在 band 内也不得越过**。

    ⚠️ 取样点很关键：不能用「用户 20000 / AI 提 30000」——那偏差 50% 已超
    ±30%，会先被偏差护栏拒掉，压根走不到钳制，钳制被移除也测不出来。
    必须取「band 内但越硬上限」的点：
      - 上限：用户 20000 / AI 提 24000（+20%，在 band 内，但 > 20000）
      - 下限：用户 500 / AI 提 400（-20%，在 band 内，但 < 500）
    """
    resolved, _ = resolve_chapter_target_words(20_000, 24_000)
    assert resolved == 20_000, (
        f"提议 24000 在 band 内但越过 20000 硬上限，实际得到 {resolved}"
    )

    resolved2, _ = resolve_chapter_target_words(500, 400)
    assert resolved2 == 500, (
        f"提议 400 在 band 内但跌破 500 硬下限，实际得到 {resolved2}"
    )


def test_clamp_runs_before_the_deviation_check():
    """钳制必须**先于**偏差判定 —— 否则「越界再被拉回」的路径语义会错。"""
    # 提议 30000 → 先钳到 20000 → 偏差 0% → 采纳 20000
    resolved, note = resolve_chapter_target_words(20_000, 30_000)
    assert resolved == 20_000
    assert "AI 提议" in note, "钳制后的值应视为可采纳的提议"


def test_custom_band_is_respected():
    assert resolve_chapter_target_words(1000, 1500)[0] == 1000, "默认 ±30% 之外"
    assert resolve_chapter_target_words(
        1000, 1500, max_deviation_ratio=0.8
    )[0] == 1500, "放宽到 ±80% 后应采纳"


def test_zero_band_accepts_only_exact_match():
    assert resolve_chapter_target_words(2500, 2600, max_deviation_ratio=0.0)[0] == 2500
    assert resolve_chapter_target_words(2500, 2500, max_deviation_ratio=0.0)[0] == 2500


def test_default_band_is_thirty_percent():
    assert DEFAULT_MAX_DEVIATION_RATIO == 0.30
    # 刚好 30% 边界（2500 * 1.3 = 3250）
    assert resolve_chapter_target_words(2500, 3250)[0] == 3250
    # 超出一点点就拒绝
    assert resolve_chapter_target_words(2500, 3260)[0] == 2500


# ── 三态 ────────────────────────────────────────────────────────────────

def test_no_proposal_keeps_user_setting_and_says_so():
    """AI 没说话 ≠「AI 认为就该用设定」—— 说明里必须写出来。"""
    resolved, note = resolve_chapter_target_words(2500, None)
    assert resolved == 2500
    assert "未给出" in note


def test_unparseable_proposal_keeps_user_setting():
    """无法转成数字的提议一律视为「AI 没给」，取值不受影响。"""
    for bad in ("两千五", "", {}, [], 3.7j, object()):
        resolved, note = resolve_chapter_target_words(2500, bad)
        assert resolved == 2500, f"{bad!r} 不该改变取值"


def test_numeric_edge_proposals_are_clamped_not_treated_as_missing():
    """这些**能**转成数字 ⇒ 走钳制 + band 判定，而不是「不可解析」。"""
    # 500 是数字，会被钳制后按 band 判定（偏离过大 ⇒ 拒绝）
    assert resolve_chapter_target_words(2500, 500)[0] == 2500
    # True 能转成 1，钳到 500 后同样按 band 判定
    assert resolve_chapter_target_words(2500, True)[0] == 2500


def test_invalid_user_target_falls_back_to_proposal():
    """用户设定本身非法时，退回按 AI 提议（并钳制），而不是抛异常。

    注意 clamp 会把 0 抬到 500 —— 所以判定必须看**原始值**。
    """
    resolved, note = resolve_chapter_target_words(0, 3000)
    assert resolved == 3000
    assert "用户设定非法" in note


def test_invalid_user_target_and_no_proposal_falls_to_floor():
    resolved, note = resolve_chapter_target_words(0, None)
    assert resolved == 500, "两者都不可用时应落在钳制下限"
    assert "下限" in note


# ── 开关 ────────────────────────────────────────────────────────────────

def test_settings_default_disabled():
    """默认关闭 ⇒ ctx.target_word_count 完全不动。"""
    s = ChapterTargetRuntimeSettings()
    assert s.enabled is False
    assert s.max_deviation_ratio == 0.30
    assert get_chapter_target_runtime_settings().enabled is False


def test_chapter_target_switch_is_independent_of_others():
    """三项是不同决策，开一个不该顺带开另一个。"""
    from application.engine.services.chapter_mode_settings import (
        ChapterModeRuntimeSettings,
    )
    from application.engine.services.context_weight_settings import (
        ContextWeightRuntimeSettings,
    )

    keys = [
        set(ChapterTargetRuntimeSettings.__dataclass_fields__),
        set(ChapterModeRuntimeSettings.__dataclass_fields__),
        set(ContextWeightRuntimeSettings.__dataclass_fields__),
    ]
    assert keys[0] != keys[1] and keys[1] != keys[2] and keys[0] != keys[2]
    # 三个模块互不 import 对方的 settings
    src = (ChapterTargetRuntimeSettings.__module__)
    assert "context_weight" not in src and "chapter_mode" not in src


# ── 输出通道解析 ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "data,expected",
    [
        ({"target_words": 2600}, 2600),
        ({"target_words": "2600"}, 2600),
        ({"target_words": None}, None),
        ({}, None),
        ({"target_words": "两千五"}, None),
        ({"target_words": 0}, None),
        ({"target_words": -100}, None),
        ({"target_words": True}, None),          # bool 不当数字用
        ({"target_words": 3.7}, 3),              # 浮点截断为 int
    ],
)
def test_proposal_parsing_is_strict(data, expected):
    """**缺失/null/非数值一律 None**，绝不用默认值顶替。"""
    got = ChapterPreplanningService._extract_proposed_target_words(data)
    assert got == expected, f"{data} → {got}，期望 {expected}"


def test_result_dataclass_defaults_to_no_proposal():
    r = ChapterPreplanResult(text="计划")
    assert r.text == "计划"
    assert r.proposed_target_words is None, "默认不得带上任何字数提议"


# ── 管线接线 ────────────────────────────────────────────────────────────

class _Ctx:
    def __init__(self):
        self.novel_id = "n1"
        self.chapter_number = 7
        self.target_word_count = 2500
        self.metadata = {}
        self.outline = "原大纲"


@pytest.mark.asyncio
async def test_disabled_leaves_target_word_count_untouched(monkeypatch):
    from engine.pipeline.base import BaseStoryPipeline

    monkeypatch.setattr(
        "application.engine.services.chapter_target_settings.get_chapter_target_runtime_settings",
        lambda: ChapterTargetRuntimeSettings(enabled=False))

    ctx = _Ctx()
    await BaseStoryPipeline._apply_chapter_target_words(
        object.__new__(BaseStoryPipeline), ctx, 8000)
    assert ctx.target_word_count == 2500


@pytest.mark.asyncio
async def test_enabled_accepts_in_band_proposal(monkeypatch):
    from engine.pipeline.base import BaseStoryPipeline

    monkeypatch.setattr(
        "application.engine.services.chapter_target_settings.get_chapter_target_runtime_settings",
        lambda: ChapterTargetRuntimeSettings(enabled=True, max_deviation_ratio=0.30))

    ctx = _Ctx()
    await BaseStoryPipeline._apply_chapter_target_words(
        object.__new__(BaseStoryPipeline), ctx, 3000)
    assert ctx.target_word_count == 3000
    assert "chapter_target_words_note" in ctx.metadata


@pytest.mark.asyncio
async def test_enabled_rejects_out_of_band_proposal(monkeypatch):
    from engine.pipeline.base import BaseStoryPipeline

    monkeypatch.setattr(
        "application.engine.services.chapter_target_settings.get_chapter_target_runtime_settings",
        lambda: ChapterTargetRuntimeSettings(enabled=True, max_deviation_ratio=0.30))

    ctx = _Ctx()
    await BaseStoryPipeline._apply_chapter_target_words(
        object.__new__(BaseStoryPipeline), ctx, 9000)
    assert ctx.target_word_count == 2500, "越界提议被采纳了"
    assert "沿用用户设定" in ctx.metadata["chapter_target_words_note"]


@pytest.mark.asyncio
async def test_enabled_with_no_proposal_keeps_setting(monkeypatch):
    from engine.pipeline.base import BaseStoryPipeline

    monkeypatch.setattr(
        "application.engine.services.chapter_target_settings.get_chapter_target_runtime_settings",
        lambda: ChapterTargetRuntimeSettings(enabled=True, max_deviation_ratio=0.30))

    ctx = _Ctx()
    await BaseStoryPipeline._apply_chapter_target_words(
        object.__new__(BaseStoryPipeline), ctx, None)
    assert ctx.target_word_count == 2500
    assert "未给出" in ctx.metadata["chapter_target_words_note"]


@pytest.mark.asyncio
async def test_resolver_blowing_up_does_not_break_pipeline(monkeypatch):
    """裁决是锦上添花，挂了绝不能拖垮章前剧本生成。"""
    import engine.pipeline.base as base_mod
    from engine.pipeline.base import BaseStoryPipeline
    import application.engine.services.chapter_target_resolver as resolver_mod

    monkeypatch.setattr(
        "application.engine.services.chapter_target_settings.get_chapter_target_runtime_settings",
        lambda: ChapterTargetRuntimeSettings(enabled=True))

    def _boom(*a, **k):
        raise RuntimeError("裁决函数炸了")

    # 直接让裁决函数抛异常 —— 只 patch logger 是没用的，那条路根本不会走到
    monkeypatch.setattr(resolver_mod, "resolve_chapter_target_words", _boom)
    monkeypatch.setattr(base_mod, "logger", type("L", (), {
        "warning": lambda self, *a, **k: None, "info": lambda self, *a, **k: None,
    })())

    ctx = _Ctx()
    await BaseStoryPipeline._apply_chapter_target_words(
        object.__new__(BaseStoryPipeline), ctx, 3000)
    assert ctx.target_word_count == 2500, "裁决炸了却仍改写了取值"
