"""runtime delegates 的 host 隐式契约显式化（B1-2）。

背景：5 个 delegate（act_planning / audit / legacy_writing / macro_planning /
writing）的首参是 ``host: Any``，鸭子调用约 48 个不同成员——host 侧
（DaemonHostMixin / StoryPipelineRunner）重构时这些调用点没有任何静态
保护，静默破坏风险高。本 Protocol 把契约固化：host 拆分时保持本接口
实现即可，delegate 侧可逐步把 ``Any`` 换成 ``DaemonHostProtocol``。

注意：成员按 delegates 实际调用面归纳（方法 + 属性），不要求 host
显式 inherit——structural typing。
"""
from __future__ import annotations

from typing import Any, Awaitable, Dict, List, Optional, Protocol, runtime_checkable

from domain.ai.value_objects.prompt import Prompt
from domain.ai.services.llm_service import GenerationConfig
from domain.novel.entities.novel import Novel


@runtime_checkable
class DaemonHostProtocol(Protocol):
    """delegates 依赖的宿主能力面（按调用频次分组）。"""

    # ── 共享状态 / IPC（最高频）──
    def _update_shared_state(self, novel_id: str, **fields) -> None: ...
    def _report_llm_failure(self, novel_id: str, summary: str, *, failure_count: Optional[int] = None) -> None: ...
    def _clear_llm_error_summary(self, novel_id: str) -> None: ...
    def _read_autopilot_status_ephemeral(self, novel_id: Any) -> Optional[Any]: ...
    def _merge_autopilot_status_from_db(self, novel: Novel) -> None: ...
    def _is_still_running(self, novel: Novel) -> bool: ...
    def _flush_novel(self, novel: Novel) -> None: ...
    def _save_novel_state(self, novel: Novel) -> None: ...

    # ── LLM 流式 / 超时保护 ──
    def _stream_llm_with_stop_watch(
        self,
        prompt: Prompt,
        config: GenerationConfig,
        novel: Optional[Novel] = None,
        chapter_draft_so_far: str = "",
        total_timeout: float = 600.0,
        idle_timeout: float = 120.0,
    ) -> Awaitable[str]: ...
    async def _call_with_timeout(
        self,
        coro: Any,
        timeout: float,
        novel_id: str = "",
        label: str = "",
        timeout_default: Any = None,
    ) -> Any: ...

    # ── 章节写入 ──
    async def _upsert_chapter_content(
        self, novel: Novel, chapter_node: Any, content: str, status: str = "draft"
    ) -> Any: ...
    async def _stream_one_beat(
        self,
        outline: str,
        context: str,
        beat_prompt: str,
        beat: Any,
        novel: Optional[Novel] = None,
        voice_anchors: str = "",
        chapter_draft_so_far: str = "",
    ) -> str: ...
    def _save_chapter_ephemeral(self, novel_id: str, chapter_number: int, **fields) -> None: ...

    # ── 审计 / 事件 ──
    def _publish_audit_event(self, novel_id: str, event: Dict, **kwargs) -> None: ...
    def _count_completed_chapters(self, novel_id: Any) -> int: ...
    async def _run_memory_update(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        outline: str = "",
    ) -> Dict[str, Any]: ...

    # ── 注入的服务（属性）──
    planning_service: Any
    context_builder: Any
    chapter_workflow: Any
    aftermath_pipeline: Any
    background_task_service: Any
    chapter_repository: Any
    novel_repository: Any
    story_node_repo: Any
    llm_service: Any
    circuit_breaker: Any
    voice_drift_service: Any
    volume_summary_service: Any
    foreshadowing_repository: Any
    knowledge_service: Any
    poll_interval: int
