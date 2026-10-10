"""state_updater 是第二条活的伏笔种植路径，必须同样解析绑定判据字段。

背景：chapter_narrative_sync 与 state_updater 都能种植 Foreshadowing。若只有前者
解析 emotional_weight / binding_level，则这条路径种下的伏笔永远停在 0.5/1 的惰性
默认值——而绑定层级校验的条件是 emotional_weight > 0.8，它永远判不出来。
"""
from unittest.mock import Mock

from application.analyst.services.state_updater import StateUpdater
from domain.bible.entities.bible import Bible
from domain.novel.entities.foreshadowing_registry import ForeshadowingRegistry
from domain.novel.value_objects.chapter_state import ChapterState
from domain.novel.value_objects.novel_id import NovelId


def _updater():
    novel_id = NovelId("novel-1")
    bible_repository = Mock()
    foreshadowing_repository = Mock()
    bible_repository.get_by_novel_id.return_value = Bible(id="bible-1", novel_id=novel_id)
    foreshadowing_repository.get_by_novel_id.return_value = ForeshadowingRegistry(
        id="fr-1", novel_id=novel_id
    )
    return StateUpdater(
        bible_repository=bible_repository,
        foreshadowing_repository=foreshadowing_repository,
    ), foreshadowing_repository


def _plant(updater, foreshadowing_repository, planted):
    chapter_state = ChapterState(
        new_characters=[],
        character_actions=[],
        relationship_changes=[],
        foreshadowing_planted=planted,
        foreshadowing_resolved=[],
        events=[],
    )
    updater.update_from_chapter("novel-1", 5, chapter_state)
    return foreshadowing_repository.save.call_args[0][0].get_unresolved()[0]


def test_planted_foreshadow_carries_binding_fields():
    updater, repo = _updater()

    got = _plant(updater, repo, [{
        "description": "断魂崖下的残剑",
        "chapter": 5,
        "importance": "critical",
        "emotional_weight": 0.95,
        "binding_level": 4,
    }])

    assert got.emotional_weight == 0.95
    assert got.binding_level == 4


def test_planted_foreshadow_without_fields_falls_back_to_defaults():
    """旧 LLM 输出没有这两字段时必须回落默认值，不能抛错。"""
    updater, repo = _updater()

    got = _plant(updater, repo, [{"description": "旧伏笔", "chapter": 5}])

    assert got.emotional_weight == 0.5
    assert got.binding_level == 1


def test_planted_foreshadow_rejects_out_of_range_values():
    """LLM 越界值不得进库——否则 Foreshadowing.__post_init__ 会抛，整章状态更新炸掉。"""
    updater, repo = _updater()

    got = _plant(updater, repo, [{
        "description": "越界伏笔",
        "chapter": 5,
        "emotional_weight": 1.5,
        "binding_level": 9,
    }])

    assert got.emotional_weight == 0.5
    assert got.binding_level == 1
