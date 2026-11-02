# -*- coding: utf-8 -*-
"""第 82 批（N-07 / N-08）：**来源如实**与**不许假成功**。

N-07（A-05 残留）：关系谓词 / 地点连接类型
------------------------------------------------
旧格式分支（关系是字符串）里，谓词是**代码按关键词猜的**
（``"师徒" in rel`` → 「师徒关系」…）⇒ 而所有三元组 ``confidence`` 都写死 0.9
⇒ **代码的猜测**与**AI 的判断**在数据上完全同形 ⇒ 下游分不出来。

⇒ 现在：谓词来源进 ``attributes["predicate_source"]``，置信度按来源分档。

N-08（两处潜伏缺陷）
--------------------
① ``POST /novels/{id}/macro/generate`` 的 ``request.structure.dict()`` ——
   ``structure`` 是**可选**的 ⇒ 不传就 AttributeError（此前必崩）。
② DAG 的 ``exec_planning`` 节点少传必填参数 ``target_chapters``
   ⇒ TypeError 被 ``except`` 吞掉 ⇒ 节点恒 **SUCCESS + 空计划**（**假成功**）。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


# ══════════════════════ N-07 · 三元组谓词的来源 ══════════════════════


class _RecordingTripleRepo:
    def __init__(self):
        self.saved = []

    async def save(self, triple):
        self.saved.append(triple)


def _bible_generator():
    from application.world.services.auto_bible_generator import AutoBibleGenerator

    svc = AutoBibleGenerator.__new__(AutoBibleGenerator)
    svc.triple_repository = _RecordingTripleRepo()
    return svc


@pytest.mark.asyncio
async def test_the_keyword_guessed_predicate_is_marked_and_downscaled():
    """⚠ 旧格式（字符串关系）⇒ 谓词是**代码猜的** ⇒ 来源要标记、置信度要低。

    ⚠⚠ 期望值**写死字面量**（不从被测模块 import）——
      否则把常量从 0.5 改成 0.9 也会照样绿（自指恒真，第 80 批的老病）。
    """
    svc = _bible_generator()
    await svc._generate_character_triples(
        "n1",
        [
            ("c1", {"name": "张三", "relationships": ["与李四是师徒关系"]}),
            ("c2", {"name": "李四", "relationships": []}),
        ],
    )

    assert len(svc.triple_repository.saved) == 1, "没生成三元组"
    triple = svc.triple_repository.saved[0]
    assert triple.predicate == "师徒关系", "关键词归并的行为没变（这是兼容路径，不该变）"
    assert triple.attributes["predicate_source"] == "keyword_guess", (
        "代码猜的谓词没标来源 ⇒ 下游分不出它与 AI 判定的区别"
    )
    # ⚠ 期望值**写死字面量**（不从被测模块 import）—— 否则把常量改成 0.9 也照样绿
    assert triple.confidence == 0.5, (
        "代码猜的谓词不该拿 AI 那一档置信度（现值 %r）" % (triple.confidence,)
    )


@pytest.mark.asyncio
async def test_the_ai_given_predicate_keeps_the_high_confidence():
    """⚠ 对照组：新格式（AI 给的 relation）⇒ 来源 = ai、置信度照旧。"""
    svc = _bible_generator()
    await svc._generate_character_triples(
        "n1",
        [
            ("c1", {"name": "张三", "relationships": [
                {"target": "李四", "relation": "亦师亦友", "description": "d"}
            ]}),
            ("c2", {"name": "李四", "relationships": []}),
        ],
    )

    assert len(svc.triple_repository.saved) == 1
    triple = svc.triple_repository.saved[0]
    assert triple.predicate == "亦师亦友"
    assert triple.attributes["predicate_source"] == "ai"
    assert triple.confidence == 0.9, (
        "AI 给的谓词置信度变了（现值 %r）" % (triple.confidence,)
    )


@pytest.mark.asyncio
async def test_the_location_connection_has_the_same_provenance_rule():
    """⚠ 地点连接走同一套口径（关键词猜的也要标来源）。"""
    svc = _bible_generator()
    await svc._generate_location_triples(
        "n1",
        [
            ("l1", {"name": "青云城", "connections": ["内部包含东市"]}),
            ("l2", {"name": "东市", "connections": []}),
        ],
    )

    assert len(svc.triple_repository.saved) == 1
    triple = svc.triple_repository.saved[0]
    assert triple.attributes["predicate_source"] == "keyword_guess"
    assert triple.confidence == 0.5


def test_the_confidence_is_no_longer_hardcoded_to_one_value():
    """⚠ 结构性钉住：三元组创建处不许再出现写死的 ``confidence=0.9``。"""
    src = (ROOT / "application/world/services/auto_bible_generator.py").read_text(
        encoding="utf-8-sig"
    )
    assert "confidence=0.9" not in src, "三元组置信度还写死 0.9（代码猜测与 AI 判断同形）"


# ══════════════════════ N-08 · 路由与 DAG 节点 ══════════════════════


class _FakePlanningService:
    def __init__(self):
        self.calls = []
        self.errors = []

    def initialize_macro_plan_task(self, novel_id):
        return None

    async def generate_macro_plan(self, **kwargs):
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors[0]
        return {"plan": "p"}

    def store_macro_plan_result(self, novel_id, result):
        return None

    def store_macro_plan_error(self, novel_id, msg):
        self.errors.append(msg)

    def _update_macro_progress(self, novel_id, **kwargs):
        return None


@pytest.mark.asyncio
async def test_the_macro_route_survives_a_missing_structure():
    """⚠ ``structure`` 不传 ⇒ 不许 AttributeError（它是可选的）。"""
    from fastapi import BackgroundTasks

    from interfaces.api.v1.blueprint.continuous_planning_routes import (
        MacroPlanRequest,
        generate_macro_plan,
    )

    service = _FakePlanningService()
    request = MacroPlanRequest(target_chapters=12)

    background = BackgroundTasks()
    await generate_macro_plan(
        novel_id="n1", request=request, background_tasks=background, service=service
    )
    await background()

    assert service.calls, "规划服务没被调用"
    assert service.calls[0]["structure_preference"] is None, (
        "没传 structure 时应传 None，而不是炸掉"
    )
    assert service.calls[0]["target_chapters"] == 12


@pytest.mark.asyncio
async def test_the_macro_route_still_passes_the_structure_when_given():
    """⚠ 对照组：传了 structure ⇒ 照旧转成 dict。"""
    from fastapi import BackgroundTasks

    from interfaces.api.v1.blueprint.continuous_planning_routes import (
        MacroPlanRequest,
        generate_macro_plan,
    )

    service = _FakePlanningService()
    request = MacroPlanRequest(target_chapters=12, structure={"parts": 3})

    background = BackgroundTasks()
    await generate_macro_plan(
        novel_id="n1", request=request, background_tasks=background, service=service
    )
    await background()

    assert service.calls[0]["structure_preference"]["parts"] == 3, (
        "传了 structure 时应原样带过去"
    )


@pytest.mark.asyncio
async def test_the_dag_planning_node_passes_the_target_chapters(monkeypatch):
    """⚠ DAG 节点必须把 ``target_chapters`` 传进去（少传 ⇒ TypeError ⇒ 假成功）。"""
    import interfaces.api.dependencies as deps
    from application.engine.dag.nodes.execution_nodes import PlanningNode

    service = _FakePlanningService()
    monkeypatch.setattr(deps, "get_continuous_planning_service", lambda: service)

    result = await PlanningNode().execute({"novel_id": "n1", "target_chapters": 12}, {})

    assert service.calls, "规划服务没被调用"
    assert service.calls[0]["target_chapters"] == 12, "目标章数没传进去"
    assert result.status.value == "success"


@pytest.mark.asyncio
async def test_the_dag_planning_node_does_not_report_a_fake_success(monkeypatch):
    """⚠ 服务失败 ⇒ 节点必须报 **ERROR**（不许「SUCCESS + 空计划」）。

    ⚠ 「假成功」的形状：异常被吞 ⇒ 画布上看着跑通了，其实什么都没产出。
    """
    import interfaces.api.dependencies as deps
    from application.engine.dag.nodes.execution_nodes import PlanningNode

    service = _FakePlanningService()
    service.errors = [RuntimeError("upstream 502")]
    monkeypatch.setattr(deps, "get_continuous_planning_service", lambda: service)

    result = await PlanningNode().execute({"novel_id": "n1", "target_chapters": 12}, {})

    assert result.status.value == "error", (
        "规划失败却报成功 ⇒ 假成功（画布在骗人）"
    )
    assert result.error, "报错时必须带原因"


@pytest.mark.asyncio
async def test_the_dag_planning_node_reports_a_missing_target_chapters(monkeypatch):
    """⚠ 拿不到目标章数 ⇒ 明确报错（不许静默产出空计划）。"""
    import interfaces.api.dependencies as deps
    from application.engine.dag.nodes.execution_nodes import PlanningNode

    class _NoNovelRepo:
        def get_by_id(self, _novel_id):
            return None

    service = _FakePlanningService()
    service.novel_repository = _NoNovelRepo()
    monkeypatch.setattr(deps, "get_continuous_planning_service", lambda: service)

    result = await PlanningNode().execute({"novel_id": "n1"}, {})

    assert result.status.value == "error", "缺参数却报成功 ⇒ 假成功"
