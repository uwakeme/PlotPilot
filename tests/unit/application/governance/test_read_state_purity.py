"""E8: governance 读路径纯读（不创建契约/不回填/不 emit）。"""
from unittest.mock import MagicMock

from application.governance.models import NarrativeContract
from application.governance.service import NarrativeGovernanceService


def _service(contract: NarrativeContract | None = None):
    repo = MagicMock()
    repo.get_contract.return_value = contract
    repo.list_storylines.return_value = []
    repo.list_open_debts.return_value = []
    repo.latest_report.return_value = None
    repo.append_event = MagicMock()
    repo.save_contract = MagicMock()
    service = NarrativeGovernanceService(repo, novel_repository=None, legacy_storyline_repository=None)
    # preview_chapter_budget 依赖仓储交互，这里给最小桩
    service.preview_chapter_budget = lambda nid: MagicMock(to_dict=lambda: {})
    return service, repo


def test_read_state_does_not_create_contract_or_emit():
    service, repo = _service(contract=None)

    state = service.read_state("novel-1")

    repo.save_contract.assert_not_called()
    repo.append_event.assert_not_called()
    # 缺契约时返回同结构默认视图（前端无感知），不是 null
    assert isinstance(state["contract"], dict)
    assert state["contract"]["novel_id"] == "novel-1"
    assert state["contract"]["title_promise"]


def test_get_or_create_still_initializes_for_write_paths():
    service, repo = _service(contract=None)

    contract = service.get_or_create_contract("novel-1")

    repo.save_contract.assert_called_once()
    repo.append_event.assert_called_once()  # NarrativeContractCreated 事件仍会发出
    assert contract.novel_id == "novel-1"


def test_read_state_uses_persisted_contract_when_present():
    existing = NarrativeContract(
        novel_id="novel-1",
        title_promise="已存契约",
        core_question="q",
        theme_anchors=[],
        forbidden_early_payoffs=[],
        reveal_budget={},
    )
    service, repo = _service(contract=existing)

    state = service.read_state("novel-1")

    assert state["contract"]["title_promise"] == "已存契约"
    repo.save_contract.assert_not_called()


# ── E13: 一致性检查的时间倒流检测 ─────────────────────────────────────────

def _checker_ctx_with_timeline(last_chapter: int):
    from domain.bible.entities.bible import Bible
    from domain.bible.entities.character_registry import CharacterRegistry
    from domain.novel.entities.foreshadowing_registry import ForeshadowingRegistry
    from domain.novel.entities.plot_arc import PlotArc
    from domain.novel.value_objects.consistency_context import ConsistencyContext
    from domain.novel.value_objects.event_timeline import EventTimeline
    from domain.novel.value_objects.novel_event import EventType, NovelEvent
    from domain.novel.value_objects.novel_id import NovelId
    from domain.bible.value_objects.relationship_graph import RelationshipGraph

    timeline = EventTimeline()
    timeline.add_event(NovelEvent(
        chapter_number=last_chapter,
        event_type=EventType.DECISION,
        description="历史事件",
        involved_characters=(),
    ))
    return ConsistencyContext(
        bible=Bible(id="b", novel_id=NovelId("n")),
        character_registry=CharacterRegistry(id="cr", novel_id="n"),
        foreshadowing_registry=ForeshadowingRegistry(id="fr", novel_id=NovelId("n")),
        plot_arc=PlotArc(id="pa", novel_id=NovelId("n")),
        event_timeline=timeline,
        relationship_graph=RelationshipGraph(),
    )


def test_time_backwards_event_produces_warning():
    from domain.novel.services.consistency_checker import ConsistencyChecker
    from domain.novel.value_objects.chapter_state import ChapterState

    state = ChapterState(
        new_characters=[],
        character_actions=[],
        relationship_changes=[],
        foreshadowing_planted=[],
        foreshadowing_resolved=[],
        events=[{"chapter": 3, "involved_characters": []}],  # 历史已到第 10 章
    )
    report = ConsistencyChecker().check_all(state, _checker_ctx_with_timeline(10))
    assert any("时间倒流" in w for w in report.warnings)


def test_normal_forward_event_no_warning():
    from domain.novel.services.consistency_checker import ConsistencyChecker
    from domain.novel.value_objects.chapter_state import ChapterState

    state = ChapterState(
        new_characters=[],
        character_actions=[],
        relationship_changes=[],
        foreshadowing_planted=[],
        foreshadowing_resolved=[],
        events=[{"chapter": 11, "involved_characters": []}],
    )
    report = ConsistencyChecker().check_all(state, _checker_ctx_with_timeline(10))
    assert not any("时间倒流" in w for w in report.warnings)
