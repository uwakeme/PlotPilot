"""第 22 批：K 类·越界角色的**确定性核对**（白名单变体比对）。

背景
----
K 类（越界角色）此前**只由 LLM 判**，没有机器口径 ⇒ LLM 把白名单内角色报成越界
时**没人能驳回**；而「K 类报得出来」在生产上一直**验不到**（第 21 批实测）。

⇒ 现在对 K 类命中做**集合核对**（不依赖中文分词）：
   名字落在白名单变体集合里 ⇒ **驳回**；不在 ⇒ **确认**；判不了 ⇒ **不下结论**。

⚠ 守卫纪律：
- **双向钉住**：已钉住的形态（裸名/别名/角色前缀别名/名部分）一个都不许漏；
  合法写法（白名单外的真越界）**零误报**。
- 判不了时**保守保留**（不许把真问题误当成误报删掉）。
"""
from __future__ import annotations

from application.audit.services.character_whitelist_check import (
    CONFIRMED,
    REJECTED,
    REJECTED_CATEGORY,
    UNDECIDED,
    classify_rogue_character_hit,
    expand_whitelist,
    is_rogue_category,
    normalize_name_text,
)

#: 生产书《雨夜第七个观众》的**真实**白名单形态（带别名/角色前缀/称谓）
WHITELIST_ENTRIES = [
    "沈砚青",
    "楚半声（本名楚明谦）",
    "聂九枝（艺名玉惊鸿）",
    "闻鹤年（市井称「闻聋子」）",
    "郑铁珊",
    "魏长庚",
    "贺九（贺松龄，人称九爷）",
    "郎世安",
]


def _variants() -> set[str]:
    return expand_whitelist(WHITELIST_ENTRIES)


# ── 1) 类别判定 ──────────────────────────────────────────────────────────


def test_the_rogue_category_is_recognized_in_every_written_form():
    """LLM 契约里是单个字母，但也容忍模型写成「K类」「K 类·越界角色」。"""
    assert is_rogue_category("K")
    assert is_rogue_category("k")
    assert is_rogue_category("K类")
    assert is_rogue_category("K 类·越界角色")
    assert is_rogue_category(" K ")


def test_the_other_categories_are_not_mistaken_for_the_rogue_one():
    """⚠ J 类（生造词）尤其要分清 —— 两者都是新增的。"""
    for category in ("A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "", "句式", "严禁词"):
        assert not is_rogue_category(category), category


# ── 2) 白名单展开：已钉住的形态一个都不许漏 ───────────────────────────────


def test_the_bare_names_are_expanded():
    variants = _variants()
    for name in ("沈砚青", "楚半声", "聂九枝", "闻鹤年", "郑铁珊", "魏长庚", "贺九", "郎世安"):
        assert name in variants, f"裸名 {name} 没被展开"


def test_the_aliases_inside_the_parentheses_are_expanded():
    """别名：艺名 / 本名 / 市井称 / 人称 —— 都要能匹配。"""
    variants = _variants()
    for alias in ("玉惊鸿", "楚明谦", "闻聋子", "九爷", "贺松龄"):
        assert alias in variants, f"别名 {alias} 没被展开"


def test_the_role_prefixes_are_stripped_from_the_aliases():
    """「艺名玉惊鸿」要能匹配「玉惊鸿」（角色前缀不是名字的一部分）。"""
    variants = _variants()
    assert "艺名玉惊鸿" in variants
    assert "玉惊鸿" in variants
    assert "本名楚明谦" in variants
    assert "楚明谦" in variants
    assert "人称九爷" in variants
    assert "九爷" in variants
    assert "闻聋子" in variants


def test_the_given_name_part_is_expanded_for_three_character_names():
    """「沈砚青」→「砚青」也算白名单（模型可能只写名）。"""
    variants = _variants()
    assert "砚青" in variants
    assert "九枝" in variants


def test_single_character_variants_are_never_kept():
    """⚠ 单字变体会乱匹配（「九」「青」这种）⇒ 一律丢掉。"""
    variants = _variants()
    assert not any(len(v) == 1 for v in variants), variants


# ── 3) 核对：白名单内 ⇒ 驳回 ────────────────────────────────────────────


def test_a_whitelisted_character_is_rejected():
    """模型把白名单内角色报成越界 ⇒ 驳回。"""
    verdict, name = classify_rogue_character_hit("沈砚青", _variants())
    assert verdict == REJECTED
    assert name == "沈砚青"


def test_a_whitelisted_alias_is_rejected():
    """别名同样要驳回（模型看见「玉惊鸿」不在裸名列表里就报 ⇒ 那是误报）。"""
    for alias in ("玉惊鸿", "楚明谦", "闻聋子", "九爷"):
        verdict, name = classify_rogue_character_hit(alias, _variants())
        assert verdict == REJECTED, f"{alias} 没被驳回"
        assert name == alias


def test_a_name_with_a_whitelisted_core_is_rejected():
    """「沈砚青妹」这种（多带一两个字）也算白名单内。"""
    verdict, _ = classify_rogue_character_hit("沈砚青妹", _variants())
    assert verdict == REJECTED


def test_the_punctuation_around_the_name_does_not_break_the_check():
    """命中原文里常带引号/标点 ⇒ 剥掉再比。"""
    verdict, name = classify_rogue_character_hit("「聂九枝」", _variants())
    assert verdict == REJECTED
    assert name == "聂九枝"


# ── 4) 核对：白名单外 ⇒ 确认（**K 类的正例在这一层可验**）─────────────────


def test_a_genuinely_rogue_character_is_confirmed():
    """⚠ 这就是「K 类**报得出来**」那半边的可测点。"""
    verdict, name = classify_rogue_character_hit("柳三娘", _variants())
    assert verdict == CONFIRMED
    assert name == "柳三娘"


def test_a_rogue_name_sharing_a_surname_is_not_mistaken_for_the_whitelist():
    """「郑铁生」≠「郑铁珊」⇒ 不能被驳回。"""
    verdict, _ = classify_rogue_character_hit("郑铁生", _variants())
    assert verdict == CONFIRMED


def test_a_rogue_name_sharing_the_given_name_is_not_mistaken_either():
    """「砚清」≠「砚青」⇒ 不能被驳回。"""
    verdict, _ = classify_rogue_character_hit("砚清", _variants())
    assert verdict == CONFIRMED


# ── 5) 判不了时：保守保留，不下结论 ────────────────────────────────────


def test_an_empty_whitelist_leaves_the_verdict_undecided():
    """白名单为空（取不到）⇒ 不下结论（不许臆测）。"""
    verdict, _ = classify_rogue_character_hit("柳三娘", [])
    assert verdict == UNDECIDED


def test_an_empty_pattern_leaves_the_verdict_undecided():
    verdict, _ = classify_rogue_character_hit("", _variants())
    assert verdict == UNDECIDED


def test_a_long_sentence_leaves_the_verdict_undecided():
    """命中是一整句话 ⇒ 判不了（不许拿片段去比）。"""
    verdict, _ = classify_rogue_character_hit(
        "正文里出现了一个白名单之外的有名字的角色柳三娘，她自称是戏院的账房", _variants()
    )
    assert verdict == UNDECIDED


def test_a_title_suffixed_name_leaves_the_verdict_undecided():
    """「郑探长」既可能是别名也可能是新角色 ⇒ 判不了。"""
    verdict, _ = classify_rogue_character_hit("郑探长", _variants())
    assert verdict == UNDECIDED


def test_the_rejected_category_is_distinguishable_from_a_real_rogue_one():
    """⚠ 驳回项与真越界**必须可分辨**（否则面板上分不出哪个是真问题）。"""
    assert REJECTED_CATEGORY != "K"
    assert "越界角色" in REJECTED_CATEGORY


# ── 6) 名字归一 ─────────────────────────────────────────────────────────


def test_the_name_text_is_stripped_of_punctuation():
    assert normalize_name_text("「柳三娘」") == "柳三娘"
    assert normalize_name_text("  柳三娘， ") == "柳三娘"
    assert normalize_name_text("") == ""
