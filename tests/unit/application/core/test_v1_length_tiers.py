"""V1 体量档推导与历史前缀清理"""
from application.core.v1_length_tiers import (
    resolve_v1_length_params,
    strip_generated_premise_prefixes,
    V1_LENGTH_TIERS,
)
from tests.unit.legacy_premise_fixtures import LEGACY_STRUCTURE_BLACK_BOX_PREFIX


def test_resolve_standard_tier_default_words():
    chapters, wpc, tier = resolve_v1_length_params("standard", 100, None)
    assert tier == "standard"
    assert wpc == 2000
    assert chapters == 500  # 1_000_000 / 2000


def test_resolve_short_tier_custom_words():
    chapters, wpc, tier = resolve_v1_length_params("short", 100, 2400)
    assert tier == "short"
    assert wpc == 2400
    assert chapters == 125  # ceil(300_000 / 2400)


def test_resolve_without_tier_uses_chapters():
    chapters, wpc, tier = resolve_v1_length_params(None, 80, 3000)
    assert tier is None
    assert chapters == 80
    assert wpc == 3000


def test_legacy_black_box_prefix_is_stripped():
    """历史前缀必须被读侧清理掉（存量库兼容）。"""
    text = strip_generated_premise_prefixes(
        f"{LEGACY_STRUCTURE_BLACK_BOX_PREFIX}\n\n作者真正的梗概"
    )
    assert text == "作者真正的梗概"


def test_tier_meta_present():
    assert "approx_total_words" in V1_LENGTH_TIERS["epic"]
