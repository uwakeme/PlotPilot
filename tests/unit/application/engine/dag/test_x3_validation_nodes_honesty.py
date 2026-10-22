"""X-03：DAG 校验节点不许「恒假成功」。

核实结论（运行时探针 + 读码）：6 个校验节点里 **5 个从来没真正跑过一次** ——
它们调用的依赖方法名/签名全对不上：

| 节点 | 旧实现调的东西 | 真实 API | 后果 |
|---|---|---|---|
| `val_anti_ai` | `ClicheScanner().scan(content)` | `scan_cliches` / `scan_by_category` / `get_critical_count` / `get_category_stats` | AttributeError ⇒ 吞掉 ⇒ 恒 SUCCESS + 0 分 |
| `val_tension` | `TensionScoringService().score(novel_id, content)` | `score_chapter(content, chapter_number)`（且构造函数要 `llm_service`） | TypeError/AttributeError ⇒ 同上 |
| `val_foreshadow` | `ForeshadowingRepository(db).find_by_novel(id)` | `get_by_novel_id` | AttributeError ⇒ 同上 |
| `val_narrative` | `ChapterAftermathPipeline().run_narrative_sync(...)` | `run_after_chapter_saved`（且构造函数要 2 个必选依赖） | AttributeError/TypeError ⇒ 同上 |
| `val_kg_infer` | `KnowledgeGraphService().infer_from_chapter(novel_id, chapter_number)` | `infer_from_chapter(chapter_id)`（且构造函数要 3 个仓储） | TypeError ⇒ 同上 |

⇒ 「分数 0 + 空命中 + SUCCESS」读起来完全像「查过了，没问题」。

本文件的守卫：
- `val_anti_ai` **真的**扫出命中（喂一段含俗套句的正文 ⇒ 分数 > 0、hits 非空）；
- `val_anti_ai` 走的是既有单一事实源 `AntiAIAuditor`（接线守卫）；
- 依赖失败时一律「未评估」（输出为 None + state=UNASSESSED + status=ERROR），
  **不是** SUCCESS + 0/[]。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.engine.dag.models import NodeStatus
from application.engine.dag.nodes.validation_nodes import (
    AntiAINode,
    ForeshadowCheckNode,
    KGInferNode,
    NarrativeNode,
    TensionNode,
)
from domain.ai.value_objects.assessment_state import AssessmentState


# ─────────────────────────── val_anti_ai ───────────────────────────


@pytest.mark.asyncio
async def test_anti_ai_node_really_scans_cliches():
    """喂一段含俗套句的正文 ⇒ 节点必须**真的**扫出命中（不是恒 0 分）。"""
    node = AntiAINode()
    content = "他嘴角勾起一抹笑意，眼里闪过一丝不易察觉的温柔。"

    result = await node.execute({"content": content}, {"novel_id": "n-1"})

    assert result.status in (NodeStatus.SUCCESS, NodeStatus.WARNING)
    assert result.outputs["severity_score"] > 0, "含俗套句却报 0 分 ⇒ 又在假成功"
    assert result.outputs["hits"], "有命中却报空列表"
    assert result.outputs["state"] == AssessmentState.DECIDED


@pytest.mark.asyncio
async def test_anti_ai_node_clean_content_is_a_real_zero():
    """干净正文 ⇒ 真的是 0 分（**已评估**），不是「没评估」。"""
    node = AntiAINode()

    result = await node.execute({"content": "他放下杯子，推开门走了出去。"}, {"novel_id": "n-1"})

    assert result.outputs["severity_score"] == 0.0
    assert result.outputs["hits"] == []
    assert result.outputs["state"] == AssessmentState.DECIDED


@pytest.mark.asyncio
async def test_anti_ai_node_goes_through_the_shared_auditor(monkeypatch):
    """接线守卫：分数/命中/建议必须来自 `AntiAIAuditor`（章后审计的同一份口径）。"""
    from application.audit.services.anti_ai_audit import AntiAIAuditor

    stub_report = SimpleNamespace(
        metrics=SimpleNamespace(severity_score=42.5),
        hits=[
            SimpleNamespace(
                pattern="嘴角微表情",
                text="嘴角勾起",
                start=0,
                end=4,
                severity="critical",
                category="微表情",
                replacement_hint="写完整姿态变化",
            )
        ],
        recommendations=["别写嘴角勾起"],
    )
    captured = {}

    def _fake_scan(self, chapter_id, content, novel_id=""):
        captured["chapter_id"] = chapter_id
        captured["content"] = content
        captured["novel_id"] = novel_id
        return stub_report

    monkeypatch.setattr(AntiAIAuditor, "scan_chapter", _fake_scan)

    node = AntiAINode()
    result = await node.execute({"content": "正文"}, {"novel_id": "n-9", "chapter_number": 7})

    assert result.outputs["severity_score"] == 42.5
    assert result.outputs["hits"][0]["pattern"] == "嘴角微表情"
    assert result.outputs["recommendations"] == ["别写嘴角勾起"]
    assert captured["novel_id"] == "n-9"


@pytest.mark.asyncio
async def test_anti_ai_node_failure_is_unassessed_not_fake_success(monkeypatch):
    """依赖失败 ⇒ 显式「未评估」，**不是** SUCCESS + 0 分。"""
    from application.audit.services.anti_ai_audit import AntiAIAuditor

    def _boom(self, chapter_id, content, novel_id=""):
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(AntiAIAuditor, "scan_chapter", _boom)

    node = AntiAINode()
    result = await node.execute({"content": "正文"}, {"novel_id": "n-1"})

    assert result.status == NodeStatus.ERROR
    assert result.outputs["severity_score"] is None
    assert result.outputs["hits"] is None
    assert result.outputs["state"] == AssessmentState.UNASSESSED
    assert "scanner exploded" in (result.error or "")


# ─────────────── 其它四个节点：失败必须「未评估」 ───────────────


@pytest.mark.asyncio
async def test_tension_node_failure_is_unassessed():
    node = TensionNode()
    result = await node.execute({"content": "正文"}, {"novel_id": "n-1"})

    assert result.status == NodeStatus.ERROR
    assert result.outputs["composite"] is None
    assert result.outputs["state"] == AssessmentState.UNASSESSED
    assert result.error


@pytest.mark.asyncio
async def test_foreshadow_node_failure_is_unassessed():
    node = ForeshadowCheckNode()
    result = await node.execute({"novel_id": "n-1", "content": "正文"}, {})

    assert result.status == NodeStatus.ERROR
    assert result.outputs["recovered"] is None
    assert result.outputs["recovery_rate"] is None
    assert result.outputs["state"] == AssessmentState.UNASSESSED
    assert result.error


@pytest.mark.asyncio
async def test_narrative_node_failure_is_unassessed():
    node = NarrativeNode()
    result = await node.execute({"content": "正文"}, {"novel_id": "n-1"})

    assert result.status == NodeStatus.ERROR
    assert result.outputs["summary"] is None
    assert result.outputs["triples"] is None
    assert result.outputs["state"] == AssessmentState.UNASSESSED
    assert result.error


@pytest.mark.asyncio
async def test_kg_infer_node_failure_is_unassessed():
    node = KGInferNode()
    result = await node.execute({"novel_id": "n-1", "chapter_number": 3}, {})

    assert result.status == NodeStatus.ERROR
    assert result.outputs["inferred_triples"] is None
    assert result.outputs["state"] == AssessmentState.UNASSESSED
    assert result.error


# ─────────────── 依赖的「真实 API 形状」不许再漂移 ───────────────


def test_validation_nodes_call_real_dependency_apis():
    """依赖方法名/签名不许再漂移（漂了就是「假成功」的来源）。

    这条守卫是**前瞻**的：它盯的是依赖类本身有没有那些方法，
    节点改调用点时会被它挡住。
    """
    from application.analyst.services.tension_scoring_service import TensionScoringService
    from application.audit.services.cliche_scanner import ClicheScanner
    from application.engine.services.chapter_aftermath_pipeline import (
        ChapterAftermathPipeline,
    )
    from application.world.services.knowledge_graph_service import KnowledgeGraphService
    from domain.novel.repositories.foreshadowing_repository import (
        ForeshadowingRepository,
    )

    assert hasattr(ClicheScanner, "scan_cliches")
    assert not hasattr(ClicheScanner, "scan")
    assert hasattr(TensionScoringService, "score_chapter")
    assert hasattr(ForeshadowingRepository, "get_by_novel_id")
    assert hasattr(ChapterAftermathPipeline, "run_after_chapter_saved")
    assert hasattr(KnowledgeGraphService, "infer_from_chapter")


@pytest.mark.asyncio
async def test_anti_ai_node_does_not_call_the_nonexistent_scan(monkeypatch):
    """反向守卫：绝不能再调 `scanner.scan(...)`（那个方法根本不存在）。"""
    from application.audit.services.cliche_scanner import ClicheScanner

    def _boom(*args, **kwargs):
        raise AssertionError("不得调用不存在的 ClicheScanner.scan")

    monkeypatch.setattr(ClicheScanner, "scan", _boom, raising=False)

    node = AntiAINode()
    result = await node.execute({"content": "他嘴角勾起一抹笑意。"}, {"novel_id": "n-1"})

    assert result.outputs["severity_score"] > 0
