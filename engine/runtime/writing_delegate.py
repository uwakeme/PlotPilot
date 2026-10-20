"""StoryPipeline 写作阶段委托

环境变量 PLOTPILOT_USE_STORY_PIPELINE:
  - 未设置（默认）/ writing / 1 / true: StoryPipeline 写作
  - full / all / engine: StoryPipeline 写作（与 writing 等价）
  - off / legacy / false / 0: legacy 节拍写作（紧急回退）

生产入口统一为 EngineDaemon（Phase 9）。
"""
from __future__ import annotations

import logging
import time
import hashlib
from engine.runtime.protocols import DaemonHostProtocol
from typing import Any, Dict, Literal

from infrastructure.engine.story_pipeline_environment import (
    STORY_PIPELINE_MODE_ENV,
    StoryPipelineEnvironmentSettings,
)

logger = logging.getLogger(__name__)

PipelineMode = Literal["off", "writing", "full"]


def get_story_pipeline_mode() -> PipelineMode:
    """解析引擎内核模式（未设置时默认 writing，Phase 4）"""
    settings = StoryPipelineEnvironmentSettings.from_env()
    if settings.is_unknown:
        logger.warning(
            "未知 %s=%r，使用默认 writing；回退 legacy 请设 off",
            STORY_PIPELINE_MODE_ENV,
            settings.raw_mode,
        )
    return settings.mode


def story_pipeline_mode_was_unset() -> bool:
    return StoryPipelineEnvironmentSettings.from_env().is_unset


def is_story_pipeline_writing_enabled() -> bool:
    """写作阶段是否走新内核（4a 或 4b）"""
    return get_story_pipeline_mode() in ("writing", "full")


def _build_runner(daemon: Any):
    """构建（并按 daemon 实例缓存）StoryPipelineRunner。

    runner 只是把 daemon 启动时注入的固定依赖打包，每章重建纯属浪费；
    daemon 属性（planning_service 等）在运行期不变，缓存安全。
    """
    cached = daemon.__dict__.get("_cached_story_runner")
    if cached is not None:
        return cached

    from engine.runtime.runner import StoryPipelineRunner

    runner = StoryPipelineRunner(
        novel_repository=daemon.novel_repository,
        llm_service=daemon.llm_service,
        context_builder=daemon.context_builder,
        background_task_service=daemon.background_task_service,
        planning_service=daemon.planning_service,
        story_node_repo=daemon.story_node_repo,
        chapter_repository=daemon.chapter_repository,
        poll_interval=daemon.poll_interval,
        voice_drift_service=daemon.voice_drift_service,
        circuit_breaker=daemon.circuit_breaker,
        chapter_workflow=daemon.chapter_workflow,
        aftermath_pipeline=daemon.aftermath_pipeline,
        volume_summary_service=daemon.volume_summary_service,
        foreshadowing_repository=daemon.foreshadowing_repository,
        knowledge_service=daemon.knowledge_service,
    )
    daemon._cached_story_runner = runner
    return runner


async def run_writing(host: "DaemonHostProtocol", novel: Any) -> None:
    """写作阶段统一入口 — 按 host 配置或环境变量选择新/旧管线"""
    if getattr(host, "use_story_pipeline_for_writing", False):
        await run_story_pipeline_writing(host, novel)
        return
    from engine.runtime.legacy_writing_delegate import run_legacy_writing

    await run_legacy_writing(host, novel)


async def run_story_pipeline_writing(daemon: Any, novel: Any) -> None:
    """执行单章写作（新管线），并同步 novel 状态到 daemon 模型"""
    from domain.novel.entities.novel import NovelStage
    from engine.pipelines.registry import get_pipeline_registry

    novel_id = novel.novel_id.value if hasattr(novel.novel_id, "value") else str(novel.novel_id)
    runner = _build_runner(daemon)
    target_words = int(getattr(novel, "target_words_per_chapter", None) or runner.DEFAULT_TARGET_WORDS)
    genre = (getattr(novel, "genre", "") or "").strip().lower()

    logger.info("[%s] StoryPipeline 写作模式 genre=%s", novel_id, genre or "(default)")

    def _writing_sink(substep: str, label: str, extra: Dict[str, Any]) -> None:
        merged = dict(extra)
        nw = merged.get("story_pipeline_wave_index")
        try:
            import sys

            shared = sys.modules.get("__shared_state")
            if shared is not None and nw is not None:
                nk = f"novel:{novel_id}"
                prev = dict(shared.get(nk, {}))
                pw = prev.get("story_pipeline_wave_index")
                if pw != nw:
                    merged["story_pipeline_wave_entered_at"] = time.time()
                elif "story_pipeline_wave_entered_at" not in merged and prev.get("story_pipeline_wave_entered_at"):
                    merged["story_pipeline_wave_entered_at"] = prev["story_pipeline_wave_entered_at"]
                # 新的一章开篇：清零事件轨迹
                if substep == "chapter_found" and int(nw) == 1:
                    merged["story_pipeline_events"] = [
                        {
                            "t": time.time(),
                            "wave": nw,
                            "wave_id": merged.get("story_pipeline_wave_id"),
                            "substep": substep,
                            "label": label,
                        }
                    ]
                else:
                    ev = list(prev.get("story_pipeline_events") or [])
                    ev.append(
                        {
                            "t": time.time(),
                            "wave": nw,
                            "wave_id": merged.get("story_pipeline_wave_id"),
                            "substep": substep,
                            "label": label,
                        }
                    )
                    merged["story_pipeline_events"] = ev[-32:]
            elif nw is not None:
                merged.setdefault("story_pipeline_wave_entered_at", time.time())
        except Exception:
            if nw is not None:
                merged.setdefault("story_pipeline_wave_entered_at", time.time())

        daemon._update_shared_state(
            novel_id,
            writing_substep=substep,
            writing_substep_label=label,
            **merged,
        )

    ctx = runner._make_context(
        novel_id=novel_id,
        target_word_count=target_words,
        phase=runner._get_novel_phase(novel),
        auto_approve_mode=getattr(novel, "auto_approve_mode", False),
        genre=getattr(novel, "genre", ""),
        era=getattr(novel, "era", "ancient"),
    )
    ctx.writing_progress_sink = _writing_sink

    # pipeline 无状态（状态全在 ctx），按 daemon+genre 缓存避免每章重建
    # （用 __dict__ 而非 getattr：MagicMock 的 getattr 会自动产 Mock 导致缓存误命中）
    pipelines = daemon.__dict__.get("_cached_pipelines")
    if pipelines is None:
        pipelines = {}
        daemon._cached_pipelines = pipelines
    pipeline = pipelines.get(genre)
    if pipeline is None:
        pipeline = get_pipeline_registry().create_pipeline(genre)
        pipelines[genre] = pipeline
    result = await pipeline.run_chapter(ctx)

    if not result.success and result.error == "awaiting_ai_review":
        novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
        daemon._flush_novel(novel)
        logger.info("[%s] StoryPipeline 等待 AI Invocation 审阅", novel_id)
        return

    if not result.success and result.error == "interrupted":
        try:
            from application.engine.services.autopilot_recovery_policy import AutopilotRecoveryPolicy
            from application.engine.services.chapter_generation_workspace import ChapterGenerationWorkspace

            policy = AutopilotRecoveryPolicy(workspace=ChapterGenerationWorkspace())
            decision = policy.decide_on_daemon_tick(novel)
            policy.apply_transient_cleanup(decision)
        except Exception:
            pass
        daemon._update_shared_state(
            novel_id,
            writing_substep="interrupted",
            writing_substep_label="正文生成已中断，等待重试",
            current_stage="writing",
        )
        if daemon._is_still_running(novel):
            novel.current_stage = NovelStage.WRITING
            daemon._flush_novel(novel)
        logger.info("[%s] StoryPipeline 正文生成中断，未提交正式章节", novel_id)
        return

    if result.success:
        # 写作成功：清除模型失败摘要与连续计数（前端提示随之消失）
        daemon._clear_llm_error_summary(novel_id)
        chapter_num = result.chapter_number or ctx.chapter_number
        if getattr(result, "audit_snapshot", None):
            pending = getattr(daemon, "_pending_story_pipeline_aftermath", None)
            if pending is not None:
                content = result.content or ""
                pending[(novel_id, chapter_num)] = {
                    **dict(result.audit_snapshot),
                    "chapter_number": chapter_num,
                    "tension_composite": result.tension,
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "source": "story_pipeline",
                    "reused": False,
                }
        novel.current_auto_chapters = (novel.current_auto_chapters or 0) + 1
        novel.current_chapter_in_act = (novel.current_chapter_in_act or 0) + 1
        novel.current_beat_index = 0
        novel.beats_completed = False
        # 张力三态：`None` = 未评估，`0` = 评估过且张力就是 0。
        # 判据必须是 `is not None` —— 写成 `if result.tension:` 会把真 0 当成
        # 「没打分」，于是本章的张力留成**上一章**的值，读者看到的是别人的数字。
        if result.tension is not None:
            novel.last_chapter_tension = result.tension
        novel.current_stage = NovelStage.AUDITING

        daemon._update_shared_state(
            novel_id,
            writing_substep="pipeline_done",
            writing_substep_label="审计准备",
            current_chapter_number=chapter_num,
            last_chapter_tension=result.tension,
            audit_aftermath_reused=False,
            audit_aftermath_rebuilt=False,
        )
        daemon._flush_novel(novel)
        # 张力是章后审计算的，写完这一刻通常还没评分 —— 直接打 `%s` 会输出
        # 「张力None」。写「未评估」才是事实。
        logger.info(
            "[%s] StoryPipeline 完成：第%s章 %s字 张力%s",
            novel_id,
            chapter_num,
            result.word_count,
            "未评估" if result.tension is None else result.tension,
        )
        return

    error = result.error or "unknown"
    if "所有章节已写完" in error:
        logger.info("[%s] StoryPipeline：当前幕章节已全部写完", novel_id)
        if await daemon._current_act_fully_written(novel):
            novel.current_act = (novel.current_act or 0) + 1
            novel.current_chapter_in_act = 0
            novel.current_stage = NovelStage.ACT_PLANNING
            daemon._update_shared_state(
                novel_id,
                current_stage="act_planning",
                writing_substep="act_planning",
                writing_substep_label="幕级规划",
            )
        else:
            # Pipeline 没找到下一章，但当前幕又没有达到“已全部完成”的条件。
            # 这通常表示章节规划节点缺失或刚被并发清理，不应跳到下一幕；
            # 回到本幕规划，让引擎补齐当前幕章节。
            novel.current_chapter_in_act = 0
            novel.current_stage = NovelStage.ACT_PLANNING
            daemon._update_shared_state(
                novel_id,
                current_stage="act_planning",
                writing_substep="act_planning",
                writing_substep_label="幕级规划",
            )
        daemon._flush_novel(novel)
        return

    # 模型调用失败摘要透出给前端（/status: autopilot_last_error_summary）；
    # 连续计数用共享内存承载——novel.consecutive_error_count 会在每个
    # 「status 仍是 RUNNING」的 tick 尾部被 process_novel 重置，若只靠它，
    # 持续失败会被无限重试且熔断器始终记成功（对齐 act_planning_delegate 的做法）。
    from domain.novel.entities.novel import AutopilotStatus

    from application.ai_invocation.autopilot.shared_state import read_autopilot_shared_state

    failure_count = int(read_autopilot_shared_state(novel_id).get("autopilot_llm_failure_count") or 0) + 1
    chapter_label = f"第 {result.chapter_number} 章" if getattr(result, "chapter_number", None) else "本轮章节"
    summary = f"{chapter_label}写作失败（连续第 {failure_count}/3 次）：{str(error)[:140]}"
    daemon._report_llm_failure(novel_id, summary, failure_count=failure_count)
    novel.consecutive_error_count = (getattr(novel, "consecutive_error_count", 0) or 0) + 1
    if failure_count >= 3:
        novel.autopilot_status = AutopilotStatus.ERROR
        logger.error(
            "[%s] 写作连续失败 %s 次，已挂起等待处理（可在更换模型端点后解除）",
            novel_id,
            failure_count,
        )
    daemon._flush_novel(novel)
    logger.error("[%s] StoryPipeline 写作失败: %s", novel_id, error)
