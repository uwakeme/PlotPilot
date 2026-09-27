"""全书终审服务(BookReviewService)单元测试"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.audit.services.book_review_service import BookReviewError, BookReviewService


def _chapter(number, content="正文内容。", title=""):
    return SimpleNamespace(number=number, title=title or f"第{number}章", content=content)


def _act_node(number, start, end, title=""):
    return SimpleNamespace(
        node_type=SimpleNamespace(value="act"),
        number=number,
        title=title or f"第{number}幕",
        chapter_start=start,
        chapter_end=end,
    )


def _other_node(kind):
    return SimpleNamespace(node_type=SimpleNamespace(value=kind), number=1, title="x", chapter_start=1, chapter_end=2)


class FakeDB:
    """记录 execute、按表名回配查询结果的假 DatabaseConnection"""

    def __init__(self):
        self.executed = []
        self._all = {}
        self._one = {}

    def fetch_all(self, sql, params=None):
        for key, rows in self._all.items():
            if key in sql:
                return rows
        return []

    def fetch_one(self, sql, params=None):
        for key, row in self._one.items():
            if key in sql:
                return row
        return None

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def get_connection(self):
        return SimpleNamespace(commit=MagicMock())

    def transaction(self):
        conn = SimpleNamespace(execute=MagicMock())
        return SimpleNamespace(__enter__=lambda s: conn, __exit__=lambda s, *a: False)


def _make_service(monkeypatch, *, chapters, nodes, planted=None, llm_json="{}", db=None):
    db = db or FakeDB()
    llm = MagicMock()
    llm.generate = AsyncMock(return_value=SimpleNamespace(content=llm_json))
    service = BookReviewService(
        llm,
        db=db,
        chapter_repository=SimpleNamespace(list_by_novel=lambda nid: chapters),
        story_node_repository=SimpleNamespace(get_by_novel_sync=lambda nid: nodes),
        foreshadow_repository=SimpleNamespace(get_planted_sql=lambda nid: planted or []),
    )
    captured = {}

    def fake_render(node_key, variables):
        captured["node_key"] = node_key
        captured["variables"] = variables
        return SimpleNamespace(system="SYS", user="USER")

    monkeypatch.setattr(
        "application.audit.services.book_review_service.render_required_prompt",
        fake_render,
    )
    return service, llm, captured, db


@pytest.mark.asyncio
async def test_list_acts_uses_story_tree_ranges_and_merges_extras(monkeypatch):
    chapters = [_chapter(i) for i in range(1, 8)]  # 1..7
    nodes = [_other_node("volume"), _act_node(1, 1, 3), _act_node(2, 4, 6)]
    service, _, _, _ = _make_service(monkeypatch, chapters=chapters, nodes=nodes)

    acts = service.list_acts("n-1")

    assert [a["act_number"] for a in acts] == [1, 2]
    assert acts[0]["chapter_start"] == 1 and acts[0]["chapter_end"] == 3
    # 第 7 章在结构外 → 并入最后一幕
    assert acts[-1]["chapter_end"] == 7


@pytest.mark.asyncio
async def test_list_acts_fallback_chunks_by_five(monkeypatch):
    chapters = [_chapter(i) for i in range(1, 13)]  # 1..12
    service, _, _, _ = _make_service(monkeypatch, chapters=chapters, nodes=[])

    acts = service.list_acts("n-1")

    assert len(acts) == 3
    assert acts[0]["chapter_start"] == 1 and acts[-1]["chapter_end"] == 12


@pytest.mark.asyncio
async def test_run_act_review_assembles_prompt_and_persists(monkeypatch):
    chapters = [
        _chapter(1, "甲的正文。"),
        _chapter(2, "乙的正文。"),
        _chapter(3, "丙的正文。", title="第三章"),
    ]
    planted = [
        {"planted_chapter": 2, "due_chapter": 3, "description": "应在本幕回收的伏笔"},
    ]
    llm_json = json.dumps(
        {
            "summary": "本幕质量尚可",
            "quality_score": 82,
            "findings": [
                {
                    "severity": "major",
                    "category": "logic",
                    "chapter_number": 2,
                    "quote": "乙的正文。",
                    "problem": "因果断裂",
                    "suggestion": "补一句动机",
                    "suggested_instruction": "在第2章补充甲行动的动机交代",
                }
            ],
        },
        ensure_ascii=False,
    )
    service, llm, captured, db = _make_service(
        monkeypatch,
        chapters=chapters,
        nodes=[_act_node(1, 1, 3)],
        planted=planted,
        llm_json=llm_json,
    )

    payload = await service.run_act_review("n-1", 1)

    assert captured["node_key"] == "book-act-review"
    variables = captured["variables"]
    assert "甲的正文。" in variables["act_text"] and "第三章" in variables["act_text"]
    assert "应在本幕回收的伏笔" in variables["foreshadow_context"]
    assert payload["quality_score"] == 82
    assert payload["findings"][0]["chapter_number"] == 2
    assert payload["suggested" ] if False else payload["findings"][0]["suggested_instruction"].startswith("在第2章")
    # 落库:确定性 id + JSON payload
    assert db.executed and "book_review_reports" in db.executed[0][0]
    assert db.executed[0][1][0] == "n-1:act:1"
    llm.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_act_review_missing_act_raises(monkeypatch):
    service, _, _, _ = _make_service(
        monkeypatch,
        chapters=[_chapter(1)],
        nodes=[_act_node(1, 1, 1)],
    )
    with pytest.raises(BookReviewError):
        await service.run_act_review("n-1", 9)


@pytest.mark.asyncio
async def test_synthesize_aggregates_acts_and_persists_final(monkeypatch):
    act_payload = json.dumps(
        {"act_number": 1, "summary": "s", "quality_score": 70, "findings": [{"problem": "p"}]},
        ensure_ascii=False,
    )
    db = FakeDB()
    db._all["book_review_reports"] = [{"act_number": 1, "payload": act_payload}]
    db._one["FROM novels"] = {"title": "测试书", "premise": "梗概", "genre": "悬疑"}
    db._one["FROM chapters"] = {"c": 3}
    db._one["foreshadows"] = {"status": "planted", "c": 2}
    llm_json = json.dumps(
        {
            "headline": "整体可读，局部需修",
            "overall_score": 78,
            "scores": {"plot_logic": 70},
            "top_issues": [
                {
                    "severity": "critical",
                    "category": "logic",
                    "chapter_numbers": [2, 5],
                    "title": "因果断裂",
                    "detail": "两处断裂同根因",
                    "quote": "q",
                    "suggestion": "s",
                    "suggested_instruction": "fix ch2",
                }
            ],
            "promise_verdicts": [],
            "act_summaries": [{"act_number": 1, "summary": "s"}],
        },
        ensure_ascii=False,
    )
    service, llm, captured, db = _make_service(
        monkeypatch,
        chapters=[_chapter(i) for i in range(1, 4)],
        nodes=[_act_node(1, 1, 3)],
        llm_json=llm_json,
        db=db,
    )

    payload = await service.synthesize("n-1")

    assert captured["node_key"] == "book-review-synthesis"
    assert "测试书" in captured["variables"]["book_meta"]
    assert "应在本幕回收" not in captured["variables"]["act_findings"]  # act_findings 是纯 JSON
    assert payload["top_issues"][0]["priority"] == 1
    assert payload["top_issues"][0]["chapter_numbers"] == [2, 5]
    final_writes = [e for e in db.executed if "book_review_reports" in e[0]]
    assert final_writes and final_writes[0][1][0] == "n-1:final:final"


def test_get_report_returns_progress_and_final(monkeypatch):
    db = FakeDB()
    db._all["book_review_reports"] = [
        {
            "act_number": 1,
            "payload": json.dumps({"quality_score": 70, "summary": "s", "findings": [{}]}),
            "finding_count": 1,
            "updated_at": "2026-09-24",
        }
    ]
    db._one["scope = 'final'"] = {
        "payload": json.dumps({"headline": "h", "top_issues": []}),
        "updated_at": "2026-09-24",
    }
    service, _, _, _ = _make_service(
        monkeypatch,
        chapters=[_chapter(i) for i in range(1, 4)],
        nodes=[_act_node(1, 1, 3), _act_node(2, 4, 6)],
        db=db,
    )

    report = service.get_report("n-1")

    assert report["total_act_count"] == 2
    assert report["reviewed_act_count"] == 1
    first, second = report["acts"]
    assert first["reviewed"] is True and first["review"]["quality_score"] == 70
    assert second["reviewed"] is False
    assert report["final_report"]["headline"] == "h"
