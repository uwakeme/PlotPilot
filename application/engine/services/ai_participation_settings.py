"""X-02：AI 参与能力的**总闸**（模型异常 / 成本失控时的回退路径）。

背景
----
全仓只有少数几项「AI 参与」能力各自带开关
（``chapter_mode`` / ``chapter_target`` / ``context_weights`` / 声音分析的 LLM 模式），
而上一批新增的 AI 调用点**一个开关都没有**
⇒ 模型异常或成本失控时**没有回退路径**（只能回滚代码）。

总闸语义（刻意保守，**默认不改变今天的行为**）
------------------------------------------------
- ``master_enabled`` 默认 **True** ⇒ 默认状态下与现在逐位一致；
- 置 False ⇒ 登记在册的「AI 参与」能力全部回到改造前行为（不发起模型调用），
  **并且**逐条开关也被强制关闭（**总闸优先于逐条开关** —— 这才是回退路径的保证）；
- ``disabled_features`` ⇒ 逐条点名要关的能力（总闸开着时也能单独关）。

⚠ **新增「AI 参与」能力时必须登记进 ``AI_PARTICIPATION_FEATURES``** ——
不登记 ⇒ 总闸管不到它（守卫 ``test_ai_participation_master_switch.py`` 盯这张表）。

配置位置：``config/performance.yaml`` 的 ``ai_participation:`` 段。
"""
from __future__ import annotations

from dataclasses import dataclass

from application.core.config.config_loader import get_config
from application.core.config.runtime_settings_utils import section_value

#: 登记在册的「AI 参与」能力（feature id）。
#: 总闸关闭 ⇒ 下面每一项都回到改造前行为（不发起模型调用）。
#:
#: ⚠ **这张表必须与「真的会去打模型的模块」保持一致** ——
#: 登记了却没接线 ⇒ 总闸管不到它（承诺是空的）；
#: 能打模型却没登记 ⇒ 总闸同样管不到它。
#: 两个方向的守卫在
#: ``tests/unit/application/engine/test_ai_participation_master_switch.py``。
AI_PARTICIPATION_FEATURES: tuple[str, ...] = (
    # ── 第 3 批之前就有的 5 项 ──
    "chapter_mode",  # D-01 余韵章判定
    "chapter_target",  # D-05 每章字数目标
    "context_weights",  # D-02 上下文槽位权重
    "voice_llm_mode",  # F-04 声音分析的 LLM 模式
    "continuation_strategy",  # D-08 续写策略
    # ── 第 1/2 批加的 AI 能力（此前也漏登记了）──
    "finale_intent",  # B-07/B-13 终局意图
    "anti_ai_pattern_induction",  # F-06 anti_ai 模式归纳
    "conflict_action_extraction",  # H-02 冲突动作抽取
    "character_belief_check",  # H-03 角色信念冲突判定
    "character_context_selection",  # D-04 角色上下文按章取材
    "foreshadow_selection",  # D-03 伏笔按章取材
    "plot_point_classifier",  # F-08 剧情点标签与描述
    # ── 第 3 批新增的 10 项 ──
    "structure_proposal",  # B-05 结构网格提案
    "first_act",  # C-07 全书首幕
    "target_budget_proposal",  # J-01 目标章数/终局预算提案
    "planning_depth",  # B-06 规划深度自评
    "rhythm_quota",  # E-06 题材节奏配额
    "act_volume_placement",  # B-08 建幕时声明所属卷
    "character_ner",  # G-05 新角色发现（NER）
    "dead_character_check",  # H-01 死亡角色出场方式判定
    "wuxia_style_audit",  # E-04 武侠风格审
    "macro_breakpoint",  # H-06A 宏观断点语义分类
    "macro_refactor_proposal",  # H-06B 宏观重构提案
    "emotion_ledger",  # G-04 情绪账本抽取
    # ── 第 9 批新增 ──
    "style_preset_match",  # J-02 文风预设语义匹配
)

#: **没有代码兜底物**的能力（总闸关掉 ⇒ 这项判断**本轮不存在**，
#: 而不是「回到改造前的代码判断」）。
#:
#: ⚠ 这几项在改造时**刻意删掉了代码侧的替代物** ——
#:   F-08 的剧情点阈值表、H-03 的信念关键词表 ——
#:   ⇒ 没有可回落的东西 ⇒ 关闸后它们只能标「未评估」。
#: ⇒ 「关闸 ⇒ 回到改造前行为」这句话**对它们不成立**（第 3 批三次复审 R4）。
#:
#: ⚠ 第 82 批（N-12）：**G-04 `emotion_ledger` 也在这张表里** ——
#:   关闸后 ``EmotionLedgerExtractor.extract_deltas`` 返回 ``_empty_deltas()``，
#:   而模块里**没有任何规则版的情绪抽取器** ⇒ 关闸 = **账本全空**，
#:   不是「回到改造前行为」（改造前根本没有情绪账本）。
#:
#: 其余登记在册的能力都有代码兜底 ⇒ 关闸 = 回到改造前行为。
FEATURES_WITHOUT_CODE_FALLBACK: tuple[str, ...] = (
    "plot_point_classifier",  # F-08：剧情点标签的阈值表已刻意删除
    "character_belief_check",  # H-03：信念冲突的关键词表 + 正则已刻意删除
    "emotion_ledger",  # G-04：没有规则版抽取器 ⇒ 关闸 = 账本全空
)

#: 走「settings 模块」**间接**接闸的服务（服务本身不直接调用总闸）。
#:
#: 这几项的历史原因是：它们各自有一个 ``*_settings`` 模块负责读配置，
#: 总闸的判定被折进了那个 ``enabled`` 字段 ⇒ 服务侧看不到 ``ai_participation_enabled``。
#:
#: 三元组 = (会打模型的模块, **持闸的**模块, feature id)。
#: ⚠ 持闸模块可能不是调用方本身（``continuation_strategy`` 的闸在 ``base.py`` 里）。
#:
#: ⚠ **新增这类服务必须登记在这里**，否则双向守卫
#: （``test_ai_participation_master_switch_coverage.py``）会报红。
AI_PARTICIPATION_SETTINGS_MEDIATED: tuple[tuple[str, str, str], ...] = (
    (
        "application/engine/services/chapter_mode_planner.py",
        "application/engine/services/chapter_mode_settings.py",
        "chapter_mode",
    ),
    (
        "application/engine/services/context_weight_planner.py",
        "application/engine/services/context_weight_settings.py",
        "context_weights",
    ),
    (
        "application/engine/services/continuation_strategy_planner.py",
        "engine/pipeline/base.py",
        "continuation_strategy",
    ),
    (
        "application/analyst/services/llm_voice_analysis_service.py",
        "application/analyst/services/voice_drift_service.py",
        "voice_llm_mode",
    ),
)

#: 「AI 参与能力」模块 —— **可选的判断类**模型调用。
#: 总闸关闭 ⇒ 回到改造前行为（代码兜底 / 未评估）。
#:
#: ⚠ 这张表是**归类声明**，不是名单式的「豁免」——
#: 守卫会要求表里每个模块都真的接了闸（直接调或走中介）。
AI_PARTICIPATION_CAPABILITY_MODULES: frozenset[str] = frozenset(
    {
        "application/analyst/services/llm_voice_analysis_service.py",
        # 闸门收在**分派点**（`use_llm_mode` 有 4 个生产调用点，
        # 收在服务层才一次全覆盖）⇒ 它自己也是会打模型的模块。
        "application/analyst/services/voice_drift_service.py",
        "application/audit/services/anti_ai_pattern_induction.py",
        "application/audit/services/conflict_action_extraction.py",
        "application/audit/services/macro_breakpoint_classifier.py",
        "application/audit/services/macro_refactor_proposal_service.py",
        "application/audit/services/wuxia_style_audit.py",
        "application/blueprint/services/act_volume_placement_service.py",
        "application/blueprint/services/finale_intent_service.py",
        "application/blueprint/services/first_act_planner.py",
        "application/blueprint/services/structure_proposal.py",
        "application/blueprint/services/target_budget_proposal.py",
        "application/character/services/character_belief_check.py",
        "application/character/services/character_context_selection.py",
        "application/character/services/character_ner.py",
        "application/engine/services/chapter_mode_planner.py",
        "application/engine/services/context_weight_planner.py",
        "application/engine/services/continuation_strategy_planner.py",
        "application/engine/services/planning_depth_assessor.py",
        "application/engine/services/rhythm_quota_planner.py",
        "application/evolution/services/dead_character_checker.py",
        "application/world/services/foreshadow_selection.py",
        "application/world/services/plot_point_classifier.py",
        "application/world/services/style_preset_matcher.py",
        "engine/infrastructure/memory/emotion_ledger_extractor.py",
    }
)



# #: 核心生成链路模块 —— **不受**总闸管辖。
# #:
# #: 这些模块的模型调用是「把书写出来」本身（正文、大纲、场景、Bible、摘要、
# #: 张力评分、审稿、记忆抽取、DAG 节点…），关掉它们 = **写不出书**，
# #: 不是「回到改造前行为」。
# #: X-02 的语义是「模型异常 / 成本失控时的**回退路径**」，
# #: 指的是**可选的创作判断**，不是把整个生成链路掐掉。
AI_PARTICIPATION_CORE_PIPELINE_MODULES: frozenset[str] = frozenset(
    {
        "application/ai/llm_control_service.py",
        "application/ai/structured_json_pipeline.py",
        # ── 第 82 批（N-11）：探测器放宽后**暴露**出来的 8 个真打模型的模块 ──
        # 此前分类守卫的探测器只认「<名字带 llm 的东西>.generate(...)」
        # ⇒ 看不见 ``stream_generate`` 与 ``self._attempt_service.generate(...)``
        # ⇒ 这 8 个模块**从来没被归类过** ⇒ 分类清单一直是残缺的。
        # ⚠ 归类是**声明**，不是「它们不受总闸管辖」的结论：
        #   它们要么是**出模型口本身**（关掉 = 写不出书），
        #   要么是**把模型调用转发出去**的通道 ⇒ 都属核心链路。
        "application/ai_invocation/gateway.py",  # CPMS 调用网关（生产主路）
        "application/ai_invocation/autopilot/orchestrator.py",  # 自动驾驶的调用编排
        "application/engine/dag/plan/outline_beat_planner.py",  # DAG 大纲节拍规划
        "engine/runtime/daemon_host.py",  # 守护进程（正文生成等）
        "infrastructure/ai/llm_client.py",  # LLM 客户端（出模型口）
        "infrastructure/ai/provider_factory.py",  # provider 工厂（最终出模型口）
        "infrastructure/ai/providers/mock_provider.py",  # Mock provider（同一出口）
        "interfaces/api/v1/engine/ai_invocation_routes.py",  # 调用网关的 HTTP 入口
        # ── 第 63 批：AI 调用通道（CPMS invocation）的**统一出模型口** ──
        # `AttemptService.generate / generate_streaming` 直接调 `LLMService`
        # ⇒ 正文生成（chapter.generate.prose）、A1 审计、章后同步、桥接修复…
        # **全部**经它 ⇒ 关掉它 = 写不出书 ⇒ 归核心链路，不要求自己接闸。
        # ⚠ 它之前**从来没被归类过**：文件带 BOM ⇒ 覆盖扫描（ast.parse）静默跳过它
        # ⇒ 总闸的覆盖清单一直是残缺的 ⇒ 第 63 批去掉 BOM 才暴露出来。
        "application/ai_invocation/services.py",
        "application/analyst/services/state_extractor.py",
        "application/analyst/services/tension_analyzer.py",
        "application/audit/services/book_review_service.py",
        "application/audit/services/chapter_ai_review_service.py",
        "application/audit/services/chapter_review_service.py",
        "application/audit/services/macro_calibration.py",
        # ── 第 3 批二次复审 S3 新增的 13 个「**传递调用**」模块 ──
        # 它们**自己不打模型**，但调用了某个已接闸的能力模块
        # （⇒ 关闸对它们天然生效）⇒ 归核心链路，不要求自己接闸。
        "application/audit/services/anti_ai_learning.py",
        "application/audit/services/macro_diagnosis_service.py",
        "application/evolution/services/gate_service.py",
        "engine/infrastructure/memory/memory_orchestrator_impl.py",
        "engine/pipeline/base.py",
        "engine/pipelines/wuxia_pipeline.py",
        "engine/runtime/macro_planning_delegate.py",
        "engine/runtime/runner.py",
        "interfaces/api/dependencies.py",
        "interfaces/api/v1/audit/macro_refactor.py",
        "interfaces/api/v1/core/novels.py",
        "interfaces/api/v1/engine/checkpoint_routes.py",
        "interfaces/api/v1/world/cast.py",
        # J-02：这条路由**自己不打模型**，但调用了已接闸的
        # ``StylePresetMatcher.suggest`` ⇒ 关闸对它天然生效（返回「未评估」）
        # ⇒ 归核心链路，不要求自己接闸。
        "interfaces/api/v1/world/bible.py",
        "application/blueprint/services/beat_sheet_service.py",
        "application/blueprint/services/chapter_preplanning_service.py",
        "application/blueprint/services/continuous_planning_service.py",
        "application/blueprint/services/volume_summary_service.py",
        "application/core/services/scene_generation_service.py",
        "application/core/services/title_suggestion_service.py",
        "application/engine/dag/nodes/anti_ai_nodes.py",
        "application/engine/dag/nodes/execution_nodes.py",
        "application/engine/dag/nodes/ext_supplement_nodes.py",
        "application/engine/dag/nodes/gen_supplement_nodes.py",
        "application/engine/dag/nodes/planning_nodes.py",
        "application/engine/dag/nodes/review_nodes.py",
        "application/engine/dag/nodes/world_nodes.py",
        "application/engine/services/ai_generation_service.py",
        "application/engine/services/memory_engine.py",
        "application/engine/services/scene_director_service.py",
        "application/prop/extractors/llm_extractor.py",
        "application/reader/services/reader_simulation_service.py",
        "application/workflows/auto_novel_generation_workflow.py",
        "application/world/services/auto_bible_generator.py",
        "application/world/services/auto_knowledge_generator.py",
        "application/world/services/chapter_narrative_sync.py",
        "infrastructure/ai/claude_chapter_summarizer.py",
        "interfaces/api/v1/workbench/sandbox.py",
    }
)



#: **已知缺口**：会打模型、但还没接总闸的模块。
#:
#: ⚠ 这张表**只该变短，不该变长** —— 每加一条都要在 ledger 里写清
#: 「为什么现在不接、什么时候接」。守卫会要求「会打模型的模块」
#: 必须被三张表之一覆盖 ⇒ 新模块漏归类会报红。
AI_PARTICIPATION_UNGOVERNED_MODULES: dict[str, str] = {
    # ⚠ 第 3 批二次复审 S8：上一轮记在这里的
    #   ``macro_refactor_proposal_service`` 已**修好**（闸门放在 service 内、
    #   try/except 之前 ⇒ 关闸返回空提案）⇒ 这张表现在是**空的**。
    #   ⇒ 它会**继续变空**（每修一个就少一个），只许变短不许变长。
}

@dataclass(frozen=True)
class AiParticipationRuntimeSettings:
    #: 总闸。False ⇒ 所有登记能力强制关闭（逐条开关也失效）。
    master_enabled: bool = True
    #: 逐条点名关闭的能力（总闸开着时生效）。
    disabled_features: tuple[str, ...] = ()


#: 总闸的 **env 级**（部署级回退）。
#:
#: ⚠ 第 83 批（N-09）：此前总闸**只有 config 一级**
#:   ⇒ 「这台机器上回退」「这本书回退」两件事都做不到
#:   ⇒ 现在补上 env 级（与 config 同层，**env 优先**）与 novel 级（在判定里读）。
ENV_MASTER_ENABLED = "PLOTPILOT_AI_MASTER_ENABLED"
ENV_DISABLED_FEATURES = "PLOTPILOT_AI_DISABLED_FEATURES"


def get_ai_participation_runtime_settings() -> AiParticipationRuntimeSettings:
    """读总闸的运行时设置（**config 级 + env 级**）。

    ⚠ 第 83 批（N-09）：补上 **env 级**。
    ⚠ 优先级与统一开关层一致：**env > config**
    （env 的 ``disabled_features`` **替换** config 那份 —— 同一层，env 更具体）。
    """
    import os

    from application.engine.services.ai_switch_layer import parse_env_bool

    section = getattr(get_config(), "ai_participation", None)
    defaults = AiParticipationRuntimeSettings()
    raw_disabled = section_value(section, "disabled_features", ())
    if isinstance(raw_disabled, str):
        raw_disabled = [raw_disabled]
    disabled = tuple(
        str(item).strip() for item in (raw_disabled or ()) if str(item).strip()
    )
    master_enabled = bool(
        section_value(section, "master_enabled", defaults.master_enabled)
    )

    # ⚠ env 级：总闸本身
    raw_env_master = os.environ.get(ENV_MASTER_ENABLED)
    if raw_env_master is not None:
        parsed = parse_env_bool(raw_env_master)
        if parsed is not None:
            master_enabled = parsed
    # ⚠ env 级：逐条点名关闭（**替换** config 那份 —— 同一层，env 更具体）
    raw_env_disabled = os.environ.get(ENV_DISABLED_FEATURES)
    if raw_env_disabled is not None:
        disabled = tuple(
            part.strip() for part in raw_env_disabled.split(",") if part.strip()
        )

    return AiParticipationRuntimeSettings(
        master_enabled=master_enabled,
        disabled_features=disabled,
    )


def ai_participation_enabled(feature: str) -> bool:
    """该「AI 参与」能力此刻是否允许发起模型调用。

    总闸优先于逐条开关：总闸关闭 ⇒ 一律 False。
    未登记在 ``AI_PARTICIPATION_FEATURES`` 的 feature id 一律**拒绝**
    （不认识的 id 绝不默认放行 —— 那是「登记漏了 ⇒ 总闸失效」的入口）。

    ⚠ 第 83 批（N-09）：**三级**回退能力（此前只有 config 一级）：
      ① **config 级**：``ai_participation.master_enabled`` + ``disabled_features``；
      ② **env 级**：``PLOTPILOT_AI_MASTER_ENABLED`` + ``PLOTPILOT_AI_DISABLED_FEATURES``；
      ③ **novel 级**：这本书的 ``generation_prefs.ai_switches[<feature_id>] = False``
         ⇒ 「**按这本书**回退」终于有了落点（规格 §3.2 承诺的那一层）。
    ⚠ novel 级**只能关，不能开** —— 总闸关掉时一律 False（总闸是回退路径，不是开关）。
    """
    feature_id = str(feature or "").strip()
    if feature_id not in AI_PARTICIPATION_FEATURES:
        return False
    settings = get_ai_participation_runtime_settings()
    if not settings.master_enabled:
        return False
    if feature_id in settings.disabled_features:
        return False

    # ⚠ novel 级：这本书把这项能力关掉了（键 = feature id）。
    # ⚠ 键空间不冲突：feature id（``chapter_mode``）与逐条开关 key
    #   （``chapter_mode.enabled``）是两个不同的字符串。
    from application.engine.services.ai_switch_layer import current_novel_switches

    novel_switches = current_novel_switches() or {}
    if novel_switches.get(feature_id) is False:
        return False
    return True
