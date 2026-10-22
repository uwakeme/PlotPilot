"""StoryPipeline beat contract helper tests.

⚠ E-08：`merge_beats_by_target`（按章字数档位决定「一章拆几次 LLM 调用」）
**零生产调用方** —— 探针扫全仓（`application` / `engine` / `interfaces` /
`infrastructure` / `domain`，含模块内部调用）确认：它只被**自己的测试**调用。
新管线 `ctx.beats` 恒空、整章一次写完 ⇒ 登记表的「一章拆几次由代码档位定」**已不成立**。

⇒ 该函数已删除；下面保留的是**活着的**那条（`merge_two_beats`，生产调用方是
`application/engine/services/context_builder.py`）与序列化器的守卫。
"""
from types import SimpleNamespace

from engine.pipeline.beat_contracts import (
    merge_two_beats,
    serialize_beats_for_shared_state,
)


def _beat(description: str, card_block: str, target_words: int = 600):
    return SimpleNamespace(
        description=description,
        target_words=target_words,
        focus="action",
        scene_goal=description,
        transition_from_prev="",
        location_id="",
        card_prompt_block=card_block,
        emotion_beat_card=SimpleNamespace(
            active_action=f"行动-{description}",
            emotion_gap=f"缺口-{description}",
            forbidden_drift=f"禁止-{description}",
        ),
    )


def test_merge_two_beats_preserves_all_card_blocks():
    """合并两拍时**不许丢**任何一张卡片的提示块（生产路径：context_builder 会用）。"""
    a = _beat("一", "active_action=A\nemotion_gap=EA\nforbidden_drift=FA")
    b = _beat("二", "active_action=B\nemotion_gap=EB\nforbidden_drift=FB")

    merged = merge_two_beats(a, b)

    block = merged.card_prompt_block
    for expected in ("active_action=A", "active_action=B"):
        assert expected in block
    for expected in ("emotion_gap=EA", "emotion_gap=EB"):
        assert expected in block
    for expected in ("forbidden_drift=FA", "forbidden_drift=FB"):
        assert expected in block


def test_merge_two_beats_keeps_pipeline_compatible_shape():
    merged = merge_two_beats(
        _beat("一", "card A", 300),
        _beat("二", "card B", 300),
    )

    assert merged.description
    assert merged.target_words == 600
    assert merged.focus == "action"
    assert "card A" in merged.card_prompt_block
    assert "card B" in merged.card_prompt_block


def test_merged_beat_keeps_all_structured_cards_for_runtime_snapshots():
    """⚠ 原用例走的是已删除的 `merge_beats_by_target`（死代码）。

    改写成走**活着的** `merge_two_beats`（生产调用方：context_builder）⇒
    守卫的意图（合并后的拍在快照里仍带全部结构化卡片）**保留**，
    且不再依赖一个零调用方的函数。
    """
    merged = merge_two_beats(
        _beat("一", "card A", 300),
        _beat("二", "card B", 300),
    )

    snapshot = serialize_beats_for_shared_state([merged])

    assert len(snapshot) == 1
    assert snapshot[0]["active_action"] == "行动-一"
    assert snapshot[0]["emotion_gap"] == "缺口-一"
    assert snapshot[0]["forbidden_drift"] == "禁止-一"
    assert [c["active_action"] for c in snapshot[0]["beat_cards"]] == ["行动-一", "行动-二"]
