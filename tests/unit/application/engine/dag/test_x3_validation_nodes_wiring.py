"""X-03：DAG 校验节点的**真装配**守卫（第 48 批）。

背景（读码核实，不是推测）：X-03 之前有 4 个校验节点的依赖调用全对不上，
每次都抛异常、被节点自己的 ``except Exception`` 吞成一条 warning，
于是「分数 0 + 空命中 + SUCCESS」读起来完全像「查过了，没问题」：

| 节点 | 旧实现调的东西 | 真实 API |
|---|---|---|
| `val_tension` | `TensionScoringService()` + `.score(novel_id, content)` | 构造要 `llm_service`；方法是 `score_chapter(content, chapter_number)` |
| `val_foreshadow` | `ForeshadowingRepository(db)` + `.find_by_novel(id)` | 那是**抽象基类**（实例化必 TypeError）；真实实现是 `SqliteForeshadowingRepository.get_by_novel_id(NovelId)` |
| `val_narrative` | `ChapterAftermathPipeline()` + `.run_narrative_sync(...)` | 构造要 knowledge+llm；入口是 `run_after_chapter_saved(novel_id, chapter_number, content)` |
| `val_kg_infer` | `KnowledgeGraphService()` + `.infer_from_chapter(novel_id, chapter_number)` | 构造要 3 个仓储；方法是 `infer_from_chapter(chapter_id)`（**story_nodes.id**） |

已有的 `test_x3_validation_nodes_honesty.py` 只守「失败时不谎报」，
**守不住「真的跑通了」** —— 装配再错也照样是「未评估」，守卫照样绿。
本文件补的就是这一层：

- 4 个节点各有一条**真跑通**的守卫（喂真数据 ⇒ 真产出、真落库）；
- 2 条**通用**守卫：① 校验节点不许无参构造依赖；② 节点的输出端口
  必须与它真实产出的键一致（能力与出口对齐）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from application.engine.dag.models import NodeStatus
from application.engine.dag.nodes.validation_nodes import (
    ForeshadowCheckNode,
    KGInferNode,
    NarrativeNode,
    TensionNode,
)
from domain.ai.value_objects.assessment_state import AssessmentState

VALIDATION_NODES_FILE = Path("application/engine/dag/nodes/validation_nodes.py")


# ─────────────────────────── 测试夹具 ───────────────────────────


def _tmp_db(tmp_path, monkeypatch, name: str):
    """把 DI 的 ``get_database`` 指向临时库（生产库一律不碰）。"""
    from infrastructure.persistence.database.connection import get_database
    import interfaces.api.dependencies as deps

    db = get_database(str(tmp_path / name))
    monkeypatch.setattr(deps, "get_database", lambda *a, **k: db)
    return db


@pytest.fixture
def kg_db(tmp_path, monkeypatch):
    return _tmp_db(tmp_path, monkeypatch, "x3-kg.db")


@pytest.fixture
def fs_db(tmp_path, monkeypatch):
    return _tmp_db(tmp_path, monkeypatch, "x3-fs.db")


def _seed_novel(db, novel_id: str) -> None:
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        (novel_id, "X3", novel_id, 10),
    )
    db.get_connection().commit()


# ─────────────────────── val_kg_infer ───────────────────────


def _seed_kg_chapter(db, novel_id: str = "n-x3", chapter_number: int = 1) -> None:
    _seed_novel(db, novel_id)
    db.execute(
        """
        INSERT INTO story_nodes (
            id, novel_id, node_type, number, title, order_index, pov_character_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("sn-ch1", novel_id, "chapter", chapter_number, "第一章", 0, "char-1"),
    )
    # chapters 表里同一章的 id 是**另一个 id 空间** —— 用来证明 KG 口径不是它
    db.execute(
        "INSERT INTO chapters (id, novel_id, number, title) VALUES (?, ?, ?, ?)",
        ("chapter-row-1", novel_id, chapter_number, "第一章"),
    )
    for i, element_id in enumerate(["char-1", "char-2"]):
        db.execute(
            """
            INSERT INTO chapter_elements (
                id, chapter_id, element_type, element_id,
                relation_type, importance, appearance_order, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"elem-{i}",
                "sn-ch1",
                "character",
                element_id,
                "appears",
                "major",
                i,
                "2026-01-01T00:00:00",
            ),
        )
    db.get_connection().commit()


@pytest.mark.asyncio
async def test_kg_infer_node_really_infers_and_persists_triples(kg_db):
    """两个共现角色 ⇒ 节点必须**真的**推断出三元组并落库（不是恒空列表）。"""
    _seed_kg_chapter(kg_db)

    node = KGInferNode()
    result = await node.execute({"novel_id": "n-x3", "chapter_number": 1}, {})

    assert result.status == NodeStatus.SUCCESS, result.error
    assert result.outputs["state"] == AssessmentState.DECIDED
    triples = result.outputs["inferred_triples"]
    assert triples, "喂了两个共现角色却推断出 0 条三元组 ⇒ 又在假成功"
    predicates = {t["predicate"] for t in triples}
    assert "认识" in predicates, f"缺少共现推断出的「认识」关系，实际: {predicates}"

    rows = kg_db.fetch_all(
        "SELECT id, predicate FROM triples WHERE novel_id = ?", ("n-x3",)
    )
    assert rows, "推断出的三元组没有落库 ⇒ 能力没接通"


def test_kg_story_node_id_is_story_nodes_id_not_chapters_id(kg_db):
    """KG 口径：**story_nodes.id**，不是 chapters.id（两套 id 空间不同）。"""
    from interfaces.api.dependencies import get_knowledge_graph_service

    _seed_kg_chapter(kg_db)
    svc = get_knowledge_graph_service()

    sid = svc.resolve_story_node_id("n-x3", 1)

    assert sid == "sn-ch1", "解析到的不是结构节点 id"
    assert sid != "chapter-row-1", "解析到了 chapters 表的 id ⇒ KG 口径串台"


def test_story_node_id_lookup_has_a_single_implementation(kg_db):
    """「小说+章节号 → story_nodes.id」在仓储里**只留一份实现**。

    两处各写一份 SQL ⇒ 各改各的、悄悄漂成两个口径（本次就差点这么干）。
    """
    from infrastructure.persistence.database.sqlite_knowledge_repository import (
        SqliteKnowledgeRepository,
    )
    from infrastructure.persistence.database.story_node_repository import (
        StoryNodeRepository,
    )

    kr_src = Path(
        "infrastructure/persistence/database/sqlite_knowledge_repository.py"
    ).read_text(encoding="utf-8")
    assert "SELECT id FROM story_nodes" not in kr_src, (
        "SqliteKnowledgeRepository 又自带了一份 SQL ⇒ 口径要漂"
    )

    _seed_kg_chapter(kg_db)
    canonical = StoryNodeRepository(kg_db).get_chapter_node_id("n-x3", 1)
    delegated = SqliteKnowledgeRepository(kg_db).find_story_node_id_for_chapter_number(
        "n-x3", 1
    )
    assert canonical == "sn-ch1"
    assert delegated == canonical, "委派结果与仓储实现不一致"


@pytest.mark.asyncio
async def test_kg_infer_node_without_story_node_is_unassessed(kg_db):
    """没有结构节点 ⇒ 未评估（不是「推断出 0 条」的假结论）。"""
    _seed_novel(kg_db, "n-x3")

    node = KGInferNode()
    result = await node.execute({"novel_id": "n-x3", "chapter_number": 9}, {})

    assert result.status == NodeStatus.ERROR
    assert result.outputs["inferred_triples"] is None
    assert result.outputs["state"] == AssessmentState.UNASSESSED
    assert result.error


@pytest.mark.asyncio
async def test_kg_infer_node_without_chapter_number_is_unassessed(kg_db):
    """缺真实章节号 ⇒ 未评估（不许拿「第 0 章」凑数）。"""
    node = KGInferNode()
    result = await node.execute({"novel_id": "n-x3"}, {})

    assert result.status == NodeStatus.ERROR
    assert result.outputs["inferred_triples"] is None
    assert result.outputs["state"] == AssessmentState.UNASSESSED
    assert result.error


# ─────────────────────── val_foreshadow ───────────────────────


def _seed_foreshadow_registry(db, novel_id: str = "n-fs") -> None:
    from domain.novel.entities.foreshadowing_registry import ForeshadowingRegistry
    from domain.novel.value_objects.foreshadowing import (
        Foreshadowing,
        ForeshadowingStatus,
        ImportanceLevel,
    )
    from domain.novel.value_objects.novel_id import NovelId
    from infrastructure.persistence.database.sqlite_foreshadowing_repository import (
        SqliteForeshadowingRepository,
    )

    _seed_novel(db, novel_id)
    registry = ForeshadowingRegistry(id=f"fr-{novel_id}", novel_id=NovelId(novel_id))
    registry.register(
        Foreshadowing(
            id="f-1",
            planted_in_chapter=1,
            description="埋下的伏笔一",
            importance=ImportanceLevel.HIGH,
            status=ForeshadowingStatus.PLANTED,
            suggested_resolve_chapter=5,
        )
    )
    registry.register(
        Foreshadowing(
            id="f-2",
            planted_in_chapter=1,
            description="埋下的伏笔二",
            importance=ImportanceLevel.MEDIUM,
            status=ForeshadowingStatus.PLANTED,
            suggested_resolve_chapter=6,
        )
    )
    registry.register(
        Foreshadowing(
            id="f-3",
            planted_in_chapter=1,
            description="已回收的伏笔三",
            importance=ImportanceLevel.LOW,
            status=ForeshadowingStatus.RESOLVED,
            resolved_in_chapter=3,
        )
    )
    registry.register(
        Foreshadowing(
            id="f-4",
            planted_in_chapter=1,
            description="已放弃的伏笔四",
            importance=ImportanceLevel.LOW,
            status=ForeshadowingStatus.ABANDONED,
        )
    )
    SqliteForeshadowingRepository(db).save(registry)


@pytest.mark.asyncio
async def test_foreshadow_node_really_counts_registry(fs_db):
    """真读注册表 ⇒ 回收/待回收/回收率都对（ABANDONED 不计入分母）。"""
    _seed_foreshadow_registry(fs_db)

    node = ForeshadowCheckNode()
    result = await node.execute({"novel_id": "n-fs", "content": "正文"}, {})

    assert result.status == NodeStatus.SUCCESS, result.error
    assert result.outputs["state"] == AssessmentState.DECIDED
    assert result.outputs["recovered"] == 1
    assert result.outputs["pending"] == 2
    assert result.outputs["recovery_rate"] == pytest.approx(33.3333, abs=0.01)


@pytest.mark.asyncio
async def test_foreshadow_node_unknown_novel_is_unassessed(fs_db):
    """书不存在 ⇒ 未评估（不是「0 条待回收」的假结论）。"""
    node = ForeshadowCheckNode()
    result = await node.execute({"novel_id": "n-not-exist", "content": "正文"}, {})

    assert result.status == NodeStatus.ERROR
    assert result.outputs["recovered"] is None
    assert result.outputs["state"] == AssessmentState.UNASSESSED
    assert result.error


# ─────────────────────── val_tension ───────────────────────


@pytest.mark.asyncio
async def test_tension_node_calls_real_score_chapter(monkeypatch):
    """真调 score_chapter + 真映射四个分维（含 composite ← composite_score）。"""
    import interfaces.api.dependencies as deps
    from application.analyst.services.tension_scoring_service import (
        TensionScoringService,
    )
    from domain.novel.value_objects.tension_dimensions import TensionDimensions

    sentinel_llm = object()
    monkeypatch.setattr(deps, "get_llm_service", lambda: sentinel_llm)
    captured = {}

    async def _fake_score(self, content, chapter_number, prev_chapter_tension=50.0):
        captured["llm"] = self._llm
        captured["content"] = content
        captured["chapter_number"] = chapter_number
        return TensionDimensions(
            plot_tension=70.0,
            emotional_tension=50.0,
            pacing_tension=60.0,
            composite_score=61.0,
        )

    monkeypatch.setattr(TensionScoringService, "score_chapter", _fake_score)

    node = TensionNode()
    result = await node.execute(
        {"content": "正文", "novel_id": "n-1", "chapter_number": 3}, {}
    )

    assert result.status == NodeStatus.SUCCESS, result.error
    assert result.outputs["state"] == AssessmentState.DECIDED
    assert result.outputs["plot_tension"] == 70.0
    assert result.outputs["emotional_tension"] == 50.0
    assert result.outputs["pacing_tension"] == 60.0
    assert result.outputs["composite"] == 61.0
    assert captured["llm"] is sentinel_llm, "服务没拿到 llm_service ⇒ 构造漏传"
    assert captured["chapter_number"] == 3


@pytest.mark.asyncio
async def test_tension_node_unevaluated_sentinel_is_not_a_score(monkeypatch):
    """服务返回 UNEVALUATED(-1) 哨兵 ⇒ 未评估，**不**把 -1 当分数报出去。"""
    import interfaces.api.dependencies as deps
    from application.analyst.services.tension_scoring_service import (
        TensionScoringService,
    )
    from domain.novel.value_objects.tension_dimensions import TensionDimensions

    monkeypatch.setattr(deps, "get_llm_service", lambda: object())

    async def _fake_score(self, content, chapter_number, prev_chapter_tension=50.0):
        return TensionDimensions.unevaluated()

    monkeypatch.setattr(TensionScoringService, "score_chapter", _fake_score)

    node = TensionNode()
    result = await node.execute(
        {"content": "正文", "novel_id": "n-1", "chapter_number": 3}, {}
    )

    assert result.status == NodeStatus.ERROR
    assert result.outputs["composite"] is None
    assert result.outputs["state"] == AssessmentState.UNASSESSED
    assert result.error


@pytest.mark.asyncio
async def test_tension_node_without_chapter_number_is_unassessed(monkeypatch):
    """缺真实章节号 ⇒ 未评估。"""
    node = TensionNode()
    result = await node.execute({"content": "正文", "novel_id": "n-1"}, {})

    assert result.status == NodeStatus.ERROR
    assert result.outputs["composite"] is None
    assert result.outputs["state"] == AssessmentState.UNASSESSED
    assert result.error


# ─────────────────────── val_narrative ───────────────────────


@pytest.mark.asyncio
async def test_narrative_node_calls_real_aftermath(monkeypatch):
    """真调 run_after_chapter_saved，并把它返回的标志位如实透出。"""
    import interfaces.api.dependencies as deps

    captured = {}

    class _FakePipeline:
        async def run_after_chapter_saved(
            self, novel_id, chapter_number, content, **kwargs
        ):
            captured.update(
                novel_id=novel_id,
                chapter_number=chapter_number,
                content=content,
            )
            return {
                "narrative_sync_ok": True,
                "triples_extracted": True,
                "causal_edges_stored": False,
                "foreshadow_stored": True,
                "vector_stored": True,
            }

    monkeypatch.setattr(
        deps, "get_chapter_aftermath_pipeline", lambda: _FakePipeline()
    )

    node = NarrativeNode()
    result = await node.execute(
        {"content": "正文", "novel_id": "n-1", "chapter_number": 2}, {}
    )

    assert result.status == NodeStatus.SUCCESS, result.error
    assert result.outputs["state"] == AssessmentState.DECIDED
    assert result.outputs["narrative_sync_ok"] is True
    assert result.outputs["triples_extracted"] is True
    assert result.outputs["causal_edges_stored"] is False
    assert result.outputs["foreshadow_stored"] is True
    assert result.outputs["vector_stored"] is True
    assert captured["novel_id"] == "n-1"
    assert captured["chapter_number"] == 2
    assert captured["content"] == "正文"


@pytest.mark.asyncio
async def test_narrative_node_failed_sync_is_unassessed(monkeypatch):
    """管线自报「同步没成功」⇒ 未评估（不谎报「已完成」）。"""
    import interfaces.api.dependencies as deps

    class _FakePipeline:
        async def run_after_chapter_saved(
            self, novel_id, chapter_number, content, **kwargs
        ):
            return {"narrative_sync_ok": False}

    monkeypatch.setattr(
        deps, "get_chapter_aftermath_pipeline", lambda: _FakePipeline()
    )

    node = NarrativeNode()
    result = await node.execute(
        {"content": "正文", "novel_id": "n-1", "chapter_number": 2}, {}
    )

    assert result.status == NodeStatus.ERROR
    assert result.outputs["narrative_sync_ok"] is None
    assert result.outputs["state"] == AssessmentState.UNASSESSED
    assert result.error


# ─────────────────── 通用守卫（对 4 个节点一视同仁） ───────────────────


def test_validation_nodes_never_construct_dependencies_without_args():
    """历史病灶：依赖类无参构造 → 必抛 TypeError → 节点恒「未评估」。

    这几个类的构造函数都要求必选参数，无参构造必然失败；
    ``ForeshadowingRepository`` 更是抽象基类，**任何**实例化都不行。
    """
    tree = ast.parse(VALIDATION_NODES_FILE.read_text(encoding="utf-8"))

    no_arg_forbidden = {
        "TensionScoringService",
        "KnowledgeGraphService",
        "ChapterAftermathPipeline",
    }
    never_instantiate = {"ForeshadowingRepository"}

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        if name in no_arg_forbidden and not node.args and not node.keywords:
            offenders.append(f"{name}() 无参构造")
        if name in never_instantiate:
            offenders.append(f"{name} 抽象基类被实例化")

    assert not offenders, f"校验节点又构造错了依赖: {offenders}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "node_cls,inputs",
    [
        (KGInferNode, {"novel_id": "n-1"}),
        (ForeshadowCheckNode, {"novel_id": ""}),
        (TensionNode, {"content": "正文"}),
        (NarrativeNode, {"content": "正文"}),
    ],
    ids=["KGInferNode", "ForeshadowCheckNode", "TensionNode", "NarrativeNode"],
)
async def test_declared_output_ports_match_real_output_keys(node_cls, inputs):
    """输出端口必须与节点**真实产出**的键一致（能力与出口对齐）。

    旧 `val_narrative` 宣称输出 summary/events/triples/causal_edges 四个**内容列表**，
    而章后管线真实返回的是**布尔标志** ⇒ 端口即 fiction。
    """
    node = node_cls()
    result = await node.execute(inputs, {})

    declared = {p.name for p in node.meta.output_ports}
    produced = set(result.outputs.keys()) - {"state"}

    assert produced == declared, (
        f"{node_cls.__name__} 端口与产出不一致："
        f"多出 {produced - declared}，缺了 {declared - produced}"
    )
