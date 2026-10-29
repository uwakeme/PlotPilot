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


def _unassessed_result(
    outputs: Dict[str, Any], error: str, duration_ms: int
) -> NodeResult:
    """未评估的统一出口（与 ``val_style`` / ``anti_ai_audit`` 同一套约定）。

    NodeStatus 里没有 UNASSESSED 成员 ⇒ 用 ERROR（它是枚举中唯一表示
    「没产出结论」的值）。

    ⚠ X-03：**不能**回 SUCCESS + 0 / [] ——
    那读起来像「查过了，没问题」，而实际是「根本没查成」。
    本文件此前 5 个校验节点全是这么干的：依赖方法名/签名对不上 ⇒
    必抛异常 ⇒ 被内层 ``except Exception`` 吞成一条 warning ⇒
    节点恒 SUCCESS + 分数 0 ⇒ **每个校验节点都没真正跑过一次**。
    """
    payload = {key: None for key in outputs}
    payload["state"] = AssessmentState.UNASSESSED
    return NodeResult(
        outputs=payload,
        status=NodeStatus.ERROR,
        duration_ms=duration_ms,
        error=error,
    )


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
            NodePort(name="novel_id", data_type=PortDataType.TEXT, required=False),
            NodePort(name="chapter_number", data_type=PortDataType.SCORE, required=False),
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
        novel_id = inputs.get("novel_id") or context.get("novel_id") or ""
        raw_chapter_number = inputs.get("chapter_number") or context.get("chapter_number") or 0
        try:
            chapter_number = int(raw_chapter_number)
        except (TypeError, ValueError):
            chapter_number = 0

        # ⚠ X-03：旧实现把异常吞成一条 warning、分数停在 0.0、节点恒 SUCCESS。
        # 而 `TensionScoringService.score` **不存在**（真实方法是 score_chapter），
        # 且构造函数要 llm_service ⇒ 这一步**每次都失败** ⇒ 张力节点从未真正评过分。
        # 现在：走 DI 工厂 + 真实方法；失败 ⇒ 显式「未评估」（分数为 None），不再谎报 0 分通过。
        plot_tension: Any = None
        emotional_tension: Any = None
        pacing_tension: Any = None
        composite: Any = None
        state = AssessmentState.UNASSESSED
        error = ""

        try:
            if not novel_id:
                raise ValueError("缺 novel_id，张力未评估（不写入无主评分）")
            if chapter_number < 1:
                raise ValueError(
                    f"缺真实 chapter_number（拿到 {raw_chapter_number!r}），张力未评估"
                )

            from application.analyst.services.tension_scoring_service import (
                TensionScoringService,
            )
            from interfaces.api.dependencies import get_llm_service

            svc = TensionScoringService(get_llm_service())
            dims = await svc.score_chapter(content, chapter_number)

            # 服务在「提示词渲染失败 / LLM 解析失败」时返回 UNEVALUATED(-1) 哨兵
            # （domain/novel/value_objects/tension_dimensions.py:8）——
            # 那不是「张力 0 分」，是「没评出来」⇒ 同样按未评估处理，绝不回填 0。
            if not dims.is_evaluated:
                raise ValueError("TensionScoringService 返回未评估哨兵（-1）")

            plot_tension = float(dims.plot_tension)
            emotional_tension = float(dims.emotional_tension)
            pacing_tension = float(dims.pacing_tension)
            composite = float(dims.composite_score)
            state = AssessmentState.DECIDED
        except Exception as e:
            error = f"TensionScoringService 调用失败: {e}"
            logger.warning(error)

        outputs = {
            "plot_tension": plot_tension,
            "emotional_tension": emotional_tension,
            "pacing_tension": pacing_tension,
            "composite": composite,
        }
        if state is AssessmentState.UNASSESSED:
            return _unassessed_result(outputs, error, int((time.time() - start) * 1000))

        return NodeResult(
            outputs={**outputs, "state": state},
            status=NodeStatus.SUCCESS,
            metrics={"composite": composite},
            duration_ms=int((time.time() - start) * 1000),
        )

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
        novel_id = inputs.get("novel_id") or context.get("novel_id") or ""
        # chapter_id 在 AntiAIAuditor 里只是个**标签**（不进库），
        # 这里没有真正的章节 id，用章号拼一个可读的即可。
        chapter_label = str(inputs.get("chapter_number") or context.get("chapter_number") or "")

        # 缺字段一律 None（「没判」），不回填 0 / [] ——
        # 「0 分 + 空命中」读起来完全像「查过了，没问题」。
        severity_score: Any = None
        hits: Any = None
        recommendations: Any = None
        state = AssessmentState.UNASSESSED
        error = ""

        try:
            # ⚠ X-03：旧实现调的是 `ClicheScanner().scan(content)` ——
            # **ClicheScanner 没有 scan 方法**（真实 API：scan_cliches /
            # scan_by_category / get_critical_count / get_category_stats），
            # 必抛 AttributeError → 被本层 except 吞成一条 warning →
            # severity_score 停在 0.0、节点恒 SUCCESS ⇒
            # Anti-AI 审计**从未响过一次**。
            # ⇒ 改走已有的单一事实源 AntiAIAuditor（章后审计用的就是它），
            # 不在这里另写一套评分口径（否则又是一份「同逻辑两份」）。
            from application.audit.services.anti_ai_audit import AntiAIAuditor

            report = AntiAIAuditor().scan_chapter(
                chapter_id=chapter_label, content=content, novel_id=novel_id
            )
            severity_score = float(report.metrics.severity_score)
            hits = [
                {
                    "pattern": h.pattern,
                    "text": h.text,
                    "start": h.start,
                    "end": h.end,
                    "severity": h.severity,
                    "category": h.category,
                    "replacement_hint": h.replacement_hint,
                }
                for h in report.hits
            ]
            recommendations = list(report.recommendations)
            state = AssessmentState.DECIDED
        except Exception as e:
            error = f"AntiAIAuditor 调用失败: {e}"
            logger.warning(error)

        if state is AssessmentState.UNASSESSED:
            return _unassessed_result(
                {
                    "severity_score": severity_score,
                    "hits": hits,
                    "recommendations": recommendations,
                },
                error,
                int((time.time() - start) * 1000),
            )

        return NodeResult(
            outputs={
                "severity_score": severity_score,
                "hits": hits,
                "recommendations": recommendations,
                "state": state,
            },
            status=NodeStatus.WARNING if severity_score > 50 else NodeStatus.SUCCESS,
            metrics={"severity_score": severity_score},
            duration_ms=int((time.time() - start) * 1000),
        )

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

        # ⚠ X-03：旧实现把异常吞成 warning、计数停在 0、节点恒 SUCCESS。
        # 而 `ForeshadowingRepository` 是**抽象基类**（domain/novel/repositories/），
        # 构造它必然 TypeError；而且 `find_by_novel` 这个方法**根本不存在**
        # （真实入口是 `SqliteForeshadowingRepository.get_by_novel_id`）
        # ⇒ 这一步**每次都失败**。
        # 现在：走 DI 工厂 + 真实仓储；失败 ⇒ 显式「未评估」，不再谎报「0 条待回收」。
        recovered: Any = None
        pending: Any = None
        recovery_rate: Any = None
        state = AssessmentState.UNASSESSED
        error = ""

        try:
            novel_id = inputs.get("novel_id") or context.get("novel_id") or ""
            if not novel_id:
                raise ValueError("缺 novel_id，伏笔雷达未评估")

            from domain.novel.value_objects.foreshadowing import ForeshadowingStatus
            from domain.novel.value_objects.novel_id import NovelId
            from interfaces.api.dependencies import get_foreshadowing_repository

            # ⚠ `get_by_novel_id` 对**不存在的书**返回 None ——
            # 那不是「这本书 0 条伏笔」，是「没有可评估的对象」⇒ 按未评估处理。
            registry = get_foreshadowing_repository().get_by_novel_id(NovelId(novel_id))
            if registry is None:
                raise ValueError(f"小说 {novel_id} 不存在，伏笔雷达未评估")

            items = registry.foreshadowings
            recovered = len([f for f in items if f.status == ForeshadowingStatus.RESOLVED])
            pending = len([f for f in items if f.status == ForeshadowingStatus.PLANTED])
            total = recovered + pending
            # 分母只算「已回收 + 待回收」；ABANDONED（已放弃）不算待回收。
            recovery_rate = (recovered / total * 100) if total > 0 else 0.0
            state = AssessmentState.DECIDED
        except Exception as e:
            error = f"伏笔雷达调用失败: {e}"
            logger.warning(error)

        outputs = {
            "recovered": recovered,
            "pending": pending,
            "recovery_rate": recovery_rate,
        }
        if state is AssessmentState.UNASSESSED:
            return _unassessed_result(outputs, error, int((time.time() - start) * 1000))

        return NodeResult(
            outputs={**outputs, "state": state},
            status=NodeStatus.SUCCESS,
            metrics={"recovery_rate": recovery_rate},
            duration_ms=int((time.time() - start) * 1000),
        )

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
            NodePort(name="novel_id", data_type=PortDataType.TEXT, required=False),
            NodePort(name="chapter_number", data_type=PortDataType.SCORE, required=False),
        ],
        output_ports=[
            # ⚠ X-03：旧端口名（summary / events / triples / causal_edges）宣称输出
            # **内容列表**，而章后管线真实返回的是**布尔标志** ⇒ 端口即 fiction。
            # 改成真实标志位，能力与出口对齐。
            NodePort(name="narrative_sync_ok", data_type=PortDataType.BOOLEAN),
            NodePort(name="triples_extracted", data_type=PortDataType.BOOLEAN),
            NodePort(name="causal_edges_stored", data_type=PortDataType.BOOLEAN),
            NodePort(name="foreshadow_stored", data_type=PortDataType.BOOLEAN),
            NodePort(name="vector_stored", data_type=PortDataType.BOOLEAN),
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

        # ⚠ X-03：旧实现把异常吞成 warning、输出停在空串/空列表、节点恒 SUCCESS。
        # 而 `ChapterAftermathPipeline.run_narrative_sync` **不存在**
        # （真实入口是 run_after_chapter_saved）、且构造函数要 knowledge_service +
        # llm_service ⇒ 这一步**每次都失败**。
        # 现在：走 DI 工厂（生产同一套装配）+ 真实入口；
        # 失败 ⇒ 显式「未评估」，不再谎报「同步完成但没内容」。
        narrative_sync_ok: Any = None
        triples_extracted: Any = None
        causal_edges_stored: Any = None
        foreshadow_stored: Any = None
        vector_stored: Any = None
        state = AssessmentState.UNASSESSED
        error = ""

        try:
            novel_id = inputs.get("novel_id") or context.get("novel_id") or ""
            raw_chapter_number = inputs.get("chapter_number") or context.get("chapter_number") or 0
            try:
                chapter_number = int(raw_chapter_number)
            except (TypeError, ValueError):
                chapter_number = 0

            if not novel_id:
                raise ValueError("缺 novel_id，叙事同步未评估")
            if chapter_number < 1:
                raise ValueError(
                    f"缺真实 chapter_number（拿到 {raw_chapter_number!r}），叙事同步未评估"
                )
            if not str(content).strip():
                raise ValueError("正文为空，叙事同步未评估")

            from interfaces.api.dependencies import get_chapter_aftermath_pipeline

            pipeline = get_chapter_aftermath_pipeline()
            result = await pipeline.run_after_chapter_saved(
                novel_id, chapter_number, content
            )
            # ⚠ 章后管线自己吞掉子步骤异常、只返回**布尔标志** ——
            # `narrative_sync_ok=False` 意味着「没同步成」，不是「同步了但没内容」
            # ⇒ 按未评估处理，不谎报「已完成」。
            if not result.get("narrative_sync_ok"):
                raise ValueError("ChapterAftermathPipeline 叙事同步未成功")

            narrative_sync_ok = True
            triples_extracted = bool(result.get("triples_extracted"))
            causal_edges_stored = bool(result.get("causal_edges_stored"))
            foreshadow_stored = bool(result.get("foreshadow_stored"))
            vector_stored = bool(result.get("vector_stored"))
            state = AssessmentState.DECIDED
        except Exception as e:
            error = f"ChapterAftermathPipeline 调用失败: {e}"
            logger.warning(error)

        outputs = {
            "narrative_sync_ok": narrative_sync_ok,
            "triples_extracted": triples_extracted,
            "causal_edges_stored": causal_edges_stored,
            "foreshadow_stored": foreshadow_stored,
            "vector_stored": vector_stored,
        }
        if state is AssessmentState.UNASSESSED:
            return _unassessed_result(outputs, error, int((time.time() - start) * 1000))

        return NodeResult(
            outputs={**outputs, "state": state},
            status=NodeStatus.SUCCESS,
            duration_ms=int((time.time() - start) * 1000),
        )

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

        # ⚠ X-03：旧实现把异常吞成 warning、三元组停在空列表、节点恒 SUCCESS。
        # 而 `KnowledgeGraphService()` 的构造函数要**三个**仓储
        # （triple_repo / chapter_element_repo / story_node_repo），
        # 且 `infer_from_chapter` 只收**一个** chapter_id
        # （节点传的是 novel_id + chapter_number 两个）⇒ 这一步**每次都失败**。
        # 现在：走 DI 工厂 + 真实仓储 + 正确的 id 口径；
        # 失败 ⇒ 显式「未评估」，不再谎报「没有三元组」。
        inferred_triples: Any = None
        state = AssessmentState.UNASSESSED
        error = ""

        try:
            novel_id = inputs.get("novel_id") or context.get("novel_id") or ""
            raw_chapter_number = inputs.get("chapter_number") or context.get("chapter_number") or 0
            try:
                chapter_number = int(raw_chapter_number)
            except (TypeError, ValueError):
                chapter_number = 0

            if not novel_id:
                raise ValueError("缺 novel_id，知识图谱推断未评估")
            if chapter_number < 1:
                raise ValueError(
                    f"缺真实 chapter_number（拿到 {raw_chapter_number!r}），知识图谱推断未评估"
                )

            from interfaces.api.dependencies import get_knowledge_graph_service

            svc = get_knowledge_graph_service()
            # ⚠ KG 口径：`infer_from_chapter` 收的是 **story_nodes.id**
            # （`chapter_elements.chapter_id` JOIN 的就是它），**不是** `chapters.id`
            # —— 传错 id 会让推断静默查空、报「0 条三元组」。
            story_node_id = svc.resolve_story_node_id(novel_id, chapter_number)
            if not story_node_id:
                raise ValueError(
                    f"小说 {novel_id} 第 {chapter_number} 章没有结构节点（story_nodes），"
                    "知识图谱推断未评估"
                )

            triples = await svc.infer_from_chapter(story_node_id)
            inferred_triples = [
                {
                    "id": t.id,
                    "subject_type": t.subject_type,
                    "subject_id": t.subject_id,
                    "predicate": t.predicate,
                    "object_type": t.object_type,
                    "object_id": t.object_id,
                    "confidence": t.confidence,
                }
                for t in triples
            ]
            state = AssessmentState.DECIDED
        except Exception as e:
            error = f"KnowledgeGraphService 调用失败: {e}"
            logger.warning(error)

        outputs = {"inferred_triples": inferred_triples}
        if state is AssessmentState.UNASSESSED:
            return _unassessed_result(outputs, error, int((time.time() - start) * 1000))

        return NodeResult(
            outputs={**outputs, "state": state},
            status=NodeStatus.SUCCESS,
            duration_ms=int((time.time() - start) * 1000),
        )

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return True
