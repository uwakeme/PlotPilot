from pathlib import Path

import pytest

from application.core.taxonomy import opening_profiles as opening_profiles_module
from application.core.taxonomy.opening_profiles import (
    OpeningProfileError,
    get_opening_profile_bundle_cached,
    resolve_opening_profile,
    split_genre_label,
)


def test_split_genre_label_accepts_market_separators():
    assert split_genre_label("都市 / 都市异能") == ("都市", "都市异能")
    assert split_genre_label("都市-都市异能") == ("都市", "都市异能")
    assert split_genre_label("都市异能") == ("都市异能", "")


def test_resolve_secondary_profile_from_config_asset():
    profile = resolve_opening_profile("都市 / 都市异能")

    assert profile is not None
    assert profile.genre_major == "都市"
    assert profile.genre_theme == "都市异能"
    assert profile.source_level == "secondary"
    variables = profile.as_variables()
    assert variables["genre_opening_profile"]["opening_mechanism"]
    assert variables["genre_reader_contract"]["reader_promise"]
    assert variables["genre_rhythm_constraints"]["payoff_interval"]


def test_resolve_secondary_alias_without_major_when_unique():
    profile = resolve_opening_profile("都市异能")

    assert profile is not None
    assert profile.genre_major == "都市"
    assert profile.genre_theme == "都市异能"
    assert profile.source_level == "secondary_alias"


def test_resolve_primary_alias_from_config_without_code_fallback():
    profile = resolve_opening_profile("修仙 / 悬疑")

    assert profile is not None
    assert profile.genre_major == "仙侠"
    assert profile.source_level == "primary_default"


def test_missing_profile_degrades_to_generic_contract_instead_of_blocking():
    """A-08：画像缺失**不再阻塞生成** —— 降级到通用契约。

    改造前：``strict=True`` 直接 raise ⇒ 新题材（无画像）根本开不了书。
    登记表 A-08 记的「禁止开书」就是这个 raise。

    现在：strict 模式下也返回**通用契约**，并用 ``source_level="generic"``
    **如实标记**来源 ⇒「用了通用契约」与「用了该题材画像」在数据上可分辨。
    """
    profile = resolve_opening_profile("不存在分类 / 不存在二级")

    assert profile is not None
    assert profile.source_level == "generic"
    # 通用契约的文本来自配置资产，不是代码写死的
    assert profile.reader_contract["first_screen_hook"]
    assert profile.reader_contract["chapter_1_payoff"]
    assert profile.reader_contract["chapter_3_lock"]
    assert profile.reader_contract["chapter_10_loop"]


def test_generic_contract_text_lives_in_config_not_in_code():
    """契约文本必须来自配置资产（``generic_contract`` 段）。

    这条守的是 A-08 的核心：读者契约文本**不许**再写死在
    ``opening_profiles.py`` 里（那与模块 docstring「不在代码里维护具体题材套路」
    自相矛盾）。
    """
    bundle = get_opening_profile_bundle_cached()

    assert "generic_contract" in bundle
    generic = bundle["generic_contract"]
    for key in (
        "first_screen_hook",
        "chapter_1_payoff",
        "chapter_3_lock",
        "chapter_10_loop",
    ):
        assert str(generic.get(key) or "").strip(), f"generic_contract.{key} 缺失"

    # 代码里不许再出现这些契约文本的字面量
    source = (
        Path(opening_profiles_module.__file__).read_text(encoding="utf-8")
    )
    for key in (
        "first_screen_hook",
        "chapter_1_payoff",
        "chapter_3_lock",
        "chapter_10_loop",
    ):
        # 键名可以出现（代码要读它），但不许出现「键名 + 冒号 + 中文句子」的写死形式
        assert f'"{key}": "' not in source, f"{key} 的文本被写死在代码里了"


def test_resolved_profile_carries_its_own_reader_promise():
    """命中题材画像时，契约里的 reader_promise 必须是**该题材的**承诺。"""
    profile = resolve_opening_profile("都市 / 都市异能")

    assert profile is not None
    assert profile.source_level == "secondary"
    assert profile.reader_contract["reader_promise"]
    # 通用契约的四条在命中时也在（通用部分不丢）
    assert profile.reader_contract["first_screen_hook"]


def test_opening_profile_bundle_covers_collected_secondary_categories():
    bundle = get_opening_profile_bundle_cached()
    profiles = bundle["profiles"]
    secondary_count = sum(len(items) for items in profiles.values())

    assert len(bundle["primary_defaults"]) >= 15
    assert secondary_count >= 82
    # A-08：缺失策略从「阻塞生成」改为「降级到通用契约」
    assert bundle["resolution_policy"]["missing_profile"] == "degrade_to_generic"
