"""BaseStoryPipeline — 写作管线基类

这是 AIText 引擎的灵魂。一个类，十个步骤，继承即扩展。

完整管线流程（单章生成）：
 1. _step_find_next_chapter(ctx)     定位下一个待写章节
 2. _step_build_context(ctx)         组装上下文（四层洋葱挤压）
 3. _step_prepare_chapter_plan(ctx)  章节执行剧本准备
 4. _step_generate(ctx)              LLM 整章一次生成
 5. _step_validate_content(ctx)      策略验证（反AI/俗套/一致性）
 6. _step_save_chapter(ctx)          保存章节（独立短连接写库）
 7. _step_validate_voice(ctx)        文风审计（声线漂移检测+改写）
 8. _step_run_post_commit(ctx)       章后管线（叙事/向量/KG/伏笔）
 9. _step_score_tension(ctx)         张力打分（0-100 多维评分）
10. _step_finalize(ctx)              收尾（落库+状态推进）
"""
from __future__ import annotations

import logging
import time
from abc import ABC
from typing import Any, Dict, List, Optional

from engine.pipeline.context import PipelineContext, PipelineResult
from engine.pipeline.recovery import StepCommitKind, boundary_for
from engine.pipeline.steps import StepResult
from engine.pipeline.telemetry import story_pipeline_wave_meta

logger = logging.getLogger(__name__)

def _writing_progress(
    ctx: PipelineContext,
    substep: str,
    label: str,
    *,
    pipeline_wave_index: Optional[int] = None,
    **extras: Any,
) -> None:
    """推送到全托管共享状态（writing_substep* + StoryPipeline 波次），供 /status 与 UI 管线图。"""
    sink = getattr(ctx, "writing_progress_sink", None)
    if sink is None:
        return
    payload = {k: v for k, v in extras.items() if v is not None}
    if pipeline_wave_index is not None:
        meta = story_pipeline_wave_meta(int(pipeline_wave_index))
        if meta:
            payload.update(meta)
    try:
        sink(substep, label, payload)
    except Exception as e:
        logger.debug("[%s] writing_progress_sink 失败: %s", getattr(ctx, "novel_id", "?"), e)


class BaseStoryPipeline(ABC):
    """写作管线基类 — 继承即扩展，开箱即用

    可覆写的类属性（调参）：
    - DEFAULT_TARGET_WORDS: 默认目标字数
    - VOICE_REWRITE_THRESHOLD: 声线漂移阈值
    - VOICE_REWRITE_MAX_ATTEMPTS: 定向改写最大轮数
    - MIN_PASS_SCORE: 策略验证最低通过分数
    - BATCH_WRITE_INTERVAL: 保留给兼容运行时的批量写库间隔
    """

    # ─── 可覆写的类属性（调参） ───
    DEFAULT_TARGET_WORDS: int = 2500
    VOICE_REWRITE_THRESHOLD: float = 0.68
    # 章节落库效果确认的轮询预算（秒）；测试可覆盖为小值
    CHAPTER_PERSIST_WAIT_SECONDS: float = 5.0
    VOICE_REWRITE_MAX_ATTEMPTS: int = 3
    VOICE_WARNING_DEFAULT_THRESHOLD: float = 0.75
    MIN_PASS_SCORE: float = 0.6
    BATCH_WRITE_INTERVAL: int = 3
    STREAM_PUSH_INTERVAL: float = 0.15

    def __init__(self):
        """初始化管线。子类可在此设置额外依赖。"""
        self._step_log: List[Dict[str, Any]] = []

    # ═══════════════════════════════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════════════════════════════

    async def run_chapter(self, ctx: PipelineContext) -> PipelineResult:
        """执行完整的章节生成管线

        严格按 _step_* 方法定义顺序调用。子类通常不需要重写此方法，
        而是重写具体的步骤方法。

        Args:
            ctx: 管线上下文，携带所有依赖和输入数据

        Returns:
            PipelineResult: 管线最终输出
        """
        self._step_log = []
        step_status: Dict[str, str] = {}

        try:
            # 1. 定位下一个待写章节
            self._mark_pipeline_step(ctx, "find_next_chapter")
            r = await self._step_find_next_chapter(ctx)
            step_status["find_next_chapter"] = "ok" if r.passed else ("skipped" if r.skip else "failed")
            if not r.passed and not r.skip:
                return self._make_result(ctx, success=False, error=r.message, step_status=step_status)
            if r.passed:
                _writing_progress(
                    ctx,
                    "chapter_found",
                    f"章节定位 · 第 {ctx.chapter_number} 章",
                    pipeline_wave_index=1,
                    current_chapter_number=ctx.chapter_number,
                    chapter_target_words=ctx.target_word_count,
                )

            # 1b. 叙事治理准备：生成章节预算与上下文请求，作为后续上下文组装的硬约束输入。
            self._mark_pipeline_step(ctx, "prepare_governance")
            await self._step_prepare_governance(ctx)

            # 2. 章节执行剧本准备：旧数据直接复用七段，新幕级轻量链条在这里补成七段。
            self._mark_pipeline_step(ctx, "prepare_chapter_plan")
            r = await self._step_prepare_chapter_plan(ctx)
            step_status["prepare_chapter_plan"] = "ok" if r.passed else "failed"
            if not r.passed:
                return self._make_result(ctx, success=False, error=r.message, step_status=step_status)
            _writing_progress(
                ctx,
                "chapter_plan_ready",
                "章节执行剧本准备完成",
                pipeline_wave_index=2,
                current_chapter_number=ctx.chapter_number,
                total_beats=0,
                current_beat_index=0,
                chapter_target_words=ctx.target_word_count,
                chapter_plan_mode=ctx.metadata.get("chapter_plan_mode") or "",
            )

            # 3. 组装上下文（四层洋葱挤压）
            self._mark_pipeline_step(ctx, "build_context")
            r = await self._step_build_context(ctx)
            step_status["build_context"] = "ok" if r.passed else "failed"
            if not r.passed:
                return self._make_result(ctx, success=False, error=r.message, step_status=step_status)
            _writing_progress(
                ctx,
                "context_assembly",
                "组装上下文",
                pipeline_wave_index=3,
                current_chapter_number=ctx.chapter_number,
                context_tokens=int(ctx.context_tokens or 0),
                chapter_target_words=ctx.target_word_count,
            )

            # 4. LLM 生成（整章一次写完）
            self._mark_pipeline_step(ctx, "generate")
            r = await self._step_generate(ctx)
            step_status["generate"] = "ok" if r.passed else "failed"
            if not r.passed:
                return self._make_result(ctx, success=False, error=r.message, step_status=step_status)

            # 5. 策略验证（反AI/俗套/一致性）
            self._mark_pipeline_step(ctx, "validate_content")
            r = await self._step_validate_content(ctx)
            step_status["validate_content"] = "ok" if r.passed else "warning"
            if not r.passed:
                logger.warning(f"[{ctx.novel_id}] 内容验证未通过: {r.message}")
                # 验证失败不阻断管线，记录违规但继续

            # 6. 保存章节（独立短连接写库）
            self._mark_pipeline_step(ctx, "save_chapter")
            r = await self._step_save_chapter(ctx)
            step_status["save_chapter"] = "ok" if r.passed else "failed"
            if not r.passed:
                return self._make_result(ctx, success=False, error=r.message, step_status=step_status)
            self._mark_pipeline_step(ctx, "save_chapter", completed=True)

            # 7. 文风审计（声线漂移检测+改写）
            self._mark_pipeline_step(ctx, "validate_voice")
            r = await self._step_validate_voice(ctx)
            step_status["validate_voice"] = "ok" if r.passed else "warning"
            if not r.passed:
                logger.warning(f"[{ctx.novel_id}] 文风审计未通过: {r.message}")

            # 8. 章后管线（叙事同步/向量索引/KG推断/伏笔/因果边/人物状态/债务）
            self._mark_pipeline_step(ctx, "run_post_commit")
            r = await self._step_run_post_commit(ctx)
            step_status["run_post_commit"] = "ok" if r.passed else "warning"
            if not r.passed:
                logger.warning(f"[{ctx.novel_id}] 章后管线失败: {r.message}")

            # 9. 张力打分（0-100 多维评分）
            self._mark_pipeline_step(ctx, "score_tension")
            r = await self._step_score_tension(ctx)
            step_status["score_tension"] = "ok" if r.passed else "warning"
            if not r.passed:
                logger.warning(f"[{ctx.novel_id}] 张力打分失败: {r.message}")

            # 10. 收尾（落库+状态推进）
            self._mark_pipeline_step(ctx, "finalize")
            r = await self._step_finalize(ctx)
            step_status["finalize"] = "ok" if r.passed else "failed"
            if not r.passed:
                return self._make_result(ctx, success=False, error=r.message, step_status=step_status)

            return self._make_result(ctx, success=True, step_status=step_status)

        except Exception as e:
            logger.error(f"[{ctx.novel_id}] 管线异常: {e}", exc_info=True)
            return self._make_result(ctx, success=False, error=str(e), step_status=step_status)

    # ═══════════════════════════════════════════════════════════════
    # 十大步骤 — 每个都有默认实现，子类可覆写
    # ═══════════════════════════════════════════════════════════════

    async def _step_find_next_chapter(self, ctx: PipelineContext) -> StepResult:
        """步骤1：定位下一个待写章节

        默认实现：查询 story_node_repo，找到下一个未写章节。
        检测余韵章需求（上章张力>=80时触发）。

        子类覆写场景：
        - 短剧引擎跳过缓冲章
        - 推理引擎要求按时间线顺序写
        """
        self._log_step("find_next_chapter", "定位待写章节")

        if ctx.story_node_repo is None:
            return StepResult.fail("story_node_repo 未设置，无法定位章节")

        try:
            nodes = await ctx.story_node_repo.get_by_novel(ctx.novel_id)
            chapter_nodes = sorted(
                [n for n in nodes if getattr(n, 'node_type', None) and n.node_type.value == "chapter"],
                key=lambda n: n.number,
            )
            for node in chapter_nodes:
                # 找到第一个未完成的章节
                existing = None
                if ctx.chapter_repository:
                    try:
                        from domain.novel.value_objects.novel_id import NovelId
                        existing = ctx.chapter_repository.get_by_novel_and_number(
                            NovelId(ctx.novel_id), node.number
                        )
                    except Exception:
                        pass
                if existing is None or getattr(existing, 'status', '') != 'completed':
                    ctx.chapter_node = node
                    ctx.chapter_number = node.number
                    ctx.outline = node.outline or node.description or node.title or ""
                    if existing is not None:
                        old_draft = str(getattr(existing, "content", "") or "").strip()
                        if old_draft:
                            ctx.metadata["ignored_existing_draft_chars"] = len(old_draft)
                        ctx.existing_content = ""
                    try:
                        from domain.novel.value_objects.novel_id import NovelId

                        novel = (
                            ctx.novel_repository.get_by_id(NovelId(ctx.novel_id))
                            if ctx.novel_repository is not None
                            else None
                        )
                        ctx.start_beat_index = int(getattr(novel, "current_beat_index", 0) or 0) if novel else 0
                    except Exception:
                        ctx.start_beat_index = 0
                    return StepResult.ok(f"定位到第 {node.number} 章")

            return StepResult.fail("所有章节已写完，无需继续")
        except Exception as e:
            return StepResult.fail(f"章节定位失败: {e}")

    async def _step_prepare_governance(self, ctx: PipelineContext) -> StepResult:
        """步骤1b：从叙事治理层领取本章预算。

        失败不阻断生成；治理报告会在章后提交闸门再次落库。
        """
        try:
            from application.governance.service import NarrativeGovernanceService
            from infrastructure.persistence.database.connection import get_database
            from infrastructure.persistence.database.sqlite_governance_repository import (
                SqliteGovernanceRepository,
            )
            from infrastructure.persistence.database.sqlite_storyline_repository import (
                SqliteStorylineRepository,
            )

            db = get_database()
            governance = NarrativeGovernanceService(
                SqliteGovernanceRepository(db),
                ctx.novel_repository,
                SqliteStorylineRepository(db),
                db,
            )
            prepared = governance.prepare_chapter(ctx.novel_id, ctx.chapter_number)
            ctx.governance_budget = prepared.get("budget") or {}
            ctx.governance_context_request = prepared.get("context_request") or {}
            ctx.metadata["governance_budget"] = ctx.governance_budget
            ctx.metadata["governance_context_request"] = ctx.governance_context_request
            try:
                from application.evolution.services.gate_service import EvolutionGateService
                from infrastructure.persistence.database.sqlite_evolution_repository import (
                    SqliteEvolutionRepository,
                )

                continuity = EvolutionGateService(SqliteEvolutionRepository(db)).check(
                    novel_id=ctx.novel_id,
                    chapter_number=ctx.chapter_number,
                    branch_id="main",
                    outline_content=ctx.outline or "",
                )
                ctx.evolution_continuity_report = continuity.to_dict()
                ctx.metadata["evolution_continuity_report"] = ctx.evolution_continuity_report
                if not continuity.is_pass:
                    return StepResult.fail(
                        "写前连续性检查未通过：" + "；".join(continuity.repair_plan[:3])
                    )
            except Exception as gate_error:
                logger.warning("[%s] 写前连续性检查失败: %s", ctx.novel_id, gate_error)
            _writing_progress(
                ctx,
                "governance_prepare",
                "叙事预算与连续性",
                pipeline_wave_index=1,
                current_chapter_number=ctx.chapter_number,
                governance_budget=ctx.governance_budget,
                evolution_continuity_report=ctx.evolution_continuity_report,
            )
            return StepResult.ok("叙事治理预算已准备")
        except Exception as e:
            logger.warning("[%s] 叙事治理预算准备失败: %s", ctx.novel_id, e)
            return StepResult.skip_step(f"叙事治理预算准备失败: {e}")

    async def _step_build_context(self, ctx: PipelineContext) -> StepResult:
        """步骤2：组装上下文（四层洋葱挤压）

        默认实现：优先委托给 chapter_workflow，降级到 context_builder。
        T0(强制)→T1(可压缩)→T2(动态水位线)→T3(可牺牲/向量召回)

        子类覆写场景：
        - 短剧引擎：注入"3分钟反转"规则
        - 推理引擎：注入"线索公平性"规则
        - 武侠引擎：注入修炼体系设定
        """
        self._log_step("build_context", f"组装上下文，目标 {ctx.target_word_count} 字")

        bundle = None

        # 优先使用 chapter_workflow（完整 bundle）
        if ctx.chapter_workflow is not None:
            try:
                bundle = ctx.chapter_workflow.prepare_chapter_generation(
                    ctx.novel_id, ctx.chapter_number, ctx.outline, scene_director=None
                )
                ctx.context_text = bundle.get("context", "")
                ctx.context_tokens = bundle.get("context_tokens", 0)
                ctx.voice_anchors = bundle.get("voice_anchors", "")
                ctx.bundle = bundle
                logger.info(
                    f"[{ctx.novel_id}] 上下文（workflow）: {len(ctx.context_text)} 字符, "
                    f"约 {ctx.context_tokens} tokens"
                )
            except Exception as e:
                logger.warning(f"chapter_workflow 准备失败，降级到 context_builder: {e}")
                bundle = None

        # 降级到 context_builder
        if bundle is None and ctx.context_builder is not None:
            try:
                ctx.context_text = ctx.context_builder.build_context(
                    novel_id=ctx.novel_id,
                    chapter_number=ctx.chapter_number,
                    outline=ctx.outline,
                    max_tokens=20000,
                )
                logger.info(f"[{ctx.novel_id}] 上下文（builder）: {len(ctx.context_text)} 字符")
            except Exception as e:
                logger.warning(f"context_builder 构建失败: {e}")

        # 声线锚点补充
        if not ctx.voice_anchors and ctx.context_builder is not None:
            try:
                ctx.voice_anchors = ctx.context_builder.build_voice_anchor_system_section(ctx.novel_id)
            except Exception:
                ctx.voice_anchors = ""

        # ─── 叙事治理预算（T0 强制层）：总编辑模型给本章的结构边界 ────
        if ctx.governance_budget:
            budget = ctx.governance_budget
            tags = "、".join(budget.get("must_serve_promise_tags") or [])
            notes = "\n".join(f"- {note}" for note in (budget.get("notes") or []))
            lines = [
                "\n\n=== 本章叙事治理预算 ===",
                f"- 最多新增故事线：{budget.get('max_new_storylines', 0)}",
                f"- 最多回收叙事债务：{budget.get('max_debt_closures', 0)}",
                f"- 允许揭秘等级：{budget.get('allowed_reveal_level', 'hint')}",
                "- 世界线规则：优先推进、交汇或回收现有世界线；不要随意开新主线/支线/暗线。",
                "- 新线约束：若预算允许新增，也必须由既有人物、地点、道具或未结因果触发，并在章内给出可追踪的世界切片变化。",
                "- 回滚约束：避免写出会破坏已存档人物状态、地点占用、道具归属和已公开真相的情节。",
            ]
            if tags:
                lines.append(f"- 必须服务承诺标签：{tags}")
            if notes:
                lines.append(notes)
            ctx.context_text = (ctx.context_text or "") + "\n".join(lines)

        # ─── 伏笔主动注入（T0 强制层）：本章应推进/兑现的待回收伏笔 ────
        if ctx.foreshadowing_repository is not None:
            try:
                from domain.novel.value_objects.novel_id import NovelId
                _registry = ctx.foreshadowing_repository.get_by_novel_id(NovelId(ctx.novel_id))
                if _registry and hasattr(_registry, "subtext_entries"):
                    _importance_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
                    _window = ctx.chapter_number + 2
                    _due = [
                        e for e in _registry.subtext_entries
                        if e.status == "pending"
                        and e.suggested_resolve_chapter is not None
                        and e.suggested_resolve_chapter <= _window
                    ]
                    _due.sort(key=lambda e: _importance_rank.get(e.importance, 2), reverse=True)
                    if _due[:3]:
                        _lines = "\n".join(
                            f"- {e.question}（埋于第{e.chapter}章）"
                            for e in _due[:3]
                        )
                        _fshadow_block = f"\n\n=== 本章应推进的伏笔 ===\n{_lines}"
                        ctx.context_text = (ctx.context_text or "") + _fshadow_block
                        logger.info(
                            "[%s] 注入 %d 条待兑现伏笔到生成上下文",
                            ctx.novel_id, len(_due[:3]),
                        )
            except Exception as _fse:
                logger.debug("伏笔注入失败（跳过）: %s", _fse)

        return StepResult.ok()

    async def _step_prepare_chapter_plan(self, ctx: PipelineContext) -> StepResult:
        """步骤3：准备章节执行剧本。

        旧数据若已有七段剧本则直接复用；新幕级规划只提供轻量主事件链，
        本步骤在正文生成前补齐单章七段执行剧本。
        """
        self._log_step("prepare_chapter_plan", "章节执行剧本准备")
        ctx.beats = []

        outline = (ctx.outline or "").strip()
        if not outline:
            return StepResult.fail("章节执行剧本为空，无法整章生成")

        from application.blueprint.services.chapter_planning_policy import has_rendered_chapter_execution_plan

        if has_rendered_chapter_execution_plan(outline):
            ctx.metadata["chapter_plan_mode"] = "full_chapter_script"
            ctx.metadata["outline_plan_mode"] = "full_chapter_script"
        else:
            if ctx.llm_service is None:
                return StepResult.fail("llm_service 未设置，无法生成章前执行剧本")
            preplanning_service = ctx.get_dep("chapter_preplanning_service")
            if preplanning_service is None:
                from application.blueprint.services.chapter_preplanning_service import ChapterPreplanningService

                preplanning_service = ChapterPreplanningService(
                    llm_service=ctx.llm_service,
                    chapter_repository=ctx.chapter_repository,
                    story_node_repo=ctx.story_node_repo,
                )
            try:
                outline = await preplanning_service.ensure_execution_plan(
                    novel_id=ctx.novel_id,
                    chapter_number=ctx.chapter_number,
                    chapter_node=ctx.chapter_node,
                    current_outline=outline,
                    target_words=ctx.target_word_count,
                )
            except Exception as exc:
                return StepResult.fail(f"章前执行剧本生成失败: {exc}")
            ctx.outline = outline
            ctx.metadata["chapter_plan_mode"] = "chapter_preplan"
            ctx.metadata["outline_plan_mode"] = "chapter_preplan"

        logger.info(
            "[%s] 第 %s 章使用整章执行剧本生成，plan_chars=%d target_words=%d",
            ctx.novel_id,
            ctx.chapter_number,
            len(outline),
            ctx.target_word_count,
        )
        self._attach_chapter_preplan_metadata(ctx)
        return StepResult.ok()

    def _attach_chapter_preplan_metadata(self, ctx: PipelineContext) -> None:
        """Expose one structured preplan source to prose generation."""
        node_meta = getattr(ctx.chapter_node, "metadata", {}) if ctx.chapter_node is not None else {}
        if not isinstance(node_meta, dict):
            node_meta = {}
        preplan = node_meta.get("chapter_preplan")
        if not isinstance(preplan, dict):
            preplan = {}

        continuity_context = str(preplan.get("continuity_context") or "").strip()
        if not continuity_context:
            try:
                from application.blueprint.services.chapter_continuity_ledger import ChapterContinuityLedgerService

                continuity_context = ChapterContinuityLedgerService(
                    chapter_repository=ctx.chapter_repository,
                    story_node_repo=ctx.story_node_repo,
                ).build_for_chapter(ctx.novel_id, ctx.chapter_number).to_planning_context_text()
            except Exception:
                continuity_context = ""

        ctx.metadata["continuity_context"] = continuity_context

    async def _step_generate(self, ctx: PipelineContext) -> StepResult:
        composer = ctx.get_dep("prose_composer")
        if composer is not None:
            return await self._step_generate_with_composer(ctx, composer)
        from engine.pipeline.prose_composer import ChapterProseInvocationComposer

        return await self._step_generate_with_composer(ctx, ChapterProseInvocationComposer())

    async def _step_generate_with_composer(self, ctx: PipelineContext, composer: Any) -> StepResult:
        self._log_step("generate", "LLM 生成（整章正文 Composer）")

        if ctx.llm_service is None:
            return StepResult.fail("llm_service 未设置，无法生成")

        from engine.pipeline.prose_composer import ProseCompositionRequest
        from application.engine.services.chapter_generation_workspace import (
            ChapterGenerationWorkspace,
            new_pipeline_run_id,
        )

        workspace = ctx.chapter_generation_workspace or ChapterGenerationWorkspace()
        run_id = ctx.active_pipeline_run_id or str(ctx.metadata.get("active_pipeline_run_id") or new_pipeline_run_id())
        ctx.active_pipeline_run_id = run_id
        ctx.chapter_generation_workspace = workspace

        # 崩溃恢复（A4）：上次流式中断残留的半章 preview 不丢弃，作为本次
        # 续写底稿注入——否则每次生成前 discard 会把已生成的几千字全部
        # 丢弃、整章重付 token。新 run 的 preview 写独立文件，旧的由
        # 成功后的 _discard_generation_workspace 全量清理。
        _resumed_draft = ""
        try:
            prev_preview = workspace.read_latest_preview(ctx.novel_id, ctx.chapter_number)
            if prev_preview and len(prev_preview.strip()) >= 500:
                _resumed_draft = prev_preview.strip()
                logger.info(
                    "[%s] 检测到上次中断的半章底稿（%d 字），本次生成改为续写",
                    ctx.novel_id, len(_resumed_draft),
                )
                ctx.context_text = (
                    (ctx.context_text or "").rstrip()
                    + f"\n\n【续写底稿】本章此前生成中断，已有以下前半章正文（约 {len(_resumed_draft)} 字）。"
                    "请从其结尾处自然续写至全章完成，不要重复已有内容：\n"
                    + _resumed_draft
                )
        except Exception as exc:
            logger.debug("[%s] 读取续写底稿失败（按全新生成处理）: %s", ctx.novel_id, exc)

        workspace.discard(ctx.novel_id, ctx.chapter_number)
        workspace.begin(ctx.novel_id, ctx.chapter_number, run_id)
        ctx.metadata["active_pipeline_run_id"] = run_id
        self._mark_pipeline_step(ctx, "generate")

        _writing_progress(
            ctx,
            "llm_calling",
            "整章正文撰写",
            pipeline_wave_index=4,
            current_chapter_number=ctx.chapter_number,
            total_beats=0,
            current_beat_index=0,
            chapter_target_words=ctx.target_word_count,
            accumulated_words=len((ctx.existing_content or "").strip()),
        )

        def _stream_sink(content: str) -> None:
            workspace.append_preview(ctx.novel_id, ctx.chapter_number, run_id, content)
            self._push_streaming_snapshot(ctx.novel_id, content)
            _writing_progress(
                ctx,
                "llm_calling",
                "整章正文撰写",
                pipeline_wave_index=4,
                current_chapter_number=ctx.chapter_number,
                total_beats=0,
                current_beat_index=0,
                chapter_target_words=ctx.target_word_count,
                accumulated_words=len((content or "").strip()),
            )

        request = ProseCompositionRequest(
            novel_id=ctx.novel_id,
            chapter_number=ctx.chapter_number,
            chapter_title=str(getattr(ctx.chapter_node, "title", "") or ""),
            novel_title=str(ctx.metadata.get("novel_title") or ctx.novel_id),
            genre=ctx.genre,
            outline=ctx.outline,
            context_text=ctx.context_text,
            style_guide=ctx.voice_anchors,
            target_words=ctx.target_word_count,
            auto_approve_mode=ctx.auto_approve_mode,
            metadata=ctx.metadata,
            stream_sink=_stream_sink,
            stop_checker=lambda: self._novel_stream_should_stop(ctx.novel_id),
            host=ctx.get_dep("autopilot_host") or ctx,
            llm_service=ctx.llm_service,
        )
        try:
            result = await composer.compose(request)
        except Exception as exc:
            logger.error("[%s] 整章 Composer 失败，停止本章生成: %s", ctx.novel_id, exc)
            return StepResult.fail(f"整章正文生成失败: {exc}")

        if result.awaiting_review:
            ctx.metadata["awaiting_ai_review"] = True
            ctx.metadata["active_invocation_session_id"] = result.session_id
            return StepResult.fail("awaiting_ai_review")

        if getattr(result, "interrupted", False):
            ctx.generation_interrupted = True
            workspace.discard(ctx.novel_id, ctx.chapter_number, run_id)
            return StepResult.fail("interrupted")

        content = self._post_process_generation(result.content, ctx)
        if not content.strip():
            return StepResult.fail("章节正文生成失败：Composer 未产出有效正文")

        ctx.chapter_content = content
        ctx.word_count = len(content)
        workspace.append_preview(ctx.novel_id, ctx.chapter_number, run_id, content)
        self._push_streaming_snapshot(ctx.novel_id, content)
        return StepResult.ok()

    async def _step_validate_content(self, ctx: PipelineContext) -> StepResult:
        """步骤5：策略验证（反AI/俗套/一致性）

        默认实现：委托给 PolicyValidator，运行六维度检查。
        - 语言风格（八股文/数字比喻/过度理性/拐弯描写）
        - 角色一致性（OOC检测/语言指纹/创伤反应）
        - 情节密度 / 命名规范 / 视角控制 / 叙事节奏

        验证失败不阻断管线，只记录违规。enforce 模式下可短路。

        子类覆写场景：
        - 添加领域特定验证（如武侠的"武功逻辑"验证）
        - 修改验证阈值
        """
        self._log_step("validate_content", "策略验证")

        _writing_progress(
            ctx,
            "policy_validate",
            "策略校验（反AI·一致性）",
            pipeline_wave_index=5,
            current_chapter_number=ctx.chapter_number,
            total_beats=0,
            chapter_target_words=ctx.target_word_count,
            accumulated_words=ctx.word_count,
        )

        if ctx.policy_validator is not None:
            try:
                report = ctx.policy_validator.advise(
                    text=ctx.chapter_content,
                    character_masks=self._get_character_masks(ctx),
                    chapter_goal=ctx.outline,
                    character_names=self._get_character_names(ctx),
                    scene_info=self._build_scene_info(ctx),
                    foreshadows=self._build_foreshadows(ctx),
                    era=ctx.era,
                )
                ctx.validation_passed = report.passed
                ctx.validation_score = report.overall_score
                ctx.validation_violations = report.all_violations
                ctx.validation_dimensions = {
                    "language_style": report.language_style_score,
                    "character_consistency": report.character_consistency_score,
                    "plot_density": report.plot_density_score,
                    "naming": report.naming_score,
                    "viewpoint": report.viewpoint_score,
                    "rhythm": report.rhythm_score,
                }
                return StepResult.ok() if report.passed else StepResult(
                    passed=True,  # 不阻断
                    message=f"验证未通过 (score={report.overall_score:.2f})",
                    score=report.overall_score,
                    violations=report.all_violations,
                )
            except Exception as e:
                logger.warning(f"策略验证异常: {e}")
        else:
            # 无 PolicyValidator，跳过
            ctx.validation_passed = True
            ctx.validation_score = 0.85
            ctx.validation_dimensions = {"language_style": 0.85}

        return StepResult.ok()

    async def _step_save_chapter(self, ctx: PipelineContext) -> StepResult:
        """步骤6：保存章节（独立短连接写库）

        默认实现：优先尝试 CQRS 持久化队列，降级到独立短连接写库。
        这是 Rails 式的"直接写库"——没有 PersistencePort 抽象。

        子类通常不需要覆写此步骤。
        """
        self._log_step("save_chapter", f"保存章节 {ctx.chapter_number}，{ctx.word_count} 字")

        if ctx.chapter_repository is None:
            return StepResult.fail("chapter_repository 未设置，无法保存")

        _writing_progress(
            ctx,
            "chapter_persist",
            "章节落盘",
            pipeline_wave_index=6,
            current_chapter_number=ctx.chapter_number,
            total_beats=0,
            current_beat_index=getattr(ctx, "start_beat_index", 0),
            chapter_target_words=ctx.target_word_count,
            accumulated_words=ctx.word_count,
        )

        try:
            # 尝试推持久化队列
            pushed = self._push_persistence_command(ctx)
            if pushed:
                persisted = self._wait_for_chapter_persistence(ctx)
                if not persisted:
                    # 队列积压/消费者延迟导致超时：独立短连接直写兜底
                    # （同一 payload 的幂等 upsert，队列任务后到无害）
                    await self._save_chapter_via_repository(ctx)
                ctx.chapter_saved = True
                ctx.save_method = "queue"
                self._discard_generation_workspace(ctx)
                return StepResult.ok()

            # 降级：通过 repository 直接写库
            await self._save_chapter_via_repository(ctx)
            ctx.chapter_saved = True
            ctx.save_method = "repository"
            self._discard_generation_workspace(ctx)
            return StepResult.ok()
        except Exception as e:
            return StepResult.fail(f"章节保存失败: {e}")

    def _wait_for_chapter_persistence(self, ctx: PipelineContext) -> bool:
        """轮询等待本章落库生效，返回是否在预算内确认。

        旧实现 wait_until_idle 等的是整个 CQRS 队列排空——多小说并发时
        5 秒预算被其他书的命令摊薄，本章尚未落库就超时走 fallback 直写。
        改为按效果确认（轮询 repository 直到本章 completed 或超时）：
        与 correlation-id ack 等价，且无需跨进程确认通道（push 在守护
        进程、消费者在 API 进程）。
        """
        import time as _time

        deadline = _time.monotonic() + self.CHAPTER_PERSIST_WAIT_SECONDS
        while _time.monotonic() < deadline:
            if self._chapter_completed_in_repository(ctx):
                return True
            _time.sleep(0.1)
        return self._chapter_completed_in_repository(ctx)

    def _chapter_completed_in_repository(self, ctx: PipelineContext) -> bool:
        try:
            from domain.novel.value_objects.novel_id import NovelId

            chapter = ctx.chapter_repository.get_by_novel_and_number(
                NovelId(ctx.novel_id),
                int(ctx.chapter_number),
            )
        except Exception:
            return False
        if chapter is None:
            return False
        status = getattr(chapter, "status", "")
        status_value = status.value if hasattr(status, "value") else str(status)
        return status_value == "completed" and bool(str(getattr(chapter, "content", "") or "").strip())

    async def _step_validate_voice(self, ctx: PipelineContext) -> StepResult:
        """步骤7：文风审计（声线漂移检测+定向改写）

        默认实现：
        1. 声线漂移检测（VoiceDriftService）
        2. 若 drift_alert=True 且 similarity < 阈值，触发定向改写
        3. 最多改写 VOICE_REWRITE_MAX_ATTEMPTS 轮

        子类覆写场景：
        - 短剧引擎：降低声线要求
        - 文学引擎：提高声线要求
        """
        self._log_step("validate_voice", "文风审计")

        _writing_progress(
            ctx,
            "voice_drift_check",
            "文风审计 · 声线漂移",
            pipeline_wave_index=7,
            current_chapter_number=ctx.chapter_number,
            accumulated_words=ctx.word_count,
        )

        if ctx.voice_drift_service is not None:
            try:
                if getattr(ctx.voice_drift_service, "use_llm_mode", False):
                    vr = await ctx.voice_drift_service.score_chapter_async(
                        novel_id=ctx.novel_id,
                        chapter_number=ctx.chapter_number,
                        content=ctx.chapter_content,
                    )
                else:
                    vr = ctx.voice_drift_service.score_chapter(
                        novel_id=ctx.novel_id,
                        chapter_number=ctx.chapter_number,
                        content=ctx.chapter_content,
                    )
                ctx.similarity_score = vr.get("similarity_score")
                ctx.drift_alert = bool(vr.get("drift_alert", False))
                ctx.voice_mode = vr.get("mode", "statistics")

                # 定向改写循环
                if ctx.drift_alert and ctx.similarity_score is not None:
                    if ctx.similarity_score < self.VOICE_REWRITE_THRESHOLD:
                        await self._apply_voice_rewrite_loop(ctx)
            except Exception as e:
                logger.warning(f"文风审计失败: {e}")

        return StepResult.ok()

    async def _step_run_post_commit(self, ctx: PipelineContext) -> StepResult:
        """步骤8：章后管线（叙事同步/向量索引/KG推断/伏笔/因果边/人物状态/债务）

        默认实现：委托给 ChapterAftermathPipeline。
        一次 LLM 调用产出：摘要/事件/埋线/三元组/伏笔/因果边/人物状态突变/叙事债务。

        子类通常不需要覆写——这是引擎的"消化系统"，
        把生成的文字转化为结构化知识存回数据库。
        """
        self._log_step("run_post_commit", "章后管线")

        _writing_progress(
            ctx,
            "chapter_aftermath",
            "章后管线（叙事/向量/伏笔等）",
            pipeline_wave_index=8,
            current_chapter_number=ctx.chapter_number,
            accumulated_words=ctx.word_count,
            aftermath_live_status="running",
            aftermath_live_chapter_number=ctx.chapter_number,
        )

        if ctx.aftermath_pipeline is not None:
            try:
                voice_result = None
                if ctx.similarity_score is not None:
                    voice_result = {
                        "similarity_score": ctx.similarity_score,
                        "drift_alert": ctx.drift_alert,
                        "mode": ctx.voice_mode,
                    }
                result = await ctx.aftermath_pipeline.run_after_chapter_saved(
                    ctx.novel_id,
                    ctx.chapter_number,
                    ctx.chapter_content,
                    voice_result=voice_result,
                )
                ctx.narrative_sync_ok = bool(result.get("narrative_sync_ok", False))
                ctx.vector_stored = bool(result.get("vector_stored", False))
                ctx.foreshadow_stored = bool(result.get("foreshadow_stored", False))
                ctx.triples_extracted = bool(result.get("triples_extracted", False))
                ctx.causal_edges_stored = bool(result.get("causal_edges_stored", False))
                ctx.character_mutations_stored = bool(result.get("character_mutations_stored", False))
                ctx.debt_updated = bool(result.get("debt_updated", False))
                ctx.tension_composite = result.get("tension_composite")
                # 文风结果也可能从 aftermath 返回
                if result.get("similarity_score") is not None and ctx.similarity_score is None:
                    ctx.similarity_score = result["similarity_score"]
                if result.get("drift_alert") and not ctx.drift_alert:
                    ctx.drift_alert = True
                _writing_progress(
                    ctx,
                    "chapter_aftermath_done",
                    "章后管线完成",
                    pipeline_wave_index=8,
                    current_chapter_number=ctx.chapter_number,
                    accumulated_words=ctx.word_count,
                    aftermath_live_status="done",
                    aftermath_live_chapter_number=ctx.chapter_number,
                    narrative_sync_ok=ctx.narrative_sync_ok,
                    vector_stored=ctx.vector_stored,
                    foreshadow_stored=ctx.foreshadow_stored,
                    triples_extracted=ctx.triples_extracted,
                    causal_edges_stored=ctx.causal_edges_stored,
                    character_mutations_stored=ctx.character_mutations_stored,
                    debt_updated=ctx.debt_updated,
                    tension_composite=ctx.tension_composite,
                )
            except Exception as e:
                logger.warning(f"章后管线失败: {e}")
                _writing_progress(
                    ctx,
                    "chapter_aftermath_failed",
                    "章后管线失败",
                    pipeline_wave_index=8,
                    current_chapter_number=ctx.chapter_number,
                    accumulated_words=ctx.word_count,
                    aftermath_live_status="failed",
                    aftermath_live_chapter_number=ctx.chapter_number,
                )

        await self._update_emotion_ledger(ctx)

        return StepResult.ok()

    async def _update_emotion_ledger(self, ctx: PipelineContext) -> None:
        """章后更新 T1 情绪账本（可选，依赖 memory_orchestrator 注入）"""
        memory = ctx.memory_orchestrator
        if memory is None or not ctx.chapter_content:
            return
        try:
            from engine.core.entities.story import StoryId

            updated = await memory.update_emotion_ledger(
                story_id=StoryId(ctx.novel_id),
                chapter_number=ctx.chapter_number,
                chapter_content=ctx.chapter_content,
            )
            ctx.emotion_ledger_updated = updated is not None
        except Exception as e:
            logger.warning(f"情绪账本更新失败: {e}")

    async def _step_score_tension(self, ctx: PipelineContext) -> StepResult:
        """步骤9：张力打分（0-100 多维评分）

        默认实现只接受章后管线的多维张力评分。
        不再用单维短提示词或固定 50 兜底，避免把评估失败伪装成真实心电图数据。

        子类覆写场景：
        - 自定义张力评分逻辑
        - 调整张力维度权重
        """
        self._log_step("score_tension", "张力打分")

        _writing_progress(
            ctx,
            "score_tension_live",
            "张力评估",
            pipeline_wave_index=9,
            current_chapter_number=ctx.chapter_number,
            accumulated_words=ctx.word_count,
        )

        # 优先使用章后管线的多维张力
        if ctx.tension_composite is not None and ctx.tension_composite > 0:
            logger.info(f"[{ctx.novel_id}] 多维张力值：{int(ctx.tension_composite)}/100")
            return StepResult.ok()

        logger.warning(
            "[%s] 张力未评估：章后管线未返回有效多维张力，跳过假分写入",
            ctx.novel_id,
        )
        return StepResult.ok()

    async def _step_finalize(self, ctx: PipelineContext) -> StepResult:
        """步骤10：收尾（落库+状态推进）

        默认实现：
        1. 构建审计快照
        2. 保存 novel 状态到 DB
        3. 推进 current_chapter_in_act / current_act

        子类覆写场景：
        - 自定义状态推进逻辑
        - 添加额外的收尾操作
        """
        self._log_step("finalize", "收尾落库")

        _writing_progress(
            ctx,
            "pipeline_finalize",
            "收尾（状态快照）",
            pipeline_wave_index=10,
            current_chapter_number=ctx.chapter_number,
            accumulated_words=ctx.word_count,
        )

        # 构建审计快照
        ctx.audit_snapshot = {
            "drift_alert": ctx.drift_alert,
            "similarity_score": ctx.similarity_score,
            "narrative_sync_ok": ctx.narrative_sync_ok,
            "vector_stored": ctx.vector_stored,
            "foreshadow_stored": ctx.foreshadow_stored,
            "triples_extracted": ctx.triples_extracted,
            "causal_edges_stored": ctx.causal_edges_stored,
            "character_mutations_stored": ctx.character_mutations_stored,
            "debt_updated": ctx.debt_updated,
            "validation_score": ctx.validation_score,
            "validation_passed": ctx.validation_passed,
        }

        # 章完成自动 checkpoint（A4 接线）：CheckpointManager 文档承诺的行为
        # 此前只被手动 API 调用，管线从不触发。失败不阻断 finalize
        #（checkpoint 是恢复增强，不是主链路）。
        await self._create_chapter_checkpoint(ctx)

        return StepResult.ok()

    async def _create_chapter_checkpoint(self, ctx: PipelineContext) -> None:
        if not ctx.chapter_saved:
            return
        try:
            from engine.infrastructure.persistence.checkpoint_store import CheckpointStore
            from engine.runtime.checkpoint_manager.manager import CheckpointManager
            from infrastructure.persistence.database.connection import get_database

            manager = CheckpointManager(CheckpointStore(get_database()))
            await manager.on_chapter_completed(
                story_id=str(ctx.novel_id),
                chapter_number=int(ctx.chapter_number),
                story_state={
                    "word_count": ctx.word_count,
                    "tension_composite": ctx.tension_composite,
                    "chapter_number": int(ctx.chapter_number),
                    "beat_index": getattr(ctx, "start_beat_index", 0) or 0,
                },
                character_masks={},
                emotion_ledger={},
                active_foreshadows=[],
                outline=(ctx.outline or "")[:2000],
                recent_summary=(ctx.chapter_content or "")[-500:],
            )
        except Exception as e:
            logger.debug(
                "[%s] 章 checkpoint 创建失败（不阻断 finalize）: %s",
                ctx.novel_id, e,
            )

    # ═══════════════════════════════════════════════════════════════
    # 辅助方法（protected，子类可覆写）
    # ═══════════════════════════════════════════════════════════════

    def _post_process_generation(self, content: str, ctx: PipelineContext) -> str:
        """生成后处理 — 子类可覆写以添加后处理逻辑"""
        try:
            from application.ai.llm_output_sanitize import strip_reasoning_artifacts
            from application.ai.prose_fragment_aggregator import aggregate_inline_prose_fragments
            from domain.novel.value_objects.novel_id import NovelId

            s = strip_reasoning_artifacts(content)
            enabled = False
            try:
                if ctx.novel_id and ctx.novel_repository is not None:
                    nov = ctx.novel_repository.get_by_id(NovelId(ctx.novel_id))
                    if nov is not None:
                        enabled = bool(nov.generation_prefs.inline_prose_aggregation_enabled)
            except Exception:
                enabled = False
            if enabled:
                return aggregate_inline_prose_fragments(s)
            return s
        except ImportError:
            return content

    def _push_streaming_snapshot(self, novel_id: str, content: str) -> None:
        """推送整章累积快照到 StreamingBus，供 /autopilot/.../chapter-stream 消费。"""
        if not novel_id or not content:
            return
        try:
            from application.engine.services.streaming_bus import streaming_bus

            streaming_bus.publish(novel_id, content=content)
        except Exception as e:
            logger.debug("[%s] streaming_bus.publish 失败: %s", novel_id, e)

    def _discard_generation_workspace(self, ctx: PipelineContext) -> None:
        workspace = getattr(ctx, "chapter_generation_workspace", None)
        if workspace is None:
            return
        try:
            # 全量清理（不传 run_id）：把历史崩溃残留的半章 preview 一并清掉，
            # 防止 data/workspace 下孤儿文件无限累积
            workspace.discard(ctx.novel_id, ctx.chapter_number)
        except Exception as e:
            logger.debug("[%s] workspace discard failed: %s", ctx.novel_id, e)

    def _mark_pipeline_step(self, ctx: PipelineContext, step_name: str, *, completed: bool = False) -> None:
        boundary = boundary_for(step_name)
        payload: Dict[str, Any] = {
            "active_pipeline_step": "" if completed else step_name,
            "active_pipeline_run_id": getattr(ctx, "active_pipeline_run_id", "") or "",
        }
        if completed and boundary.commit_kind == StepCommitKind.STABLE:
            payload["current_stage"] = boundary.stable_stage
            payload["last_stable_stage"] = boundary.stable_stage
        elif boundary.stable_stage == "writing":
            payload["current_stage"] = "writing"
        host = ctx.get_dep("autopilot_host")
        try:
            if host is not None and hasattr(host, "_update_shared_state"):
                host._update_shared_state(ctx.novel_id, **payload)
        except Exception:
            pass
        try:
            if host is not None and hasattr(host, "_patch_novel_ephemeral") and ctx.novel_id:
                from domain.novel.value_objects.novel_id import NovelId

                host._patch_novel_ephemeral(NovelId(ctx.novel_id), payload)
        except Exception:
            pass

    def _novel_stream_should_stop(self, novel_id: str) -> bool:
        """与 legacy 写作一致：IPC 停止信号 + 控制队列消费。"""
        try:
            from application.engine.services.novel_stop_signal import is_novel_stopped

            if is_novel_stopped(novel_id):
                return True
        except Exception:
            pass
        try:
            from application.engine.services.streaming_bus import streaming_bus

            streaming_bus.consume_control_signals(novel_id)
            from application.engine.services.novel_stop_signal import is_novel_stopped

            return is_novel_stopped(novel_id)
        except Exception:
            return False

    async def _stream_prose_llm(self, ctx: PipelineContext, prompt: Any, config: Any) -> str:
        """Stream a single prose call and publish cumulative snapshots.

        Kept as the small primitive behind whole-chapter composition tests and
        emergency subclasses; production StoryPipeline normally enters through
        a ProseComposer.
        """
        if ctx.llm_service is None:
            return ""
        content_parts: List[str] = []
        last_push = time.monotonic()

        def _maybe_push(*, force: bool = False) -> None:
            nonlocal last_push
            now = time.monotonic()
            if not force and (now - last_push) < self.STREAM_PUSH_INTERVAL:
                return
            content = "".join(content_parts)
            if content:
                self._push_streaming_snapshot(ctx.novel_id, content)
            last_push = now

        async for piece in ctx.llm_service.stream_generate(prompt, config):
            if self._novel_stream_should_stop(ctx.novel_id):
                return ""
            if not piece:
                continue
            content_parts.append(piece)
            _maybe_push()
        _maybe_push(force=True)
        return "".join(content_parts)

    def _get_character_masks(self, ctx: PipelineContext) -> Dict[str, Any]:
        """获取当前章节的角色面具（供策略验证使用）。

        复用 guardrail_execution._character_masks_for_novel（API 路径已在用的实现），
        让角色一致性维度不再恒短路为常量。

        适配器区分两种空态：None = 取数失败（适配器已记 warning），{} = 本书 cast
        确实为空。本方法把两者都压成 {}，因为护栏对二者的处理完全一致（退回常量分）；
        可观测性由适配器那条 warning 承担。
        """
        if not ctx.novel_id:
            return {}
        try:
            from application.engine.services.guardrail_execution import (
                _character_masks_for_novel,
            )

            masks = _character_masks_for_novel(ctx.novel_id)
            return masks if masks is not None else {}
        except Exception as e:
            logger.warning("[%s] 角色面具取数失败，质量分将退回常量: %s", ctx.novel_id, e)
            return {}

    def _get_character_names(self, ctx: PipelineContext) -> List[str]:
        """获取本章实际登场的角色名（供命名校验使用）。

        复用 guardrail_execution._character_names_fallback_from_text，只统计正文里
        出现的角色，避免把未登场角色送进命名校验。

        已知限制：该适配器对三种情况都返回 None——取数失败、本书无 cast、正文无角色
        登场——本方法一律压成 []，三者不可区分，命名维度会退回常量分。
        其中取数失败已由适配器记 warning，可观测。
        """
        if not ctx.novel_id:
            return []
        try:
            from application.engine.services.guardrail_execution import (
                _character_names_fallback_from_text,
            )

            names = _character_names_fallback_from_text(
                ctx.novel_id, ctx.chapter_content or ""
            )
            return list(names) if names else []
        except Exception as e:
            logger.warning("[%s] 角色名取数失败，命名分将退回常量: %s", ctx.novel_id, e)
            return []

    def _get_scene_context(self, ctx: PipelineContext) -> Optional[Dict[str, Any]]:
        """获取最小视点语境（供视角校验使用）。

        复用 guardrail_execution._minimal_scene_context（API 路径已在用的实现）。
        它要求同框登场角色 ≥ 2 人，否则返回 None（取数失败或 cast 为空同样返回 None）。

        注意：本管线里"返回 None"的真实后果不是"退回常量分"。_build_scene_info 还有一个
        角色名回退，能在 cast 推断失败时捞回正文里出现过的角色并组装出真值 dict，
        quality_guardrail 见到真值 dict 就认为视角可评估，无违规时报满分 1.0。
        真正的保守分只发生在 scene_info is None 时，因此"同框 < 2 人"必须由
        _build_scene_info 一起兜住（本方法单独返回 None 并不足够）。
        """
        if not ctx.novel_id:
            return None
        try:
            from application.engine.services.guardrail_execution import (
                _minimal_scene_context,
            )

            return _minimal_scene_context(ctx.novel_id, ctx.chapter_content or "")
        except Exception as e:
            logger.warning("[%s] 视点语境取数失败，视角分将退回常量: %s", ctx.novel_id, e)
            return None

    def _build_foreshadows(self, ctx: PipelineContext) -> List[Dict[str, Any]]:
        """取当前伏笔，供 viewpoint_guardrail 的绑定层级校验。

        只取未回收的伏笔——已回收的不再需要校验绑定。
        取数失败一律降级为空列表：校验是观测性的，不能因此让整章失败。
        但取数失败必须可见——否则护栏静默失效，与 _get_character_masks 等同类取数一致用 warning。
        """
        repo = getattr(ctx, "foreshadowing_repository", None)
        if repo is None or not ctx.novel_id:
            return []
        try:
            from domain.novel.value_objects.novel_id import NovelId

            registry = repo.get_by_novel_id(NovelId(ctx.novel_id))
            if registry is None:
                return []
            entries = registry.get_unresolved()
        except Exception as e:
            logger.warning("[%s] 伏笔注册表取数失败，伏笔绑定校验将静默跳过: %s", ctx.novel_id, e)
            return []

        out: List[Dict[str, Any]] = []
        for entry in entries or []:
            try:
                weight = getattr(entry, "emotional_weight", None)
                level = getattr(entry, "binding_level", None)
                out.append({
                    "description": getattr(entry, "description", "") or "",
                    # None 才回落默认值：0.0 是合法权重，不能被 `or` 吞掉
                    "emotional_weight": 0.5 if weight is None else float(weight),
                    "binding_level": 1 if level is None else int(level),
                })
            except (TypeError, ValueError):
                continue
        return out

    def _build_scene_info(self, ctx: PipelineContext) -> Optional[Dict[str, Any]]:
        """组装视角校验所需的场景信息。

        以 _get_scene_context 的 cast 推断结果为底（viewpoint_character /
        characters_present），并补上它恒为空的 key_event——用本章大纲填充，
        否则视角校验的关键事件判据拿不到东西。

        cast 与正文角色名都取不到时返回 None：None 是"视角无法评估、保持保守分"的
        既定契约（见 quality_guardrail.check 的 vp_score 默认 0.77）。返回一个空壳
        dict 会让 quality_guardrail 认为视角可评估，在无违规时报出满分 1.0。
        伏笔绑定层级校验不依赖 scene_info，已在 quality_guardrail 侧独立门控。

        同框不足 2 人同样返回 None：视角两条判据都以 viewpoint_character 为前置，
        只剩 1 人时 viewpoint_character 必为空，判据双双早退 → 无违规 → 报 1.0，
        比"没有数据"更不可信。这与 guardrail_execution._minimal_scene_context 的契约一致。

        契约要求 viewpoint_guardrail.check 的
        {viewpoint_character, characters_present, key_event, information_asymmetry}。
        """
        scene = self._get_scene_context(ctx) or {}
        characters_present = scene.get("characters_present") or self._get_character_names(ctx)
        if not characters_present or len(characters_present) < 2:
            return None
        return {
            "viewpoint_character": scene.get("viewpoint_character", ""),
            "characters_present": characters_present,
            "key_event": scene.get("key_event") or ctx.outline or "",
            "information_asymmetry": scene.get("information_asymmetry", {}),
        }

    def _push_persistence_command(self, ctx: PipelineContext) -> bool:
        """推送持久化命令到 CQRS 队列"""
        try:
            from application.engine.services.persistence_queue import get_persistence_queue
            pq = get_persistence_queue()
            return pq.push("upsert_chapter", {
                "chapter_id": f"autopilot:{ctx.novel_id}:{ctx.chapter_number}",
                "novel_id": ctx.novel_id,
                "chapter_number": ctx.chapter_number,
                "title": str(getattr(ctx.chapter_node, "title", "") or f"第{ctx.chapter_number}章"),
                "outline": ctx.outline or "",
                "content": ctx.chapter_content,
                "word_count": ctx.word_count,
                "status": "completed",
            })
        except Exception:
            return False

    async def _save_chapter_via_repository(self, ctx: PipelineContext) -> None:
        """通过 repository 保存章节"""
        from domain.novel.entities.chapter import Chapter, ChapterStatus
        from domain.novel.value_objects.novel_id import NovelId

        novel_id = NovelId(ctx.novel_id)
        existing = ctx.chapter_repository.get_by_novel_and_number(novel_id, int(ctx.chapter_number))
        if existing is not None:
            existing.update_content(ctx.chapter_content)
            existing.status = ChapterStatus.COMPLETED
            if ctx.outline:
                existing.outline = ctx.outline
            if getattr(ctx.chapter_node, "title", None):
                existing.title = str(ctx.chapter_node.title)
            ctx.chapter_repository.save(existing)
            return

        chapter = Chapter(
            id=f"autopilot:{ctx.novel_id}:{int(ctx.chapter_number)}",
            novel_id=novel_id,
            number=int(ctx.chapter_number),
            title=str(getattr(ctx.chapter_node, "title", "") or f"第{ctx.chapter_number}章"),
            content=ctx.chapter_content,
            outline=ctx.outline or "",
            status=ChapterStatus.COMPLETED,
        )
        ctx.chapter_repository.save(chapter)

    async def _apply_voice_rewrite_loop(self, ctx: PipelineContext) -> None:
        """声线漂移定向改写循环（真实改写，复用 CPMS VOICE_REWRITE 节点）。

        历史缺陷：此方法曾只置 rewrite_applied=True 而不改任何内容——声线
        严重漂移的章节「改写」后一字未变，审计快照与前端都误以为修过文。
        现在与 legacy 路径（daemon_host._apply_voice_rewrite_loop）同语义：
        有限次改写 + 即时复评，改写失败或无变化时如实记录 attempts。
        """
        from application.engine.services.style_constraint_builder import build_style_summary

        style_summary = ""
        try:
            fingerprint_repo = getattr(ctx.voice_drift_service, "fingerprint_repo", None)
            if fingerprint_repo:
                fingerprint = fingerprint_repo.get_by_novel(ctx.novel_id, None)
                style_summary = build_style_summary(fingerprint)
        except Exception as e:
            logger.debug("[%s] 改写循环取风格指纹失败: %s", ctx.novel_id, e)

        style_block = style_summary.strip() or "暂无明确统计摘要，优先保持既有作者语气与句式节奏。"
        outline = (ctx.outline or "").strip() or "无单独大纲，必须严格保留现有剧情事实。"

        # 首轮由调用方保证 drift_alert 且 similarity < 阈值；后续轮看复评结果
        for attempt in range(1, self.VOICE_REWRITE_MAX_ATTEMPTS + 1):
            variables = {
                "style_fingerprint": style_block,
                "anchor_block": "无额外角色声线锚点。",
                "chapter_number": str(ctx.chapter_number),
                "attempt": str(attempt),
                "similarity_score": f"{float(ctx.similarity_score or 0.0):.4f}",
                "threshold": f"{self.VOICE_REWRITE_THRESHOLD:.2f}",
                "outline": outline,
                "content": ctx.chapter_content,
            }
            rewritten_text = await self._invoke_voice_rewrite(ctx, variables)
            ctx.rewrite_attempts = attempt
            if not rewritten_text or rewritten_text.strip() == ctx.chapter_content.strip():
                logger.warning(
                    "[%s] 定向修文未产生有效变化，停止继续重试（attempt=%d）",
                    ctx.novel_id, attempt,
                )
                break

            ctx.chapter_content = rewritten_text
            ctx.rewrite_applied = True
            # 即时复评：达标则停止循环
            try:
                vr = ctx.voice_drift_service.score_chapter(
                    novel_id=ctx.novel_id,
                    chapter_number=ctx.chapter_number,
                    content=ctx.chapter_content,
                )
                ctx.similarity_score = vr.get("similarity_score")
                ctx.drift_alert = bool(vr.get("drift_alert", False))
            except Exception as e:
                logger.warning("[%s] 改写后复评失败: %s", ctx.novel_id, e)
                break
            if not (
                ctx.drift_alert
                and (ctx.similarity_score or 0) < self.VOICE_REWRITE_THRESHOLD
            ):
                break

    async def _invoke_voice_rewrite(self, ctx: PipelineContext, variables: dict) -> Optional[str]:
        """执行一次 VOICE_REWRITE 调用，返回改写后正文（失败返回 None）。"""
        from application.ai_invocation.autopilot.factory import get_or_create_autopilot_helper_invoker
        from application.ai_invocation.autopilot.helper_invoker import AutopilotHelperRequest
        from application.ai.llm_output_sanitize import strip_reasoning_artifacts
        from infrastructure.ai.prompt_keys import VOICE_REWRITE

        content = variables.get("content", "")
        max_tokens = max(4096, min(8192, int(len(content) * 1.5)))
        try:
            owner = type("PipelineVoiceRewriteOwner", (), {"llm_service": ctx.llm_service})()
            rewritten_text = await get_or_create_autopilot_helper_invoker(owner).invoke_text(
                AutopilotHelperRequest(
                    novel_id=str(ctx.novel_id or "global"),
                    stage="audit",
                    operation="autopilot.voice.rewrite",
                    node_key=VOICE_REWRITE,
                    explicit_variables=variables,
                    context={
                        "novel_id": str(ctx.novel_id or "global"),
                        "chapter_number": ctx.chapter_number,
                    },
                    metadata={"source": "story_pipeline.voice_rewrite"},
                    config={"max_tokens": max_tokens, "temperature": 0.35},
                )
            )
        except Exception as e:
            logger.warning("[%s] 文风定向修文调用失败: %s", ctx.novel_id, e)
            return None
        return strip_reasoning_artifacts((rewritten_text or "").strip()) or None

    async def _score_tension_via_llm(self, ctx: PipelineContext) -> Optional[int]:
        """通过 AI Invocation 评分。

        保留给子类/实验路径显式调用；默认管线不再把它作为兜底。
        """
        import re

        try:
            from application.ai_invocation.autopilot.factory import get_or_create_autopilot_helper_invoker
            from application.ai_invocation.autopilot.helper_invoker import AutopilotHelperRequest
            from infrastructure.ai.prompt_keys import TENSION_SCORING

            owner = type("PipelineTensionInvocationOwner", (), {"llm_service": ctx.llm_service})()
            content = await get_or_create_autopilot_helper_invoker(owner).invoke_text(
                AutopilotHelperRequest(
                    novel_id=str(ctx.novel_id or "global"),
                    stage="audit",
                    operation="autopilot.tension.score",
                    node_key=TENSION_SCORING,
                    explicit_variables={"content": ctx.chapter_content[:2000]},
                    context={
                        "novel_id": ctx.novel_id,
                        "chapter_number": ctx.chapter_number,
                    },
                    metadata={"source": "story_pipeline.tension_score"},
                    config={"max_tokens": 10, "temperature": 0.3},
                )
            )
        except Exception as e:
            logger.warning("[%s] 单维张力评分失败: %s", ctx.novel_id, e)
            return None
        match = re.search(r"(\d+)", str(content))
        if not match:
            # 解析失败不伪造中性分：与 _step_score_tension 的契约一致
            #（「不再伪装成真实数据」），缺分由调用方决定是否走统计兜底
            logger.warning("[%s] 单维张力评分输出无法解析: %r", ctx.novel_id, str(content)[:80])
            return None
        score = int(match.group(1))
        return min(score, 10) * 10  # 1-10 → 0-100

    def _make_result(
        self,
        ctx: PipelineContext,
        success: bool,
        error: Optional[str] = None,
        step_status: Optional[Dict[str, str]] = None,
    ) -> PipelineResult:
        """从上下文构建管线结果"""
        tension = 0
        if ctx.tension_composite is not None:
            tension = int(ctx.tension_composite)

        return PipelineResult(
            success=success,
            chapter_number=ctx.chapter_number,
            content=ctx.chapter_content,
            word_count=ctx.word_count,
            tension=tension,
            drift_alert=ctx.drift_alert,
            similarity_score=ctx.similarity_score,
            validation_score=ctx.validation_score,
            validation_passed=ctx.validation_passed,
            narrative_sync_ok=ctx.narrative_sync_ok,
            error=error,
            audit_snapshot=getattr(ctx, 'audit_snapshot', {}),
            step_status=step_status or {},
        )

    def _log_step(self, step_name: str, message: str) -> None:
        """记录步骤执行日志"""
        self._step_log.append({"step": step_name, "message": message})
        logger.debug(f"[Pipeline] {step_name}: {message}")

    def get_step_log(self) -> List[Dict[str, Any]]:
        """获取步骤执行日志（调试用）"""
        return self._step_log.copy()
