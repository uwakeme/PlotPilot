"""A-04：Bible 的价值观铁律与阵容规模交回 AI。

背景
----
登记表 A-04 写：「Bible 价值观铁律由模板写死」——
``bible-all/system.md`` 里写死了
「一个优秀的世界必须是【充满缝隙和阶级压迫】」
「人物阵列 3-5 人」「绝不写和平主义者」
「空间折叠 2-3 处」
「五维度底层规则只写【异于现实的设定】或【阶级剥削的规则】」；
``bible-characters/system.md`` 里写死了「主要 3-5 名、次要 2-3 名，共计 5-8 名」
+ 根/骨/网/声强制四层。

核实补充：登记表**漏了第三处** ——
``bible-locations/system.md`` 也写死了「设计 5-10 个核心场景」。

⇒ 这些是**普适性价值判断 + 数量常量**，不是所有题材都成立
（轻松日常、治愈系、硬核悬疑的驱动力完全不同）。

本笔做与不做什么
----------------
**放开**（创作判断）：
- 世界观的核心压力（价值观）—— 由 AI 依据作者创意与题材定；
- 人物阵列 / 空间折叠 / 角色阵容 / 场景数量 —— 由 AI 按实际需要定
  （给出的区间只作为「常见范围」，不是硬指标）。

**保留**（结构要求 / schema）：
- 每个人物必须有【欲望】+【致命弱点】；
- 角色的根/骨/网/声四层设计（写法模板，不是价值判断）；
- ``location_type`` / ``importance`` 的枚举 schema（登记表明确要求保留）。

⚠ 纪律：会被喂给模型的模板里**只能有给模型看的内容** ——
变更理由、issue 编号一律放 ledger / commit message ⇒ 本文件也守这一条。
"""
from __future__ import annotations

import re
from pathlib import Path

NODES = Path(__file__).resolve().parents[4] / "infrastructure/ai/prompt_packages/nodes"
ROOT = Path(__file__).resolve().parents[4]


def _read(node: str, name: str = "system.md") -> str:
    return (NODES / node / name).read_text(encoding="utf-8")


# ── 1) 价值观铁律不再写死 ───────────────────────────────────────────

def test_bible_all_no_longer_demands_a_class_oppression_world():
    """「世界必须充满缝隙和阶级压迫」这类普适价值判断不该再是模板要求。"""
    text = _read("bible-all")

    assert "一个优秀的世界（Bible）必须是【充满缝隙和阶级压迫】" not in text
    assert "充满缝隙和阶级压迫" not in text, "写死的价值观铁律还在"


def test_bible_all_no_longer_forbids_peacemakers():
    """「绝不写和平主义者」不是所有书都适用的规则。

    ⚠ 判据是**祈使用法**（「绝不写…」「必须…（数量）」），不是字面量出现 ——
    模板里**可以**提到这条旧规则（明确告诉模型「不要这么写」），
    但不能**要求**它这么写。
    ⇒ 这里查的是「绝不写和平主义者」前面有没有祈使标记。
    """
    text = _read("bible-all")

    for match in re.finditer(r"绝不写和平主义者", text):
        # 取**前后窗口**（跨行，因为那句话本身跨 3 行）——
        # 免责表述可能出现在引用之前或之后。
        window = text[max(0, match.start() - 120) : match.end() + 120]
        assert ("不要" in window) or ("不是所有书都适用" in window), (
            "「绝不写和平主义者」被当成要求写回去了"
        )
    # 但**结构要求**仍在：欲望 + 致命弱点
    assert "欲望" in text
    assert "致命弱点" in text


def test_bible_all_no_longer_restricts_five_dimension_rules():
    """「只写【异于现实的设定】或【阶级剥削的规则】」把五维锁死了一种世界观。"""
    text = _read("bible-all")

    assert "只写【异于现实的设定】或【阶级剥削的规则】" not in text


def test_bible_all_now_lets_the_ai_decide_the_world_pressure():
    """新的说法必须存在：核心压力由 AI 依据作者创意与题材定。"""
    text = _read("bible-all")

    assert "核心压力由你依据作者创意与题材定" in text
    assert "worldbuilding.style" in text


# ── 2) 数量铁律不再写死 ─────────────────────────────────────────────

def test_bible_all_character_and_location_counts_are_not_hardcoded():
    text = _read("bible-all")

    assert "人物阵列（3-5人）" not in text
    assert "空间折叠（2-3处）" not in text
    # 新说法：常见范围 + 由你定
    assert "常见范围" in text
    assert "不是硬指标" in text


def test_bible_characters_cast_size_is_not_hardcoded():
    """角色阵容规模是创作判断。"""
    text = _read("bible-characters")

    assert "主要角色 3-5 名（含主角、核心对立角色），次要角色 2-3 名，共计 5-8 名。" not in text
    assert "常见范围" in text
    assert "不是硬指标" in text


def test_bible_locations_scene_count_is_not_hardcoded():
    """**登记表漏记的第三处**：场景数量也是写死的。"""
    text = _read("bible-locations")

    assert "设计5-10个核心场景" not in text
    assert "常见范围" in text


# ── 3) 该保留的保留 ─────────────────────────────────────────────────

def test_bible_characters_keeps_the_four_layer_design():
    """根/骨/网/声是**写法模板**（不是价值判断）⇒ 保留。"""
    text = _read("bible-characters")

    for layer in ("**根**", "**骨**", "**网**", "**声**"):
        assert layer in text, f"{layer} 层被误删了"


def test_bible_characters_keeps_the_importance_enum():
    """``importance`` 枚举是 schema（登记表明确要求保留）⇒ 保留。"""
    text = _read("bible-characters")

    for value in ("primary", "secondary", "minor"):
        assert value in text


def test_bible_locations_keeps_the_field_enums():
    """``location_type`` / ``importance`` 枚举是 schema ⇒ 保留。"""
    text = _read("bible-locations")

    for value in ("city", "region", "building", "faction", "realm"):
        assert value in text
    for value in ("core", "important", "normal"):
        assert value in text


# ── 4) 模板里不能有面向维护者的说明 ─────────────────────────────────

def test_templates_do_not_contain_maintainer_facing_notes():
    """模板会被喂给模型 ⇒ 只能有给模型看的内容。

    ⚠ 变更理由、issue 编号、踩坑记录一律放 ledger / commit message。
    """
    for node in ("bible-all", "bible-characters", "bible-locations"):
        text = _read(node)
        assert "A-04" not in text, f"{node}/system.md 里出现了面向维护者的 issue 编号"
        # 「旧模板」「曾经写死」这类回顾性说明也不该进模型上下文
        for phrase in ("旧模板", "写死过", "登记表"):
            assert phrase not in text, f"{node}/system.md 里出现了维护者说明: {phrase!r}"


def test_prompt_bump_happened():
    """prompt 包改了 ⇒ bundle 版本必须 bump（否则存量安装不生效）。

    ⚠ 判据是「**不低于** 5.44」而不是「正好 5.44」——
    同一批里 A-06 又 bump 了一次 ⇒ 版本继续往上走。
    ⇒ 真正要守的是「A-04 这笔的 changelog 条目在，且版本已经离开 5.41」。
    """
    meta = (
        ROOT / "infrastructure/ai/prompt_packages/bundle_meta.yaml"
    ).read_text(encoding="utf-8")
    version = re.search(r"^version:\s*(\S+)", meta, re.M).group(1)
    major, minor, *_ = version.split(".")
    assert (int(major), int(minor)) >= (5, 44), f"bundle 版本没 bump（当前 {version}）"
    assert "v5.44.0:" in meta, "changelog 里缺 v5.44.0（A-04）条目"
    assert "A-04" in meta, "changelog 里没写清这一笔是 A-04"
