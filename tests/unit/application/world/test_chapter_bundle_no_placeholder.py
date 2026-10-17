"""G-06：章末 bundle 失败不得以占位文本冒充内容落库。

被测缺陷的形状：`"（未提取）"` / `"无"` 这类占位词会被下游 vector-first 检索
真的向量化，读起来像一条真实的关键事件。空串是诚实的「什么都不知道」，
占位词不是。

两层用例：
1. 纯函数层 `normalize_chapter_bundle` —— 归一化出口，无需 mock 整条管线；
2. 落库边界层 —— 用替身记录传给 `upsert_chapter_summary` 的实参，
   既断言没有占位词，也断言 `beat_sections` / `micro_beats` 仍然被传入
   （这两项来自非 LLM 来源，与 bundle 成败无关；跳过落库会丢数据）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from application.world.services import chapter_narrative_sync as cns
from application.world.services.chapter_narrative_sync import (
    SYNC_STATUS_EXTRACTION_FAILED,
    NormalizedBundle,
    normalize_chapter_bundle,
    sync_chapter_narrative_after_save,
)


# --------------------------------------------------------------------------
# 第一层：纯函数
# --------------------------------------------------------------------------

def test_normalize_reports_missing_fields_as_none_never_placeholder_text():
    """bundle 失败且字段缺失 → 三个字段是 None，不是「（未提取）」这类字符串。"""
    normalized = normalize_chapter_bundle({}, extraction_failed=True)

    assert isinstance(normalized, NormalizedBundle)
    assert normalized.summary is None
    assert normalized.key_events is None
    assert normalized.open_threads is None
    assert normalized.extraction_failed is True


def test_normalize_treats_empty_and_blank_bundle_fields_as_not_extracted():
    """LLM 跑了但只给出空串 / 纯空白 / None —— 同样是「没提取到」，不是内容。"""
    normalized = normalize_chapter_bundle(
        {"summary": "", "key_events": "   ", "open_threads": None}
    )

    assert normalized.summary is None
    assert normalized.key_events is None
    assert normalized.open_threads is None


def test_normalize_never_emits_placeholder_words():
    """红线守卫：归一化出口在任何输入下都不许吐出占位词。"""
    for raw in (
        {},
        {"summary": "", "key_events": "  ", "open_threads": None},
        {"summary": "正常摘要", "key_events": "正常事件", "open_threads": "正常未解"},
    ):
        normalized = normalize_chapter_bundle(raw, extraction_failed=True)
        for value in (normalized.summary, normalized.key_events, normalized.open_threads):
            assert value is None or "未提取" not in value


def test_normalize_passes_through_real_content_verbatim():
    """bundle 正常 → 原样透传，不截断不改写。"""
    normalized = normalize_chapter_bundle(
        {"summary": "林澈在雨夜听见铜铃。", "key_events": "铜铃第一次响起", "open_threads": "铜铃来自何处"}
    )

    assert normalized.summary == "林澈在雨夜听见铜铃。"
    assert normalized.key_events == "铜铃第一次响起"
    assert normalized.open_threads == "铜铃来自何处"
    assert normalized.extraction_failed is False


def test_ok_distinguishes_the_three_states():
    """三态：成功 / 跑了但空 / 崩了。只有成功为 True。"""
    ok = normalize_chapter_bundle({"summary": "有摘要", "key_events": "有事件"})
    empty = normalize_chapter_bundle({"summary": "", "key_events": ""})
    failed = normalize_chapter_bundle({}, extraction_failed=True)

    assert ok.ok is True
    assert empty.ok is False
    assert failed.ok is False
    # 「跑了但空」与「崩了」不是同一态，标志位必须能分开
    assert empty.extraction_failed is False
    assert failed.extraction_failed is True


# --------------------------------------------------------------------------
# 第二层：落库边界
# --------------------------------------------------------------------------

class _ExistingChapter:
    """已存在的章节摘要替身 —— 提供与 bundle 成败无关的兜底数据。"""

    def __init__(
        self,
        key_events: str = "既有关键事件",
        open_threads: str = "既有未解问题",
        beat_sections: Optional[List[str]] = None,
        micro_beats: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.chapter_id = 3
        self.summary = "既有摘要"
        self.key_events = key_events
        self.open_threads = open_threads
        self.consistency_note = "既有说明"
        self.beat_sections = beat_sections if beat_sections is not None else ["夜袭前奏", "雨巷对峙"]
        self.micro_beats = micro_beats


class _Knowledge:
    def __init__(self, existing: Optional[_ExistingChapter]) -> None:
        self.chapters = [existing] if existing is not None else []
        self.version = 1


class _RecordingKnowledgeService:
    """记录 `upsert_chapter_summary` 实参的替身，不断真库。"""

    def __init__(self, existing: Optional[_ExistingChapter] = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._knowledge = _Knowledge(existing)

    def get_knowledge(self, novel_id):
        return self._knowledge

    def upsert_chapter_summary(self, **kwargs):
        self.calls.append(kwargs)
        return self._knowledge


class _FailingLLM:
    """张力评分等旁路 LLM 调用的替身：直接失败，由调用方的 except 兜住。"""

    async def generate(self, *args, **kwargs):
        raise RuntimeError("测试不应触达真实 LLM")

    def __getattr__(self, name):
        async def _boom(*args, **kwargs):
            raise RuntimeError("测试不应触达真实 LLM")

        return _boom


def _break_bundle_extraction(monkeypatch):
    """让 `llm_chapter_extract_bundle` 抛异常，模拟 LLM 崩溃。"""

    async def _boom(*args, **kwargs):
        raise RuntimeError("模拟章末 bundle LLM 崩溃")

    monkeypatch.setattr(cns, "llm_chapter_extract_bundle", _boom)


def _return_empty_bundle(monkeypatch):
    """让 `llm_chapter_extract_bundle` 正常返回但内容全空。"""

    async def _empty(*args, **kwargs):
        return {"summary": "", "key_events": "", "open_threads": "",
                "relation_triples": [], "foreshadow_hints": []}

    monkeypatch.setattr(cns, "llm_chapter_extract_bundle", _empty)


@pytest.mark.asyncio
async def test_persisted_key_events_and_open_threads_are_empty_not_placeholder(monkeypatch):
    """红线本体：LLM 崩溃时不得用「（未提取）」/「无」冒充内容落库。"""
    _break_bundle_extraction(monkeypatch)
    knowledge = _RecordingKnowledgeService(existing=None)

    await sync_chapter_narrative_after_save(
        novel_id="novel-1",
        chapter_number=3,
        content="雨夜，林澈握紧铜铃。",
        knowledge_service=knowledge,
        indexing_svc=None,
        llm_service=_FailingLLM(),
    )

    assert len(knowledge.calls) == 1, "bundle 失败也必须落库：节拍与微观节拍来自非 LLM 来源"
    call = knowledge.calls[0]
    assert call["key_events"] == ""
    assert call["open_threads"] == ""
    assert "未提取" not in call["key_events"]
    assert "未提取" not in call["open_threads"]


@pytest.mark.asyncio
async def test_bundle_failure_still_persists_non_llm_sourced_fields(monkeypatch):
    """约束一的回归守卫：跳掉落库会连带丢掉这些非 LLM 来源的数据。"""
    _break_bundle_extraction(monkeypatch)
    knowledge = _RecordingKnowledgeService(existing=_ExistingChapter())

    await sync_chapter_narrative_after_save(
        novel_id="novel-1",
        chapter_number=3,
        content="雨夜，林澈握紧铜铃。",
        knowledge_service=knowledge,
        indexing_svc=None,
        llm_service=_FailingLLM(),
        chapter_micro_beats=[{"beat": "写作侧微观节拍", "summary": "起"}],
    )

    assert len(knowledge.calls) == 1, "跳过 upsert 会丢掉 beat_sections / micro_beats"
    call = knowledge.calls[0]
    assert call["beat_sections"] == ["夜袭前奏", "雨巷对峙"]
    assert call["micro_beats"] == [{"beat": "写作侧微观节拍", "summary": "起"}]
    # existing 兜底同样来自非 LLM 来源
    assert call["key_events"] == "既有关键事件"
    assert call["open_threads"] == "既有未解问题"
    assert call["consistency_note"] == "既有说明"


@pytest.mark.asyncio
async def test_sync_status_separates_llm_crash_from_empty_return(monkeypatch):
    """sync_status 必须能区分「LLM 崩了」与「LLM 跑完返回空」。"""
    crashed = _RecordingKnowledgeService(existing=None)
    _break_bundle_extraction(monkeypatch)
    await sync_chapter_narrative_after_save(
        novel_id="novel-1",
        chapter_number=3,
        content="雨夜，林澈握紧铜铃。",
        knowledge_service=crashed,
        indexing_svc=None,
        llm_service=_FailingLLM(),
    )

    returned_empty = _RecordingKnowledgeService(existing=None)
    _return_empty_bundle(monkeypatch)
    await sync_chapter_narrative_after_save(
        novel_id="novel-1",
        chapter_number=3,
        content="雨夜，林澈握紧铜铃。",
        knowledge_service=returned_empty,
        indexing_svc=None,
        llm_service=_FailingLLM(),
    )

    assert len(crashed.calls) == 1
    assert len(returned_empty.calls) == 1
    crashed_status = crashed.calls[0]["sync_status"]
    empty_status = returned_empty.calls[0]["sync_status"]

    assert crashed_status == SYNC_STATUS_EXTRACTION_FAILED
    assert crashed_status == "extraction_failed"
    assert empty_status == "draft"
    assert crashed_status != empty_status, "崩溃与空返回在库里必须可区分"


@pytest.mark.asyncio
async def test_successful_extraction_still_marks_summary_synced(monkeypatch):
    """守住既有语义：提取成功仍然是 synced，beat_sections 不受影响。"""

    async def _good(*args, **kwargs):
        return {"summary": "林澈在雨夜听见铜铃。", "key_events": "铜铃第一次响起",
                "open_threads": "铜铃来自何处", "relation_triples": [], "foreshadow_hints": []}

    monkeypatch.setattr(cns, "llm_chapter_extract_bundle", _good)
    knowledge = _RecordingKnowledgeService(existing=None)

    await sync_chapter_narrative_after_save(
        novel_id="novel-1",
        chapter_number=3,
        content="雨夜，林澈握紧铜铃。",
        knowledge_service=knowledge,
        indexing_svc=None,
        llm_service=_FailingLLM(),
    )

    assert len(knowledge.calls) == 1
    call = knowledge.calls[0]
    assert call["sync_status"] == "synced"
    assert call["summary"] == "林澈在雨夜听见铜铃。"
    assert call["key_events"] == "铜铃第一次响起"
    assert call["open_threads"] == "铜铃来自何处"
