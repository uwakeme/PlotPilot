"""Validation 节点 — 校验与监控（6 个节点）

- val_style: 文风警报器
- val_tension: 张力评估器
- val_anti_ai: Anti-AI 审计
- val_foreshadow: 伏笔雷达
- val_narrative: 叙事同步
- val_kg_infer: 知识图谱推断
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from application.engine.dag.models import (
    NodeCategory,
    NodeMeta,
    NodePort,
    NodeResult,
    NodeStatus,
    PortDataType,
)
from application.engine.dag.registry import BaseNode, NodeRegistry
from domain.ai.value_objects.assessment_state import AssessmentState
from infrastructure.ai.prompt_keys import (
    CHAPTER_AFTERMATH,
    CLICHE_SCAN,
    FORESHADOW_CHECK,
    KG_INFERENCE,
    TENSION_SCORING,
    VOICE_DRIFT,
)

logger = logging.getLogger(__name__)


# ─── val_style: 文风警报器 ───


@NodeRegistry.register("val_style")
class StyleNode(BaseNode):
    """文风警报器 — VoiceDriftService"""

    meta = NodeMeta(
        node_type="val_style",
        display_name="文风警报器",
        category=NodeCategory.VALIDATION,
        icon="",
        color="#ec4899",
        input_ports=[
            NodePort(name="content", data_type=PortDataType.TEXT, required=True),
            NodePort(name="voice_fingerprint", data_type=PortDataType.TEXT, required=False),
        ],
        output_ports=[
            NodePort(name="drift_score", data_type=PortDataType.SCORE),
            NodePort(name="drift_alert", data_type=PortDataType.BOOLEAN),
        ],
        prompt_variables=["voice_fingerprint", "scene_type", "drift_threshold", "content"],
        is_configurable=True,
        can_disable=True,
        default_timeout_seconds=180,
        cpms_node_key=VOICE_DRIFT,
        description="VoiceDriftService 文风偏离检测",
        default_edges=["gw_circuit"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()
        content = inputs.get("content", "")
        novel_id = inputs.get("novel_id") or context.get("novel_id") or ""

        # chapter_number 必须是真的章节号。来源：registry.py:307-311 把 DAG state 的
        # chapter_number 放进 context，缺省为 0；而领域不变量是 chapter_number >= 1
        # （domain/novel/value_objects/timeline_event.py:19），所以 0/缺失 == 没有章节上下文。
        # 不伪造章节号：score_chapter 会 upsert chapter_style_scores，塞 0 会往库里灌
        # 一条「第 0 章」的垃圾基准，反而污染后续漂移判定。
        raw_chapter_number = inputs.get("chapter_number") or context.get("chapter_number") or 0
        try:
            chapter_number = int(raw_chapter_number)
        except (TypeError, ValueError):
            chapter_number = 0

        thresholds = self._config.thresholds if self._config else {}
        warning_threshold = thresholds.get("drift_warning", 0.5)

        drift_score: Any = None
        drift_alert: Any = None
        state = AssessmentState.UNASSESSED
        error: str = ""

        try:
            if not novel_id:
                raise ValueError("缺 novel_id，文风漂移未评估（不写入无主评分）")
            if chapter_number < 1:
                raise ValueError(
                    f"缺真实 chapter_number（拿到 {raw_chapter_number!r}），文风漂移未评估"
                )

            # 旧实现 VoiceDriftService() 无参构造（真实签名要 score_repo / fingerprint_repo
            # 两个必选位置参数，必然 TypeError）后又调不存在的 .analyze()，异常被本层
            # except 吞成一条 warning，drift_score 停在 0.0、节点恒 SUCCESS —— 文风警报器
            # 从未响过一次。改走 dependencies 的正确装配点（同 execution_nodes.py:94）。
            from interfaces.api.dependencies import get_voice_drift_service

            svc = get_voice_drift_service()
            result = await svc.score_chapter_async(novel_id, chapter_number, content)

            if not isinstance(result, dict):
                raise TypeError(
                    f"score_chapter_async 返回 {type(result).__name__}，预期 dict"
                )

            # 真实服务在无作者指纹基准时返回 similarity_score=None
            # （voice_drift_service.py:108-111）：那是「没得比」，不是「文风完美」，
            # 与「键缺失」一样按未评估处理，绝不回填 0.0。
            if result.get("similarity_score") is None:
                raise ValueError("VoiceDriftService 未给出 similarity_score（可能无指纹基准）")

            drift_score = float(result["similarity_score"])
            drift_alert = bool(result.get("drift_alert", False))
            if drift_score > warning_threshold:
                drift_alert = True
            state = AssessmentState.DECIDED
        except Exception as e:
            error = f"VoiceDriftService 调用失败: {e}"
            logger.warning(error)

        if state is AssessmentState.UNASSESSED:
            # 未评估：分数与警报都给 None，状态不谎报 SUCCESS。
            # NodeStatus 里没有 UNASSESSED 成员，取 ERROR —— 它是枚举中唯一表示
            # 「没产出结论」的值；SUCCESS 会谎报通过，WARNING 已被 drift_alert 占用
            # （真出警报时也返回 WARNING，两者含义不能混），BYPASSED/DISABLED 指节点
            # 被跳过而非跑失败。
            return NodeResult(
                outputs={
                    "drift_score": None,
                    "drift_alert": None,
                    "state": state,
                },
                status=NodeStatus.ERROR,
                duration_ms=int((time.time() - start) * 1000),
                error=error,
            )

        return NodeResult(
            outputs={
                "drift_score": drift_score,
                "drift_alert": drift_alert,
                "state": state,
            },
            status=NodeStatus.WARNING if drift_alert else NodeStatus.SUCCESS,
            metrics={"drift_score": drift_score},
            duration_ms=int((time.time() - start) * 1000),
        )

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return "content" in inputs


# ─── val_tension: 张力评估器 ───


@NodeRegistry.register("val_tension")
class TensionNode(BaseNode):
    """张力评估器 — TensionScoringService"""

    meta = NodeMeta(
        node_type="val_tension",
        display_name="张力评估器",
        category=NodeCategory.VALIDATION,
        icon="",
        color="#f59e0b",
        input_ports=[
            NodePort(name="content", data_type=PortDataType.TEXT, required=True),
        ],
        output_ports=[
            NodePort(name="plot_tension", data_type=PortDataType.SCORE),
            NodePort(name="emotional_tension", data_type=PortDataType.SCORE),
            NodePort(name="pacing_tension", data_type=PortDataType.SCORE),
            NodePort(name="composite", data_type=PortDataType.SCORE),
        ],
        prompt_variables=["content"],
        is_configurable=True,
        can_disable=True,
        default_timeout_seconds=60,
        cpms_node_key=TENSION_SCORING,
        description="TensionScoringService 叙事张力评估",
        default_edges=["gw_circuit"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()
        content = inputs.get("content", "")

        try:
            plot_tension = 0.0
            emotional_tension = 0.0
            pacing_tension = 0.0
            composite = 0.0

            try:
                from application.analyst.services.tension_scoring_service import TensionScoringService
                novel_id = context.get("novel_id", "")
                svc = TensionScoringService()
                result = await svc.score(novel_id, content)
                if result:
                    plot_tension = getattr(result, "plot_tension", 0.0)
                    emotional_tension = getattr(result, "emotional_tension", 0.0)
                    pacing_tension = getattr(result, "pacing_tension", 0.0)
                    composite = getattr(result, "composite", 0.0)
            except Exception as e:
                logger.warning(f"TensionScoringService 调用失败: {e}")

            return NodeResult(
                outputs={
                    "plot_tension": plot_tension,
                    "emotional_tension": emotional_tension,
                    "pacing_tension": pacing_tension,
                    "composite": composite,
                },
                status=NodeStatus.SUCCESS,
                metrics={"composite": composite},
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"composite": 0.0}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return "content" in inputs


# ─── val_anti_ai: Anti-AI 审计 ───


@NodeRegistry.register("val_anti_ai")
class AntiAINode(BaseNode):
    """Anti-AI 审计 — cliche_scanner (L7)"""

    meta = NodeMeta(
        node_type="val_anti_ai",
        display_name="Anti-AI 审计",
        category=NodeCategory.VALIDATION,
        icon="",
        color="#ef4444",
        input_ports=[
            NodePort(name="content", data_type=PortDataType.TEXT, required=True),
        ],
        output_ports=[
            NodePort(name="severity_score", data_type=PortDataType.SCORE),
            NodePort(name="hits", data_type=PortDataType.LIST),
            NodePort(name="recommendations", data_type=PortDataType.LIST),
        ],
        prompt_variables=["content"],
        is_configurable=True,
        can_disable=True,
        default_timeout_seconds=60,
        cpms_node_key=CLICHE_SCAN,
        description="ClicheScanner AI 模式检测与审计",
        default_edges=["gw_circuit"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()
        content = inputs.get("content", "")

        try:
            severity_score = 0.0
            hits = []
            recommendations = []

            try:
                from application.audit.services.cliche_scanner import ClicheScanner
                scanner = ClicheScanner()
                result = scanner.scan(content)
                if result:
                    severity_score = getattr(result, "severity_score", 0.0)
                    hits = getattr(result, "hits", [])
                    recommendations = getattr(result, "recommendations", [])
            except Exception as e:
                logger.warning(f"ClicheScanner 调用失败: {e}")

            return NodeResult(
                outputs={"severity_score": severity_score, "hits": hits, "recommendations": recommendations},
                status=NodeStatus.SUCCESS,
                metrics={"severity_score": severity_score},
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"severity_score": 0.0, "hits": [], "recommendations": []}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return "content" in inputs


# ─── val_foreshadow: 伏笔雷达 ───


@NodeRegistry.register("val_foreshadow")
class ForeshadowCheckNode(BaseNode):
    """伏笔雷达 — ForeshadowingRegistry"""

    meta = NodeMeta(
        node_type="val_foreshadow",
        display_name="伏笔雷达",
        category=NodeCategory.VALIDATION,
        icon="",
        color="#22c55e",
        input_ports=[
            NodePort(name="novel_id", data_type=PortDataType.TEXT, required=True),
            NodePort(name="content", data_type=PortDataType.TEXT, required=True),
        ],
        output_ports=[
            NodePort(name="recovered", data_type=PortDataType.SCORE),
            NodePort(name="pending", data_type=PortDataType.SCORE),
            NodePort(name="recovery_rate", data_type=PortDataType.SCORE),
        ],
        prompt_variables=[],
        is_configurable=False,
        can_disable=True,
        default_timeout_seconds=30,
        cpms_node_key=FORESHADOW_CHECK,
        description="ForeshadowingRegistry 伏笔回收检测",
        default_edges=["val_kg_infer"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()

        try:
            recovered = 0
            pending = 0
            recovery_rate = 0.0

            try:
                from domain.novel.repositories.foreshadowing_repository import ForeshadowingRepository
                from infrastructure.persistence.database.connection import get_database
                db = get_database()
                repo = ForeshadowingRepository(db)
                novel_id = inputs.get("novel_id") or context.get("novel_id", "")
                all_f = repo.find_by_novel(novel_id)
                recovered = len([f for f in all_f if getattr(f, 'status', '') == 'recovered'])
                pending = len([f for f in all_f if getattr(f, 'status', '') == 'pending'])
                total = recovered + pending
                recovery_rate = (recovered / total * 100) if total > 0 else 0.0
            except Exception as e:
                logger.warning(f"伏笔雷达调用失败: {e}")

            return NodeResult(
                outputs={"recovered": recovered, "pending": pending, "recovery_rate": recovery_rate},
                status=NodeStatus.SUCCESS,
                metrics={"recovery_rate": recovery_rate},
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"recovered": 0, "pending": 0, "recovery_rate": 0.0}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return True


# ─── val_narrative: 叙事同步 ───


@NodeRegistry.register("val_narrative")
class NarrativeNode(BaseNode):
    """叙事同步 — ChapterAftermathPipeline step 1"""

    meta = NodeMeta(
        node_type="val_narrative",
        display_name="叙事同步",
        category=NodeCategory.VALIDATION,
        icon="",
        color="#06b6d4",
        input_ports=[
            NodePort(name="content", data_type=PortDataType.TEXT, required=True),
        ],
        output_ports=[
            NodePort(name="summary", data_type=PortDataType.TEXT),
            NodePort(name="events", data_type=PortDataType.LIST),
            NodePort(name="triples", data_type=PortDataType.LIST),
            NodePort(name="causal_edges", data_type=PortDataType.LIST),
        ],
        prompt_variables=["content"],
        is_configurable=True,
        can_disable=True,
        default_timeout_seconds=180,
        cpms_node_key=CHAPTER_AFTERMATH,
        description="ChapterAftermathPipeline 叙事同步",
        default_edges=["val_foreshadow"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()
        content = inputs.get("content", "")

        try:
            summary = ""
            events = []
            triples = []
            causal_edges = []

            try:
                from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
                novel_id = context.get("novel_id", "")
                pipeline = ChapterAftermathPipeline()
                result = await pipeline.run_narrative_sync(novel_id, content)
                if result:
                    summary = getattr(result, "summary", "")
                    events = getattr(result, "events", [])
                    triples = getattr(result, "triples", [])
                    causal_edges = getattr(result, "causal_edges", [])
            except Exception as e:
                logger.warning(f"ChapterAftermathPipeline 调用失败: {e}")

            return NodeResult(
                outputs={"summary": summary, "events": events, "triples": triples, "causal_edges": causal_edges},
                status=NodeStatus.SUCCESS,
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"summary": "", "events": [], "triples": [], "causal_edges": []}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return "content" in inputs


# ─── val_kg_infer: 知识图谱推断 ───


@NodeRegistry.register("val_kg_infer")
class KGInferNode(BaseNode):
    """知识图谱推断 — KnowledgeGraphService.infer_from_chapter"""

    meta = NodeMeta(
        node_type="val_kg_infer",
        display_name="KG推断",
        category=NodeCategory.VALIDATION,
        icon="",
        color="#8b5cf6",
        input_ports=[
            NodePort(name="novel_id", data_type=PortDataType.TEXT, required=True),
            NodePort(name="chapter_number", data_type=PortDataType.SCORE, required=False),
        ],
        output_ports=[
            NodePort(name="inferred_triples", data_type=PortDataType.LIST),
        ],
        prompt_variables=[],
        is_configurable=False,
        can_disable=True,
        default_timeout_seconds=120,
        cpms_node_key=KG_INFERENCE,
        description="KnowledgeGraphService.infer_from_chapter",
        default_edges=["gw_review"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()

        try:
            inferred_triples = []

            try:
                from application.world.services.knowledge_graph_service import KnowledgeGraphService
                novel_id = inputs.get("novel_id") or context.get("novel_id", "")
                chapter_number = inputs.get("chapter_number") or context.get("chapter_number", 0)
                svc = KnowledgeGraphService()
                inferred_triples = await svc.infer_from_chapter(novel_id, chapter_number)
            except Exception as e:
                logger.warning(f"KnowledgeGraphService 调用失败: {e}")

            return NodeResult(
                outputs={"inferred_triples": inferred_triples},
                status=NodeStatus.SUCCESS,
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"inferred_triples": []}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return True
