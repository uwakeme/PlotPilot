"""Pipeline Context — 管线执行的上下文和结果容器

PipelineContext 是贯穿整个管线的数据载体，每个 _step_xxx() 方法
读取和修改它。PipelineResult 是管线最终的输出。

设计原则：
- Context 是可变的（步骤往里塞数据），Result 是不可变的（管线输出）
- Context 携带所有步骤需要的依赖（novel_id, chapter_number, DB连接等）
- 步骤之间通过 Context 传递数据，不直接耦合
- 依赖通过 inject() 方法设置，不是构造参数（避免超长构造函数）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from domain.ai.value_objects.assessment_state import AssessmentState


class _ValidationStateContract:
    """三态字段（validation_state）的写入归一化。

    该字段此前在三条路径上并存两种运行时类型：dataclass 默认值给裸
    "unassessed"，护栏降级给 AssessmentState.UNASSESSED，真的跑过给
    report_state.value。因为 AssessmentState 是 str, Enum，`==` 全真，
    `is` 只有一条为真——静默的类型地雷。

    在写入点统一收敛，非法三态值直接 ValueError：写进一个不存在的状态
    比写裸串更危险。登记表 F-03。
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "validation_state" and not isinstance(value, AssessmentState):
            value = AssessmentState(value)
        super().__setattr__(name, value)


@dataclass
class PipelineContext(_ValidationStateContract):
    """管线执行上下文 — 贯穿所有步骤的数据载体

    使用方式：
        ctx = PipelineContext(novel_id="novel-123", chapter_number=5)
        ctx.inject(
            novel_repository=repo,
            llm_service=llm,
            context_builder=cb,
            ...
        )
        result = await pipeline.run_chapter(ctx)
    """

    # ═══ 核心标识（必须设置） ═══
    novel_id: str = ""
    chapter_number: int = 0

    # ═══ 输入数据 ═══
    outline: str = ""                            # 章节大纲
    target_word_count: int = 2500                # 目标字数
    # ⚠ D-09：这里曾有一个 `phase: str = "opening"`（由 runner 的「阶段→phase」
    # 硬编码映射喂进来）。那是**第二份** phase 口径：真实的那份在
    # `context_budget_allocator`（按**进度**判 StoryPhase + CPMS
    # `lifecycle-phase-directives` 指令），而 ctx.phase 全仓**无人消费**
    # ⇒ 留着只会让人误以为「阶段决定 phase」。已删。
    auto_approve_mode: bool = False              # 全自动模式
    genre: str = ""                              # 题材 (xuanhuan/xianxia/urban/mystery/short_drama)
    era: str = "ancient"                         # 时代背景

    # ═══ 步骤1产出：章节定位 ═══
    chapter_node: Optional[Any] = None           # StoryNode 实例
    needs_buffer: bool = False                   # 是否需要余韵章
    buffer_reason: str = ""                      # 余韵章原因
    # D-01：实际要施加的节奏指令。空串 = 本章不施加任何节奏约束。
    # 与 needs_buffer 分开：needs_buffer 表达「AI 判定本章是余韵章」，
    # buffer_directive 表达「这一步真的会把指令送进模型」——
    # 观察期（apply=false）时前者为 True 而后者为空，两种状态必须可分辨。
    buffer_directive: str = ""

    # D-03 P1：本章应推进的伏笔（**在七段剧本定稿之前**算出来）。
    # 剧本与正文上下文用**同一份** —— 各算一次就是两套口径。
    foreshadow_context: str = ""

    # ═══ 步骤2产出：上下文 ═══
    context_text: str = ""                       # 组装后的上下文
    context_tokens: int = 0                      # 上下文 token 数
    voice_anchors: str = ""                      # 声线锚点
    # X1：正文提示词要用的「硬事实」小块（死亡锁 / 角色锚点 / 创作契约），
    # 槽位级、带上限。与 context_text（整包）分开。
    hard_facts: str = ""
    bundle: Optional[Dict[str, Any]] = None      # chapter_workflow.prepare_chapter_generation() 的完整 bundle
    governance_budget: Optional[Dict[str, Any]] = None
    governance_context_request: Optional[Dict[str, Any]] = None
    evolution_continuity_report: Optional[Dict[str, Any]] = None

    # ═══ 步骤3产出：导演剧本 ═══
    script: str = ""                               # 六模块导演剧本文本
    beat_sheet: Optional[Any] = None             # 规划阶段的 BeatSheet（输入，保留兼容）
    beats: List[Any] = field(default_factory=list)  # 微观节拍 / 写作包

    # ═══ 步骤4产出：生成内容 ═══
    chapter_content: str = ""                    # 章节正文（最终版）
    word_count: int = 0                          # 实际字数
    raw_beat_contents: List[str] = field(default_factory=list)
    active_pipeline_run_id: str = ""             # 当前 StoryPipeline 运行 ID
    chapter_generation_workspace: Any = None     # 临时正文预览工作区
    generation_interrupted: bool = False         # stop/restart 导致本步骤中断

    # ═══ 步骤5产出：策略验证 ═══
    # 三态（AssessmentState）：UNASSESSED 时 score/passed/dimensions 均无结论，
    # 下游不得当作 0 分或「通过」使用。登记表 F-03。
    validation_state: AssessmentState = AssessmentState.UNASSESSED
    validation_passed: Optional[bool] = None
    validation_score: Optional[float] = None
    validation_violations: List[Dict[str, Any]] = field(default_factory=list)
    validation_suggestions: List[str] = field(default_factory=list)
    # 逐维诚实：某个维度可能因缺输入而未评估，此时值为 None（不是 0）。
    validation_dimensions: Dict[str, Optional[float]] = field(default_factory=dict)

    # ═══ 步骤6产出：章节保存 ═══
    chapter_saved: bool = False
    save_method: str = ""                        # ephemeral / queue / repository

    # ═══ 步骤7产出：文风审计 ═══
    drift_alert: bool = False
    similarity_score: Optional[float] = None
    voice_mode: str = "statistics"               # statistics / llm
    rewrite_applied: bool = False                # 是否触发了定向改写
    rewrite_attempts: int = 0                    # 定向改写尝试次数

    # ═══ 步骤8产出：章后管线 ═══
    narrative_sync_ok: bool = False
    vector_stored: bool = False
    foreshadow_stored: bool = False
    triples_extracted: bool = False
    causal_edges_stored: bool = False
    character_mutations_stored: bool = False
    debt_updated: bool = False
    emotion_ledger_updated: bool = False

    # ═══ 步骤9产出：张力打分 ═══
    tension_composite: Optional[float] = None    # 多维张力 (0-100)
    tension_plot: Optional[float] = None         # 情节张力
    tension_emotional: Optional[float] = None    # 情感张力
    tension_pacing: Optional[float] = None       # 节奏张力

    # ═══ 步骤10产出：收尾 ═══
    novel_stage_advanced: bool = False           # 状态是否推进
    next_stage: str = ""                         # 推进到的下一阶段

    # ═══ 断点续写 ═══
    existing_content: str = ""                   # 已有内容（断点续写）
    start_beat_index: int = 0                    # 从哪个节拍继续

    # ═══ 依赖注入（通过 inject() 设置） ═══
    novel_repository: Any = None
    chapter_repository: Any = None
    llm_service: Any = None
    context_builder: Any = None                  # ContextBuilder
    aftermath_pipeline: Any = None               # ChapterAftermathPipeline
    voice_drift_service: Any = None
    knowledge_service: Any = None
    foreshadowing_repository: Any = None
    story_node_repo: Any = None
    planning_service: Any = None
    chapter_preplanning_service: Any = None
    chapter_workflow: Any = None                 # AutoNovelGenerationWorkflow
    background_task_service: Any = None
    circuit_breaker: Any = None
    volume_summary_service: Any = None
    policy_validator: Any = None                 # PolicyValidator（新增）
    memory_orchestrator: Any = None              # MemoryOrchestratorImpl
    prose_composer: Any = None                   # StoryPipeline prose composition strategy

    # ═══ 全托管 UI：可选进度回调（substep, label, extra_dict）→ 写入共享内存 ═══
    writing_progress_sink: Any = None

    # ═══ 杂项 ═══
    metadata: Dict[str, Any] = field(default_factory=dict)
    _dependencies: Dict[str, Any] = field(default_factory=dict)

    def inject(self, **deps) -> "PipelineContext":
        """注入依赖（链式调用）

        Args:
            **deps: 任意依赖，键名对应上述字段名

        Returns:
            self（支持链式调用）

        Usage:
            ctx.inject(
                novel_repository=repo,
                llm_service=llm,
                context_builder=cb,
            )
        """
        for key, value in deps.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                self._dependencies[key] = value
        return self

    def get_dep(self, key: str, default: Any = None) -> Any:
        """获取注入的依赖"""
        if hasattr(self, key):
            val = getattr(self, key)
            return val if val is not None else default
        return self._dependencies.get(key, default)

    def is_fully_equipped(self) -> bool:
        """检查是否具备运行管线的最低依赖"""
        return all([
            self.novel_id,
            self.novel_repository is not None,
            self.llm_service is not None,
        ])


@dataclass
class PipelineResult(_ValidationStateContract):
    """管线最终输出 — 不可变记录"""
    success: bool = True
    chapter_number: int = 0
    content: str = ""
    word_count: int = 0
    # 张力三态：None = 未评估（打分没跑 / 失败），0 = 评估过且张力就是 0。
    # 这两者在「0」这一列上同形，任何拿它做创作判断的读者都会读到编造的值。
    # 单一事实源就是值本身 —— 不另设「已评估」标记，那等于制造第二个可能漂移的事实源。
    tension: Optional[int] = None                 # 0-100
    drift_alert: bool = False
    similarity_score: Optional[float] = None
    validation_score: Optional[float] = None
    validation_passed: Optional[bool] = None
    # 护栏三态（AssessmentState）：UNASSESSED 时 validation_score 为 None，
    # 不得当作 0 分使用（AssessmentState.UNASSESSED）。登记表 F-03。
    validation_state: AssessmentState = AssessmentState.UNASSESSED
    narrative_sync_ok: bool = False
    error: Optional[str] = None

    # 审计快照（供 _step_finalize 落库）
    audit_snapshot: Dict[str, Any] = field(default_factory=dict)

    # 各步骤的执行状态
    step_status: Dict[str, str] = field(default_factory=dict)  # step_name → "ok"/"skipped"/"failed"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "chapter_number": self.chapter_number,
            "word_count": self.word_count,
            "tension": self.tension,
            "drift_alert": self.drift_alert,
            "similarity_score": self.similarity_score,
            # 未评估时是 None，不是 0 分——round(None) 会 TypeError
            "validation_score": (
                None if self.validation_score is None else round(self.validation_score, 3)
            ),
            "validation_passed": self.validation_passed,
            "validation_state": self.validation_state.value,
            "narrative_sync_ok": self.narrative_sync_ok,
            "error": self.error,
            "step_status": self.step_status,
        }
