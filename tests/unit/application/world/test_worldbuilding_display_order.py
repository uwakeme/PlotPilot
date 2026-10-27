"""第 26 批：世界观**按硬度排**（硬规则别被预算挤掉）+ 截断时**点名丢了哪一维**。

背景（生产实测，第 25 批核实）
------------------------------
创作契约块真实长度 **4379 字**（文风公约 277 + 五维 4083 + 标题），
而正文侧每块上限只有 **2000 字** ⇒ **一半以上装不下** ⇒ 刀口必然落在块中间。

旧顺序（契约顺序 core_rules → geography → society → culture → daily_life）实测结果：
核心法则 ✓、地理生态 ✓ ⇒ 而**文化禁忌 / 关键历史 / 政治体制 / 阶级等级 / 宗教 / 俚语
全丢** ⇒ 模型压根不知道「三更闻唤莫应」「戏票不能捡」这类**硬规则**
⇒ 违反即穿帮，且**它连规则是什么都不知道**。

⇒ 第 26 批：**按硬度排**（不按篇幅排）+ 槽位**声明分节标题** ⇒ 被切时点名。

⚠ 守卫纪律：
  * 走**真实**渲染层（``build_narrative_contract_block``，含文风公约）
    + **真实**截断函数 ⇒ 样本形状必须与**生产一致**（否则测不到那个临界点）；
  * 不复刻任何一层的形状。
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from application.engine.services.context_budget_policy import mark_truncated
from application.world.services.narrative_contract_text import (
    WORLDBUILDING_DIMENSION_DISPLAY_ORDER,
    WORLDBUILDING_DIMENSION_SECTION_HEADERS,
    WORLDBUILDING_FIELD_DIMENSION_OVERRIDE,
    WORLDBUILDING_FIELD_DISPLAY_ORDER,
    build_narrative_contract_block,
    format_worldbuilding_slices_for_prompt,
)
from application.world.worldbuilding_merge import (
    WORLD_BUILDING_DIMENSION_KEYS,
    WORLD_BUILDING_FIELD_KEYS_BY_DIMENSION,
)

ROOT = Path(__file__).resolve().parents[4]

#: 正文侧每块上限（与生产一致）
PROSE_SLOT_CAP = 2000

#: 生产实测：每个字段 ~250 字、**文风公约 ~250 字**
#: ⚠ 文风公约**必须**在样本里 ⇒ 它占 250 字预算
#:   ⇒ 缺了它，样本比生产**宽松** ⇒ 那个「差 ~100 字」的临界点测不到（假绿）。
FIELD_CHARS = 250
STYLE_CHARS = 250


def _slices() -> dict:
    """按**契约**造一份五维切片（各字段等长 ⇒ 只比顺序，不比内容）。"""
    out: dict = {}
    for dim in WORLD_BUILDING_DIMENSION_KEYS:
        out[dim] = {
            key: f"{key}-内容" + "补" * FIELD_CHARS
            for key in WORLD_BUILDING_FIELD_KEYS_BY_DIMENSION.get(dim, ())
        }
    return out


def _bible() -> SimpleNamespace:
    """带**文风公约**的 Bible（生产形状：契约块开头就是它）。"""
    return SimpleNamespace(
        style_notes=[
            SimpleNamespace(category="文风公约", content="文风内容" + "衬" * STYLE_CHARS),
        ],
        world_settings=[],
    )


def _block() -> str:
    """**真实**渲染层产出的完整契约块（含文风公约 + 五维）。"""
    return build_narrative_contract_block(
        bible=_bible(),
        worldbuilding=None,
        worldbuilding_slices=_slices(),
    )


def _kept() -> str:
    """渲染 + **真实**截断（正文侧上限）⇒ 模型实际看得见的那份。"""
    return mark_truncated(
        _block(), PROSE_SLOT_CAP, section_headers=WORLDBUILDING_DIMENSION_SECTION_HEADERS
    )


# ── ① 硬规则必须**挤得进**预算 ─────────────────────────────────────────


def test_the_sample_matches_the_production_shape():
    """⚠ **前提守卫**：样本必须与生产一样「装不下」。

    ⚠ 否则下面的断言会**空过**（样本宽松 ⇒ 什么顺序都能装下）。
    """
    block = _block()

    assert len(block) > PROSE_SLOT_CAP, (
        f"样本只有 {len(block)} 字，装得下 ⇒ 这条守卫验不到东西"
    )
    assert "文风公约" in block, "样本里没有文风公约 ⇒ 与生产形状不一致"


def test_the_hard_rules_survive_the_budget():
    """⚠ 核心价值：禁忌 / 历史 / 政治体制**必须**在模型看得见的那份里。

    ⚠ 旧顺序实测这三个**全丢** ⇒ 模型不知道规则是什么 ⇒ 违反即穿帮。
    """
    kept = _kept()

    for key in ("taboos", "history", "politics"):
        assert f"{key}-内容" in kept, f"硬规则 {key} 被预算挤掉了 ⇒ 模型无从遵守"


def test_the_soft_fields_are_the_ones_that_get_cut():
    """⚠ 反向边界：**软**的（生态 / 消遣 / 宗教）才是先被牺牲的。

    ⇒ 守住「按硬度排」这个口径（不是「按字母序」也不是「按篇幅」）。
    """
    kept = _kept()

    for key in ("ecology", "entertainment", "religion"):
        assert f"{key}-内容" not in kept, f"软字段 {key} 竟然留下来了 ⇒ 顺序没按硬度排"


def test_the_core_rules_are_still_first():
    """核心法则（力量体系/物理/技术）必须**最前**（它是全书最硬的约束）。"""
    text = _block()

    assert text.index("power_system-内容") < text.index("taboos-内容")
    assert text.index("power_system-内容") < text.index("politics-内容")


# ── ② 展示顺序**不碰契约**（前端 / schema 不受影响）─────────────────────


def test_the_contract_order_is_untouched():
    """⚠ 展示顺序是**呈现层**的事 ⇒ 契约（前端 / 提示词 schema 依赖）不许动。"""
    assert WORLD_BUILDING_DIMENSION_KEYS == (
        "core_rules",
        "geography",
        "society",
        "culture",
        "daily_life",
    ), "契约顺序被改了 ⇒ 前端与 schema 会跟着变"


def test_every_declared_dimension_and_field_exists_in_the_contract():
    """⚠ 展示顺序里写的东西必须**存在于契约** ⇒ 不会指向不存在的维度/字段。"""
    for dim in WORLDBUILDING_DIMENSION_DISPLAY_ORDER:
        assert dim in WORLD_BUILDING_DIMENSION_KEYS, f"展示顺序里有契约没有的维度：{dim}"
    for dim, fields in WORLDBUILDING_FIELD_DISPLAY_ORDER.items():
        contract_fields = tuple(WORLD_BUILDING_FIELD_KEYS_BY_DIMENSION.get(dim, ()))
        for field in fields:
            assert field in contract_fields, f"{dim} 的展示顺序里有契约没有的字段：{field}"


def test_a_field_not_in_the_display_order_is_still_rendered(monkeypatch):
    """⚠ 契约新增字段时**不许丢**（展示顺序没列的 ⇒ 按契约顺序追加在末尾）。"""
    from application.world.services import narrative_contract_text as nct

    # 模拟「契约新增了一个字段」
    original = nct.WORLD_BUILDING_FIELD_KEYS_BY_DIMENSION
    patched = dict(original)
    patched["core_rules"] = tuple(original["core_rules"]) + ("brand_new_field",)
    monkeypatch.setattr(nct, "WORLD_BUILDING_FIELD_KEYS_BY_DIMENSION", patched)

    slices = _slices()
    slices["core_rules"]["brand_new_field"] = "新字段-内容"

    text = nct.format_worldbuilding_slices_for_prompt(slices)

    assert "新字段-内容" in text, "契约新增的字段被静默丢掉了"


def test_the_overridden_field_is_rendered_exactly_once():
    """⚠ 跨维调整的字段**不许重复出现**（原组 + 目标组各一次 ⇒ 模型看到两遍）。"""
    text = _block()

    for field in WORLDBUILDING_FIELD_DIMENSION_OVERRIDE:
        assert text.count(f"{field}-内容") == 1, f"{field} 出现了多次"


# ── ③ 截断时**点名丢了哪一维** ─────────────────────────────────────────


def test_the_dropped_dimensions_are_named():
    """⚠ 不点名 ⇒ 模型以为「这个世界没有地理/没有民俗」⇒ **静默失效**。"""
    kept = _kept()

    assert "被截掉的小节" in kept, kept[:200]
    naming = kept.split("】", 1)[0]
    for dim in ("地理生态", "沉浸感细节"):
        assert dim in naming, f"丢掉的一维没被点名：{dim}"


def test_the_section_headers_match_the_rendered_dimension_titles():
    """⚠ 声明的标题必须**就是**渲染出来的标题 ⇒ 否则点名认不出来。"""
    text = _block()

    for header in WORLDBUILDING_DIMENSION_SECTION_HEADERS:
        assert header in text, f"声明的分节标题在内容里不存在：{header}"


def test_the_slot_declares_the_section_headers():
    """⚠ 创作契约槽位必须**声明**分节标题（不声明 ⇒ 截断了也没人点名）。"""
    source = (ROOT / "application/engine/services/context_budget_allocator.py").read_text(
        encoding="utf-8"
    )
    # ⚠ 只看**创作契约那一个构造调用** ⇒ 别的槽位的 section_headers 不算数
    match = re.search(
        r'slots\["narrative_contract"\]\s*=\s*ContextSlot\(.*?\n\s*\)',
        source,
        re.S,
    )
    assert match, "没找到创作契约槽位的构造"
    assert "section_headers=" in match.group(0), (
        "创作契约槽位没有声明 section_headers ⇒ 被切时不会点名"
    )
    assert "WORLDBUILDING_DIMENSION_SECTION_HEADERS" in match.group(0), (
        "声明的标题不是世界观的那一组 ⇒ 点名认不出来"
    )


# ── ④ 文风公约**不受影响**（这次调整不许把它挤掉）─────────────────────


def test_the_style_notes_stay_at_the_front():
    """⚠ 文风公约在五维**之前**（它软，但「文风漂移」是本产品盯着的指标）⇒ 不动它。"""
    text = _block()

    assert "文风公约" in text
    assert text.index("文风公约") < text.index("power_system-内容")
