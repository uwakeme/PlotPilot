"""A-06：世界观字段粒度不再由提示词写死字数区间。

背景
----
登记表 A-06 写：「世界观字数夹逼与五维键集」——
每字段 80-160 字（user.md）/ 20-500 字夹逼（``worldbuilding_schema.py``）；
五维键集写死。

核实（2026-10-22）修正了两点
----------------------------
1. **五维键集已不写死**（登记表这条过期）：
   ``WORLD_BUILDING_DIMENSION_KEYS`` 现在由
   ``shared/taxonomy/worldbuilding_contract_cn_v1.yaml`` 驱动
   （``worldbuilding_merge.py`` 读契约）⇒ 本条不涉及五维键集。
2. **「80-160 字」有第二个来源**（登记表只说 user.md）：
   ``worldbuilding_schema.py::build_fields_desc_for_prompt`` 里**逐字段**又注入了一遍
   ⇒ 只改 prompt 改不掉 Python 生成的那份（这是本条要修的「同一逻辑写两份」）。

本笔做与不做什么
----------------
**放开**：具体写几句、每句多长 ⇒ 由 AI 按该字段实际要说的内容量自定。

**保留**（真不变量）：
- 单段 / 不得换行 / 不得嵌套 JSON 或英文键 —— JSON 解析与前端渲染的要求；
- **上下限护栏**：不少于 20 字、不多于 500 字 —— 由代码的 pydantic 夹逼强制
  （防撑爆上下文、防交白卷），**不该由提示词代答**。

⇒ 字段粒度提示文案收敛到**单一来源**
（``worldbuilding_schema.WORLDBUILDING_FIELD_FORMAT_HINT``）。
"""
from __future__ import annotations

import re
from pathlib import Path

from application.world.worldbuilding_schema import (
    MAX_WORLDBUILDING_FIELD_CHARS,
    MIN_WORLDBUILDING_FIELD_CHARS,
    WORLDBUILDING_FIELD_FORMAT_HINT,
    build_fields_desc_for_prompt,
    validate_complete_dimension_fields,
)

ROOT = Path(__file__).resolve().parents[4]
USER_MD = ROOT / "infrastructure/ai/prompt_packages/nodes/bible-worldbuilding/user.md"


def _user_md() -> str:
    return USER_MD.read_text(encoding="utf-8")


# ── 1) 字数区间不再写死 ─────────────────────────────────────────────

def test_prompt_no_longer_demands_a_fixed_word_count():
    """user.md 里不该再有「80-160 字」这种固定区间。"""
    text = _user_md()

    assert "80-160" not in text, "user.md 还在要求固定字数区间"
    assert "2-3句" not in text, "user.md 还在要求固定句数"


def test_generated_fields_desc_no_longer_demands_a_fixed_word_count():
    """**第二个来源**也必须改掉（只改 prompt 是不够的）。

    ⚠ 这条是本条的核心 —— 登记表只提了 user.md 那个来源，
    而 Python 逐字段注入的那份**改 prompt 改不掉**。
    """
    desc = build_fields_desc_for_prompt()

    assert "80-160" not in desc, "逐字段注入里还在写死字数区间"
    assert "2-3句" not in desc, "逐字段注入里还在写死句数"


def test_both_sources_agree_on_the_bounds():
    """两个来源的**口径**必须一致（上下限护栏）。

    ⇒ 判据是「都提到 20 / 500 这两个数」，不是逐字相同
    （user.md 是自然语言，fields_desc 是逐字段注入）。
    """
    text = _user_md()
    desc = build_fields_desc_for_prompt()

    for source, label in ((text, "user.md"), (desc, "fields_desc")):
        assert str(MIN_WORLDBUILDING_FIELD_CHARS) in source, f"{label} 没写下限"
        assert str(MAX_WORLDBUILDING_FIELD_CHARS) in source, f"{label} 没写上限"


# ── 2) 真不变量保留 ─────────────────────────────────────────────────

def test_format_constraints_are_still_stated():
    """单段 / 不得换行 / 不得嵌套 —— 这些是 JSON 解析要求，不能丢。"""
    text = _user_md()
    desc = build_fields_desc_for_prompt()

    for source, label in ((text, "user.md"), (desc, "fields_desc")):
        assert "单段" in source, f"{label} 丢了「单段」"
        assert "不得换行" in source, f"{label} 丢了「不得换行」"
        assert "勿嵌套JSON" in source or "不得嵌套对象或数组" in source, (
            f"{label} 丢了「不得嵌套」"
        )


def test_code_still_clamps_the_field_length():
    """**代码护栏**仍在：太短/太长的字段会被拒。"""
    dim = "core_rules"
    fields = list(
        __import__(
            "application.world.worldbuilding_schema", fromlist=["x"]
        ).WORLDBUILDING_DIMENSION_DEFS[dim]["fields"].keys()
    )
    short = {name: "太短" for name in fields}
    assert validate_complete_dimension_fields(dim, short) == {}, "短字段不该通过"

    long_value = "字" * (MAX_WORLDBUILDING_FIELD_CHARS + 10)
    too_long = {name: long_value for name in fields}
    assert validate_complete_dimension_fields(dim, too_long) == {}, "超长字段不该通过"


def test_a_reasonable_field_still_passes():
    """正常长度的字段仍要能过（别把闸门关死）。"""
    dim = "core_rules"
    fields = list(
        __import__(
            "application.world.worldbuilding_schema", fromlist=["x"]
        ).WORLDBUILDING_DIMENSION_DEFS[dim]["fields"].keys()
    )
    ok = {name: "这是一段足够长、且能说明该字段要表达内容的世界观描述文本。" for name in fields}
    assert validate_complete_dimension_fields(dim, ok), "正常字段不该被拒"


# ── 3) 单一来源 ─────────────────────────────────────────────────────

def test_field_format_hint_is_a_single_source():
    """字段粒度提示文案必须由**一个常量**产出（两处调用共用）。"""
    assert "80-160" not in WORLDBUILDING_FIELD_FORMAT_HINT
    assert "2-3句" not in WORLDBUILDING_FIELD_FORMAT_HINT
    assert "单段" in WORLDBUILDING_FIELD_FORMAT_HINT
    assert "不得换行" in WORLDBUILDING_FIELD_FORMAT_HINT
    assert "不少于20字" in WORLDBUILDING_FIELD_FORMAT_HINT
    assert "不多于500字" in WORLDBUILDING_FIELD_FORMAT_HINT


def test_generated_desc_uses_the_shared_hint():
    desc = build_fields_desc_for_prompt()
    assert WORLDBUILDING_FIELD_FORMAT_HINT in desc


# ── 4) 版本已 bump ──────────────────────────────────────────────────

def test_prompt_bump_happened():
    """prompt 包改了 ⇒ bundle 版本必须 bump（否则存量安装不生效）。

    ⚠ 判据是「**不低于** 5.45」而不是「正好 5.45」——
    后续若再 bump，这条不该假红；真正要守的是「A-06 的 changelog 条目在」。
    """
    meta = (
        ROOT / "infrastructure/ai/prompt_packages/bundle_meta.yaml"
    ).read_text(encoding="utf-8")
    version = re.search(r"^version:\s*(\S+)", meta, re.M).group(1)
    major, minor, *_ = version.split(".")
    assert (int(major), int(minor)) >= (5, 45), f"bundle 版本没 bump（当前 {version}）"
    assert "v5.45.0:" in meta, "changelog 里缺 v5.45.0（A-06）条目"
    assert "A-06" in meta, "changelog 里没写清这一笔是 A-06"


def test_the_field_char_bounds_are_pinned_by_value():
    """常量本身的值必须被钉住。

    ⚠ 断言用**字面量**（"不少于20字"）而不是 ``str(MIN_...)``：
    HINT 是用常量拼出来的 f-string ⇒ 用常量断言它是**恒真式**。
    """
    assert MIN_WORLDBUILDING_FIELD_CHARS == 20
    assert MAX_WORLDBUILDING_FIELD_CHARS == 500
