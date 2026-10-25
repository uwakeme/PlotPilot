"""Prose composition strategies for StoryPipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from application.ai_invocation.contracts.autopilot_writing import (
    AUTOPILOT_CHAPTER_PROSE_CONTINUATION,
    AUTOPILOT_CHAPTER_PROSE_OPERATION,
)
from application.ai_invocation.dtos import InvocationSessionStatus
from application.engine.services.prose_context_settings import (
    get_prose_context_runtime_settings,
    prose_context_total_cap,
)
# E-07：温度来自单一来源（config/performance.yaml 的 prose_sampling 段）。
# ⚠ 必须是**模块级** import：之前它只在 ``_build_config`` 里做了局部 import，
# 而 ``compose``（另一个方法）也用了 ``prose_temperature()`` ⇒ 运行时 NameError
# ⇒ 整章正文**永远生成不出来**（真实跑一本真书时炸出来的）。
#   ⇒ 「局部 import 只覆盖那个函数」是个陷阱：同名函数在别的方法里用就炸。
from application.engine.services.prose_sampling_settings import prose_temperature
from application.core.chapter_target_limits import resolve_chapter_words
from infrastructure.ai.prompt_keys import CHAPTER_PROSE_GENERATION
from engine.runtime.generation_token_policy import CHAPTER_PROSE_MAX_TOKENS


StreamSink = Callable[[str], None]
StopChecker = Callable[[], bool]


@dataclass(frozen=True)
class ProseCompositionRequest:
    novel_id: str
    chapter_number: int
    chapter_title: str = ""
    novel_title: str = ""
    genre: str = ""
    outline: str = ""
    context_text: str = ""
    # X1：正文提示词要用的「硬事实」小块（槽位级、带上限）：
    # 死亡锁 / 角色锚点 / 创作契约。模型写正文时必须看见这些**不得违反的既成事实**。
    hard_facts: str = ""
    # E-06：给正文提示词的「本章节奏配额」（受开关控制，默认空）。
    rhythm_quota: str = ""
    style_guide: str = ""
    target_words: int = 2500
    auto_approve_mode: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    stream_sink: StreamSink | None = None
    stop_checker: StopChecker | None = None
    host: Any = None
    llm_service: Any = None


@dataclass(frozen=True)
class ProseCompositionResult:
    content: str = ""
    awaiting_review: bool = False
    session_id: str = ""
    interrupted: bool = False
    status: str = ""


class ProseComposer(Protocol):
    async def compose(self, request: ProseCompositionRequest) -> ProseCompositionResult:
        ...


class ChapterProseInvocationComposer:
    """Compose a whole chapter through the workbench prose CPMS node.

    The composer uses an autopilot-specific operation, so the manual chapter
    projection never bypasses the StoryPipeline save / validate / aftermath
    stages.
    """

    def _build_variables(self, request: ProseCompositionRequest) -> dict[str, Any]:
        metadata = request.metadata or {}
        # X1：「近3章承接摘要」与「硬事实」是**两个不同的东西**，
        # 此前挤在同一个变量槽里（`or request.context_text` 的后半支），
        # 而 metadata 那一支恒非空（continuity ledger 空时返回「暂无近3章承接摘要。」）
        # ⇒ 硬事实永远进不了正文提示词 ⇒ 模型看不见死亡锁、角色当前状态、文风公约。
        # 现在硬事实走**独立变量** `chapter_context`（槽位级、带上限）。
        continuity_context = str(metadata.get("continuity_context") or "").strip()

        settings = get_prose_context_runtime_settings()
        chapter_context = ""
        if settings.enabled:
            chapter_context = str(request.hard_facts or "").strip()
            if settings.max_chars_per_slot > 0 and len(chapter_context) > 0:
                # 总长兜底：与 builder **同一个公式**（`prose_context_total_cap`）。
                # ⚠ 此前 composer 用的是 `max_chars_per_slot × 槽位数`（6000），
                #   而 builder 产出 6109（块标题 + 截断标记也算进去了）
                #   ⇒ composer 每次都在第三块**中间**腰斩，把那块自己的
                #   「（本块已按上限截断）」标记换成了总标记
                #   ⇒ 模型看到的最后一块不再标着「2000 字上限」。
                #   现在 builder 自己守住总上限 ⇒ 这条兜底**不会触发**。
                total_cap = prose_context_total_cap(settings)
                if len(chapter_context) > total_cap:
                    chapter_context = (
                        chapter_context[:total_cap].rstrip()
                        + "\n（硬事实块已按总长上限截断）"
                    )
            elif not chapter_context:
                # S-07：取不到硬事实时**明说**，不留一节空的「必须遵守」。
                #
                # 模板无条件渲染「【硬事实（不得违反的既成事实）】」，
                # 而同一份提示词的硬约束第 7 条还在要求「【硬事实】……必须与之一致」
                # ⇒ 空块 ⇒ 模型看到一节**空的**约束（「这里本来该有硬事实，
                # 但现在没有」），于是要么脑补、要么直接忽略。
                #
                # ⚠ 措辞要写成「给模型看的话」，且**不许**暗示「没有约束」：
                # 「未取到」≠「不存在」，也不许模型凭空制造既成事实。
                chapter_context = (
                    "（本次未取到硬事实：上下文构建降级，"
                    "死亡锁 / 角色锚点 / 创作契约均未送达。"
                    "请勿据此推断存在任何额外约束，也不得凭空制造既成事实；"
                    "只依据下方大纲与正文写作。）"
                )

        return {
            "target_words": resolve_chapter_words(request.target_words),
            "chapter_outline": request.outline,
            "continuity_context": continuity_context,
            "chapter_context": chapter_context,
            # E-06：节奏配额**独立成变量**（它不是「硬事实」，
            # 是本章的节奏目标 ⇒ 不能塞进 chapter_context）。
            "rhythm_quota": str(request.rhythm_quota or "").strip(),
        }

    @staticmethod
    def _max_output_tokens(request: ProseCompositionRequest) -> int:
        return CHAPTER_PROSE_MAX_TOKENS

    @staticmethod
    def _build_config(request: ProseCompositionRequest):
        from domain.ai.services.llm_service import GenerationConfig

        # E-07：温度来自单一来源（模块级 import，见文件头注释）。
        return GenerationConfig(
            max_tokens=ChapterProseInvocationComposer._max_output_tokens(request),
            temperature=prose_temperature(),
        )

    @staticmethod
    def _load_committed_story_pipeline_content(request: ProseCompositionRequest) -> str:
        """Return accepted prose already committed for this StoryPipeline step.

        StoryPipeline owns the formal chapter save, so its continuation stores
        the accepted text in the invocation commit and then resumes writing.
        On the resumed pipeline tick we must consume that text instead of
        creating another AI invocation for the same chapter.
        """
        try:
            from infrastructure.persistence.database.connection import get_database

            row = get_database().fetch_one(
                """
                SELECT d.accepted_content AS accepted_content
                FROM ai_invocation_sessions s
                JOIN ai_adoption_decisions d ON d.session_id = s.id
                JOIN ai_adoption_commits c ON c.decision_id = d.id
                WHERE s.operation = ?
                  AND s.status = 'completed'
                  AND c.status = 'succeeded'
                  AND json_extract(s.context_json, '$.novel_id') = ?
                  AND CAST(json_extract(s.context_json, '$.chapter_number') AS INTEGER) = ?
                  AND json_extract(s.metadata_json, '$.commit_owner') = 'story_pipeline_save_step'
                ORDER BY c.updated_at DESC, d.accepted_at DESC
                LIMIT 1
                """,
                (
                    AUTOPILOT_CHAPTER_PROSE_OPERATION,
                    request.novel_id,
                    int(request.chapter_number or 0),
                ),
            )
        except Exception:
            return ""
        return str((row or {}).get("accepted_content") or "").strip()

    async def compose(self, request: ProseCompositionRequest) -> ProseCompositionResult:
        from application.ai_invocation.autopilot.factory import get_or_create_autopilot_orchestrator
        from application.ai_invocation.autopilot.intents import AutopilotInvocationIntent
        from application.ai_invocation.autopilot.policy import AutopilotInvocationPolicyResolver
        from application.ai_invocation.contracts import ensure_invocation_contract
        from infrastructure.persistence.database.connection import get_database

        committed_content = self._load_committed_story_pipeline_content(request)
        if committed_content:
            if request.stream_sink:
                request.stream_sink(committed_content)
            return ProseCompositionResult(
                content=committed_content,
                status="committed_story_pipeline_content",
            )

        db = get_database()
        ensure_invocation_contract(AUTOPILOT_CHAPTER_PROSE_OPERATION, CHAPTER_PROSE_GENERATION, db)
        context = {
            "novel_id": request.novel_id,
            "chapter_number": request.chapter_number,
            "beat_index": 0,
        }
        policy = AutopilotInvocationPolicyResolver().resolve(
            operation=AUTOPILOT_CHAPTER_PROSE_OPERATION,
            node_key=CHAPTER_PROSE_GENERATION,
            novel=type("StoryPipelinePolicy", (), {"auto_approve_mode": request.auto_approve_mode})(),
            context=context,
        )
        intent = AutopilotInvocationIntent(
            novel_id=request.novel_id,
            stage="writing",
            operation=AUTOPILOT_CHAPTER_PROSE_OPERATION,
            node_key=CHAPTER_PROSE_GENERATION,
            context=context,
            explicit_variables=self._build_variables(request),
            continuation_handler_key=AUTOPILOT_CHAPTER_PROSE_CONTINUATION,
            policy_hint=policy,
            metadata={
                "source": "story_pipeline.chapter_prose_composer",
                "target_words": request.target_words,
                "generation_mode": "full_chapter_once",
                "commit_owner": "story_pipeline_save_step",
            },
            config={
                "max_tokens": self._max_output_tokens(request),
                # E-07：温度来自单一来源。
                "temperature": prose_temperature(),
            },
        )
        host = request.host or type("StoryPipelineComposerHost", (), {"llm_service": request.llm_service})()
        orchestrator = get_or_create_autopilot_orchestrator(host)
        prepared = await orchestrator.prepare(intent)
        if prepared.session.status in {
            InvocationSessionStatus.AWAITING_PRE_CALL_REVIEW,
            InvocationSessionStatus.BLOCKED,
        }:
            return ProseCompositionResult(
                awaiting_review=True,
                session_id=prepared.session.id,
            )

        stop_seen = False

        def _on_chunk(chunk: str, full: str):
            nonlocal stop_seen
            if request.stop_checker and request.stop_checker():
                stop_seen = True
                return False
            if request.stream_sink:
                request.stream_sink(full)
            return True

        outcome = await orchestrator.generate_prepared_streaming(
            intent=intent,
            prepared_result=prepared,
            config=self._build_config(request),
            on_chunk=_on_chunk,
        )
        return ProseCompositionResult(
            content=outcome.accepted_content or "",
            session_id=outcome.session_id,
            interrupted=stop_seen or outcome.status == "cancelled" or outcome.next_action == "cancelled",
            status=outcome.status,
        )
