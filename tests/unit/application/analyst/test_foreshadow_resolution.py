"""伏笔回收 ID 歧义 / 无效不得静默丢弃（登记表 G-02）。

背景：`application/analyst/services/state_updater.py` 有两条静默丢失路径 ——
路径一，描述模糊命中多个伏笔时返回 `""`，调用方 `continue` 掉；
路径二，`foreshadowing_id` 不存在且描述也匹配不上时**返回未经校验的 fid**，
调用方拿它去 `mark_resolved` 抛错，异常又被降级成 warning。

两处的共同后果一致：AI 说「这条伏笔已回收」，而伏笔永远停在 PLANTED，
且没有任何痕迹说明它本该被回收。

本文件全部使用**真实 VO**（ForeshadowingRegistry / Foreshadowing / ChapterState），
这样「只取未回收伏笔」这类过滤规则也一并被覆盖。
"""
from unittest.mock import Mock

from application.analyst.services.state_updater import StateUpdater
from domain.bible.entities.bible import Bible
from domain.novel.entities.foreshadowing_registry import ForeshadowingRegistry
from domain.novel.value_objects.chapter_state import ChapterState
from domain.novel.value_objects.foreshadowing import (
    Foreshadowing,
    ForeshadowingStatus,
    ImportanceLevel,
)
from domain.novel.value_objects.novel_id import NovelId


def _foreshadowing(fid: str, description: str, chapter: int = 3) -> Foreshadowing:
    return Foreshadowing(
        id=fid,
        planted_in_chapter=chapter,
        description=description,
        importance=ImportanceLevel.MEDIUM,
        status=ForeshadowingStatus.PLANTED,
    )


def _make_updater(*foreshadowings: Foreshadowing):
    """返回 (updater, registry, foreshadowing_repository)。

    foreshadowing_repository 是 Mock：本次改造不碰仓储契约，
    但要用它断言「save 出去的 registry 里哪些伏笔被标记了」。
    """
    novel_id = NovelId("novel-1")
    bible_repository = Mock()
    bible_repository.get_by_novel_id.return_value = Bible(id="bible-1", novel_id=novel_id)

    registry = ForeshadowingRegistry(id="fr-1", novel_id=novel_id)
    for item in foreshadowings:
        registry.register(item)

    foreshadowing_repository = Mock()
    foreshadowing_repository.get_by_novel_id.return_value = registry

    updater = StateUpdater(
        bible_repository=bible_repository,
        foreshadowing_repository=foreshadowing_repository,
    )
    return updater, registry, foreshadowing_repository


def _chapter_state(resolved: list) -> ChapterState:
    return ChapterState(
        new_characters=[],
        character_actions=[],
        relationship_changes=[],
        foreshadowing_planted=[],
        foreshadowing_resolved=resolved,
        events=[],
    )


# ─── 路径一：歧义候选 ───


def test_ambiguous_description_resolves_to_none_not_a_candidate():
    """两个伏笔都模糊命中 → 必须是「明确未配对」。

    不能是某个候选 id（那是用代码替 AI 做创作判断），
    也不能是 `""`（空串是 falsy，调用方会把它当成「没有引用」继续静默跳过）。
    """
    updater, _registry, _repo = _make_updater(
        _foreshadowing("f-1", "衣物被更换"),
        _foreshadowing("f-2", "衣物被更换后袖口有血", chapter=4),
    )

    result = updater._resolve_foreshadowing_id(
        _registry, {"foreshadowing_id": "衣物被更换后", "chapter": 5}
    )

    assert result is None, f"歧义必须返回 None，实际返回了 {result!r}"
    assert result != "", "返回空串同样会让调用方静默跳过，等于什么都没改"
    assert result not in ("f-1", "f-2"), "歧义时不得替 AI 选一个候选"


# ─── 路径二：未校验的 fid ───


def test_unknown_fid_with_unmatched_description_is_never_returned_verbatim():
    """fid 不存在 + 描述匹配不上任何伏笔 → 绝不能把 fid 原样返回。

    这是路径二的回归守卫：原实现在这里 `return fid`，
    调用方随后 mark_resolved 抛错、异常被吞，回收指令就此消失。
    """
    updater, registry, _repo = _make_updater(_foreshadowing("f-1", "玉佩碎裂"))

    result = updater._resolve_foreshadowing_id(
        registry, {"foreshadowing_id": "f-nope", "description": "雪山一剑", "chapter": 5}
    )

    assert result != "f-nope", "未经 registry 校验的 fid 被原样返回，会在落库处抛错后被吞"
    assert result is None


def test_unknown_fid_but_description_uniquely_matches_still_resolves():
    """正向对照：fid 是假的，但描述唯一命中 → 仍应配到真实伏笔。"""
    updater, registry, _repo = _make_updater(_foreshadowing("f-1", "玉佩碎裂"))

    result = updater._resolve_foreshadowing_id(
        registry, {"foreshadowing_id": "f-nope", "description": "玉佩碎裂", "chapter": 5}
    )

    assert result == "f-1"


def test_valid_fid_still_resolves():
    """回归守卫：正常 fid 照常配对，别把好路径改坏。"""
    updater, registry, _repo = _make_updater(_foreshadowing("f-1", "玉佩碎裂"))

    result = updater._resolve_foreshadowing_id(
        registry, {"foreshadowing_id": "f-1", "chapter": 5}
    )

    assert result == "f-1"


# ─── 调用方：未配对必须留下可观测证据 ───


def test_unpaired_resolution_is_recorded_not_silently_skipped(caplog):
    """核心用例：光让 _resolve_* 返回 None、调用方照样 continue，等于什么都没改。

    这里断言「有回收指令但配不上伏笔」这件事留下了三样东西：
      1. 返回的 report 里显式记着这条未配对指令（含原始 resolved_data）
      2. ERROR 级日志带 novel_id / chapter_number / 原始 resolved_data
      3. 伏笔本身没有被偷偷标记成已回收
    """
    updater, registry, repo = _make_updater(
        _foreshadowing("f-1", "衣物被更换"),
        _foreshadowing("f-2", "衣物被更换后袖口有血", chapter=4),
    )
    raw = {"foreshadowing_id": "衣物被更换后", "chapter": 5}

    with caplog.at_level("ERROR"):
        report = updater.update_from_chapter("novel-1", 5, _chapter_state([raw]))

    assert report is not None, "update_from_chapter 必须返回显式结果，否则未配对项无处可查"

    # 1) 显式记录，而不是 continue 掉
    assert len(report.unresolved) == 1, "未配对的回收指令没有被记入 report，等于静默丢弃"
    entry = report.unresolved[0]
    assert entry.resolved_data == raw, "必须保留原始 resolved_data 才能复盘"
    assert entry.chapter_number == 5
    assert report.resolved_ids == [], "歧义时不得声称回收成功"

    # 2) ERROR 级日志（warning 会被淹没），带定位所需的三要素
    error_text = "\n".join(
        rec.getMessage() for rec in caplog.records if rec.levelname == "ERROR"
    )
    assert error_text, "未配对的回收指令必须留下 ERROR 日志"
    assert "novel-1" in error_text and "5" in error_text
    assert "衣物被更换后" in error_text, "日志里必须能看到原始 resolved_data"

    # 3) 伏笔确实没被标记回收
    saved = repo.save.call_args[0][0]
    assert saved.get_by_id("f-1").status == ForeshadowingStatus.PLANTED
    assert saved.get_by_id("f-2").status == ForeshadowingStatus.PLANTED


def test_unknown_fid_never_reaches_mark_resolved_end_to_end():
    """端到端守卫：不存在的 fid 不得被拿去 mark_resolved。

    原实现把未校验的 fid 传下去 → mark_resolved 抛 InvalidOperationError →
    异常被降级成 warning → 回收指令凭空消失。这里要求它在配对层就被拦下，
    因此 reason 必须是 unpaired_resolution，而不是落库阶段才失败。
    """
    updater, registry, repo = _make_updater(_foreshadowing("f-1", "玉佩碎裂"))

    report = updater.update_from_chapter(
        "novel-1",
        5,
        _chapter_state([{"foreshadowing_id": "f-nope", "description": "雪山一剑", "chapter": 5}]),
    )

    assert report is not None, "update_from_chapter 必须返回显式结果"
    assert report.resolved_ids == []
    assert len(report.unresolved) == 1
    assert report.unresolved[0].reason == "unpaired_resolution", (
        "不存在的 fid 应在配对层被拦下，而不是拖到 mark_resolved 才炸"
    )
    saved = repo.save.call_args[0][0]
    assert saved.get_by_id("f-1").status == ForeshadowingStatus.PLANTED


def test_mark_resolved_failure_is_recorded_not_only_logged(monkeypatch):
    """mark_resolved 抛错那条路径同样不能只 warning 了事。"""
    updater, registry, repo = _make_updater(_foreshadowing("f-1", "玉佩碎裂"))

    def _boom(*args, **kwargs):
        raise ValueError("落库失败")

    monkeypatch.setattr(registry, "mark_resolved", _boom)

    report = updater.update_from_chapter(
        "novel-1", 5, _chapter_state([{"foreshadowing_id": "f-1", "chapter": 5}])
    )

    assert report is not None, "update_from_chapter 必须返回显式结果"
    assert report.resolved_ids == []
    assert len(report.unresolved) == 1, "mark_resolved 失败被静默吞掉"
    assert report.unresolved[0].reason == "mark_resolved_failed"
    assert "落库失败" in report.unresolved[0].detail
    assert repo.save.called, "其余伏笔仍应正常落库，不能因单条坏数据整章不写"


def test_clean_resolution_leaves_report_empty():
    """正向对照：全部配对成功时 report 里不该有 unresolved 噪音。"""
    updater, registry, repo = _make_updater(_foreshadowing("f-1", "玉佩碎裂"))

    report = updater.update_from_chapter(
        "novel-1", 5, _chapter_state([{"foreshadowing_id": "f-1", "chapter": 5}])
    )

    assert report is not None, "update_from_chapter 必须返回显式结果"
    assert report.unresolved == []
    assert report.resolved_ids == ["f-1"]
    saved = repo.save.call_args[0][0]
    assert saved.get_by_id("f-1").status == ForeshadowingStatus.RESOLVED
