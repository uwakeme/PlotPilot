# -*- coding: utf-8 -*-
"""第 81 批：**语义常量钉死表** + 「空洞守卫」通则守卫。

起因（2026-11-02 独立验收）
--------------------------
两条守卫被复现为**空洞的**：

* ``structure_proposal.FALLBACK_GRID`` 从 ``(3, 3, 3)`` 改成 ``(9, 9, 9)``
  （= 729 幕！）⇒ 点名守卫 22 passed、**全量 4297 也全绿**；
* ``plot_point_classifier_schema.SOURCE_NONE`` 从 ``""`` 改成 ``"none"``
  ⇒ 点名守卫 17 passed、全量仍全绿。

根因：**期望值从被测模块自己 import 回来再比较** ⇒ 自指恒真。

⇒ 这条文件把「谁的值是契约」**用字面量**钉在一处（``PINNED``），
  再加一条**通则**守卫：
  **凡是测试从生产模块 import 的、名字像语义常量的，必须在某处有字面量钉住，
  否则必须在本文件里显式豁免并写理由** ⇒ 新增一个忘了登记 ⇒ 立刻红。

⚠ 为什么不全仓 253 个常量都钉：
  协议键名（``NODE_KEY`` / ``OPERATION`` / ``*_BINDING_SET_ID``）的值
  **不是语义契约**（生产方与消费方共享同一个常量即可）⇒ 不钉。
  ⇒ 只有「值本身被解释」的常量（阈值 / 上下限 / 网格 / 三态标记 / 权重）才钉。
"""
from __future__ import annotations

import ast
import importlib
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 名字像「语义常量」的形状（阈值 / 上下限 / 网格 / 三态标记 / 权重 …）。
SEMANTIC_NAME = re.compile(
    r"(THRESHOLD|GRID|_MAX\b|_MIN\b|_LIMIT|SOURCE_NONE|SOURCE_FALLBACK|DEFAULT_"
    r"|_SCORE|_WEIGHT|_RATIO|_SECONDS|_TIMEOUT|_RETRIES)"
)
CONST_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
PROD_HINT = ("application.", "engine.", "interfaces.", "infrastructure.", "domain.")

#: 显式豁免：常量 → **理由**（不许裸豁免）。
EXEMPTED: dict[tuple[str, str], str] = {
    ("infrastructure.ai.prompt_contracts.context_weights", "CONTEXT_WEIGHTS_CONTRACT"):
        "PromptContract **对象**（不是标量）⇒ 它的 node_key/version 另有守卫钉；对象整体不钉",
    ("interfaces.api.local_access", "DEFAULT_ALLOWED_HOSTS"):
        "本机访问白名单：按部署增删（前端 dev 端口等）⇒ 钉死反而碍事",
    ("interfaces.api.local_access", "DEFAULT_CORS_ORIGINS"):
        "CORS 白名单：同上（部署相关）",
    ("infrastructure.ai.llm_environment", "ARK_DEFAULT_BASE_URL"):
        "外部厂商端点：换厂商就要改 ⇒ 不属于「语义契约」",
    ("application.engine.services.prose_sampling_settings", "DEFAULT_PROSE_TEMPERATURE"):
        "采样温度是**调优项**（不是契约）⇒ 钉死会挡住调参",
}

#: 语义常量 → (模块, 常量名, **字面量**, 为什么它的字面量是契约)
#:
#: ⚠ 这里的值是**字面量**（不是 import 回来的）⇒ 改生产常量 ⇒ 这条立刻红。
PINNED: tuple[tuple[str, str, object, str], ...] = (
("application.analyst.services.voice_drift_service", "DRIFT_ALERT_THRESHOLD", 0.50, "（语义常量：值变了必须显式改本表；第 86 批口径重标定 0.75→0.50）"),
    ("application.audit.dtos.macro_refactor_dto", "SOURCE_NONE", '', "「未评估」的**三态语义标记**（空/标记串被换掉 ⇒ 下游把未评估当成真实来源）"),
    ("application.audit.services.anti_ai_pattern_induction_schema", "SOURCE_NONE", '', "「未评估」的**三态语义标记**（空/标记串被换掉 ⇒ 下游把未评估当成真实来源）"),
    ("application.audit.services.anti_ai_thresholds", "ASSESSMENT_THRESHOLDS", [(4.0, '严重'), (2.0, '中等'), (0.8, '轻微'), (0.0, '纯净')], "反 AI 评级档位（4.0/2.0/0.8/0.0）"),
    ("application.audit.services.anti_ai_thresholds", "DEFAULT_ASSESSMENT", '纯净', "反 AI 默认评级"),
    ("application.audit.services.anti_ai_thresholds", "SEVERE_ASSESSMENT_THRESHOLD", 4.0, "「严重」线（4.0）—— 与评级表首档必须一致"),
    ("application.audit.services.anti_ai_thresholds", "SEVERITY_WEIGHTS", {'critical': 3.0, 'warning': 1.0, 'info': 0.3}, "反 AI 严重度权重（critical 3 / warning 1 / info 0.3）"),
    ("application.audit.services.conflict_action_extraction_schema", "SOURCE_NONE", '', "「未评估」的**三态语义标记**（空/标记串被换掉 ⇒ 下游把未评估当成真实来源）"),
    ("application.audit.services.macro_breakpoint_schema", "SOURCE_NONE", '', "「未评估」的**三态语义标记**（空/标记串被换掉 ⇒ 下游把未评估当成真实来源）"),
    ("application.audit.services.setting_drift_audit", "DEFAULT_TIME_BUDGET_SECONDS", 600.0, "设定漂移审计的默认时间预算（秒）"),
    ("application.blueprint.services.act_volume_placement_contract", "SOURCE_FALLBACK", 'fallback', "「走了兜底」的来源标记（与真实来源必须可分辨）"),
    ("application.blueprint.services.act_volume_placement_contract", "SOURCE_NONE", 'none', "「未评估」的**三态语义标记**（空/标记串被换掉 ⇒ 下游把未评估当成真实来源）"),
    ("application.blueprint.services.finale_intent_contract", "SOURCE_NONE", 'none', "「未评估」的**三态语义标记**（空/标记串被换掉 ⇒ 下游把未评估当成真实来源）"),
    ("application.blueprint.services.planning_depth", "SOURCE_THRESHOLD", 'threshold', "「按阈值判定」的来源标记"),
    ("application.blueprint.services.structure_proposal", "FALLBACK_GRID", (3, 3, 3), "B-05 承诺「AI 判不出结构 ⇒ 兜底 3部×3卷×3幕（与改造前逐位一致）」"),
    ("application.blueprint.services.structure_proposal", "SOURCE_FALLBACK", 'fallback', "「走了兜底」的来源标记（与真实来源必须可分辨）"),
    ("application.blueprint.services.structure_proposal", "SOURCE_NONE", '', "「未评估」的**三态语义标记**（空/标记串被换掉 ⇒ 下游把未评估当成真实来源）"),
    ("application.blueprint.services.target_budget", "AUTO_EXTEND_BLOCK_MIN", 10, "自动加章块**最小**章数"),
    ("application.blueprint.services.target_budget", "CONTINUE_BLOCK_MIN", 20, "续写块**最小**章数（AI 提议的合法区间边界）"),
    ("application.blueprint.services.target_budget", "DEFAULT_TARGET_CHAPTERS", 100, "目标章数**默认值**（与用户设定缺省时一致）"),
    ("application.blueprint.services.target_budget", "FINALE_BUDGET_MAX", 200, "终局预算**上界**"),
    ("application.blueprint.services.target_budget", "FINALE_BUDGET_MIN", 5, "终局预算**下界**（AI 提议的合法区间边界）"),
    ("application.blueprint.services.target_budget", "TARGET_CHAPTERS_MAX", 9999, "目标章数**上限**（AI 提议的合法区间边界）"),
    ("application.blueprint.services.target_budget_proposal", "DEFAULT_TIMEOUT_SECONDS", 30, "AI 调用的默认超时（秒）"),
    ("application.blueprint.services.target_budget_schema", "SOURCE_FALLBACK", 'fallback', "「走了兜底」的来源标记（与真实来源必须可分辨）"),
    ("application.character.services.character_belief_check_schema", "SOURCE_NONE", '', "「未评估」的**三态语义标记**（空/标记串被换掉 ⇒ 下游把未评估当成真实来源）"),
    ("application.character.services.character_context_selection_schema", "CATEGORY_LIMIT", {'scars': 2, 'motivations': 2, 'voice': 3, 'catchphrases': 3, 'knowledge': 3, 'relationships': 2}, "人物上下文**每类上限**（防止提示词被一类灌满）"),
    ("application.character.services.character_context_selection_schema", "SOURCE_FALLBACK", 'fallback', "「走了兜底」的来源标记（与真实来源必须可分辨）"),
    ("application.character.services.character_context_selection_schema", "SOURCE_NONE", '', "「未评估」的**三态语义标记**（空/标记串被换掉 ⇒ 下游把未评估当成真实来源）"),
    ("application.character.services.character_ner", "SOURCE_NONE", '', "「未评估」的**三态语义标记**（空/标记串被换掉 ⇒ 下游把未评估当成真实来源）"),
    ("application.core.chapter_target_limits", "CHAPTER_TARGET_WORDS_MAX", 20000, "每章字数**上界**"),
    ("application.core.chapter_target_limits", "CHAPTER_TARGET_WORDS_MIN", 500, "每章字数**下界**（500..20000 的钳制口径）"),
    ("application.core.chapter_target_limits", "DEFAULT_CHAPTER_WORDS", 2500, "每章字数**默认值**"),
    ("application.engine.services.chapter_target_resolver", "DEFAULT_MAX_DEVIATION_RATIO", 0.3, "AI 提议每章字数可偏离的比例上限（±30%）"),
    ("application.engine.services.context_builder", "DEFAULT_CONTEXT_MAX_TOKENS", 60000, "上下文预算**唯一**数字（60000；改它必须显式改守卫）"),
    ("application.engine.services.context_lifecycle", "DEFAULT_PHASE_THRESHOLDS", {'opening': 0.25, 'development': 0.75, 'convergence': 0.9, 'finale': 1.01}, "四阶段的**叙事进度阈值**（开/展/收/终）"),
    ("application.engine.services.continuation_strategy_schema", "SOURCE_NONE", 'none', "「未评估」的**三态语义标记**（空/标记串被换掉 ⇒ 下游把未评估当成真实来源）"),
    ("application.engine.services.prose_context_settings", "DEFAULT_MAX_CHARS_PER_SLOT", 12000, "单个上下文槽的字符上限"),
    ("application.engine.services.recent_chapter_context", "DEFAULT_PREV_FULL_TEXT_MAX_CHARS", 3000, "上一章**全文**给出的字符上限"),
    ("application.engine.services.rhythm_quota", "SOURCE_NONE", '', "「未评估」的**三态语义标记**（空/标记串被换掉 ⇒ 下游把未评估当成真实来源）"),
    ("application.world.services.auto_bible_generator", "PREDICATE_SOURCE_AI", 'ai', "三元组谓词来源标记（AI 判的）"),
    ("application.world.services.auto_bible_generator", "PREDICATE_SOURCE_KEYWORD_GUESS", 'keyword_guess', "三元组谓词来源标记（**代码按关键词猜的**）"),
    ("application.world.services.auto_bible_generator", "AI_DERIVED_TRIPLE_CONFIDENCE", 0.9, "AI 给的谓词对应的三元组置信度"),
    ("application.world.services.auto_bible_generator", "KEYWORD_GUESSED_TRIPLE_CONFIDENCE", 0.5, "代码猜的谓词对应的置信度（**必须低于** AI 那一档 ⇒ 两者可分辨）"),
    ("application.world.services.foreshadow_pacing", "DEFAULT_RESOLVE_OFFSET", 5, "伏笔回收偏移**默认值**"),
    ("application.world.services.foreshadow_pacing", "RESOLVE_OFFSET_MAX", 30, "伏笔回收偏移**上界**"),
    ("application.world.services.foreshadow_pacing", "RESOLVE_OFFSET_MIN", 2, "伏笔回收偏移**下界**"),
    ("application.world.services.foreshadow_pacing", "SOURCE_FALLBACK", 'fallback', "「走了兜底」的来源标记（与真实来源必须可分辨）"),
    ("application.world.services.foreshadow_selection_schema", "SOURCE_FALLBACK", 'fallback', "「走了兜底」的来源标记（与真实来源必须可分辨）"),
    ("application.world.services.foreshadow_selection_schema", "SOURCE_NONE", '', "「未评估」的**三态语义标记**（空/标记串被换掉 ⇒ 下游把未评估当成真实来源）"),
    ("application.world.services.plot_point_classifier_schema", "SOURCE_NONE", '', "「未评估」的**三态语义标记**（空/标记串被换掉 ⇒ 下游把未评估当成真实来源）"),
    ("application.world.services.style_preset_match_schema", "SOURCE_NONE", '', "「未评估」的**三态语义标记**（空/标记串被换掉 ⇒ 下游把未评估当成真实来源）"),
    ("application.world.services.style_preset_matcher", "DEFAULT_TIMEOUT_SECONDS", 30, "AI 调用的默认超时（秒）"),
    ("domain.ai.services.llm_service", "DEFAULT_MAX_OUTPUT_TOKENS", 120000, "模型输出 token **地板**（用户拍板保留：挡的是「向下」）"),
    ("domain.novel.value_objects.foreshadowing", "CORE_EMOTIONAL_WEIGHT_THRESHOLD", 0.8, "核心情绪权重阈值（0.8）"),
    ("domain.novel.value_objects.storyline_end_source", "SOURCE_FALLBACK", 'fallback', "「走了兜底」的来源标记（与真实来源必须可分辨）"),
    ("engine.core.services.context_budget_limits", "DEFAULT_CONTEXT_MAX_TOKENS", 60000, "上下文预算**唯一**数字（60000；改它必须显式改守卫）"),
    ("engine.runtime.audit_delegate", "AFTERMATH_TIMEOUT_DEFAULT", {'drift_alert': False, 'similarity_score': None, 'narrative_sync_ok': None, 'vector_stored': False, 'foreshadow_stored': False, 'triples_extracted': False}, "章后链路的**默认值**（未评估 ⇒ None，不是假值）"),
    ("engine.runtime.legacy_writing_delegate", "BUFFER_TENSION_THRESHOLD", 80, "余韵章的张力判定阈值（80）"),
    ("infrastructure.ai.prompt_keys", "PLANNING_CONTEXT_WEIGHTS", 'planning-context-weights', "提示词键名（CPMS 契约的一部分）"),
)


def _resolve(module_name: str, const_name: str):
    module = importlib.import_module(module_name)
    return getattr(module, const_name)


def test_every_pinned_semantic_constant_keeps_its_literal_value():
    """⚠ 钉死表：每个语义常量的**字面量**必须一字不差。

    ⚠ 这正是那两条空洞守卫缺的东西：期望值从被测模块 import 回来比较
    ⇒ 自指恒真 ⇒ 改坏实现没人拦。
    """
    broken: list[str] = []
    for module_name, const_name, expected, why in PINNED:
        try:
            actual = _resolve(module_name, const_name)
        except Exception as exc:  # pragma: no cover —— 常量没了也算破
            broken.append("%s.%s 取不到：%s" % (module_name, const_name, exc))
            continue
        if actual != expected:
            broken.append(
                "%s.%s 现在是 %r，表里钉的是 %r（%s）"
                % (module_name, const_name, actual, expected, why)
            )
    assert not broken, "语义常量的字面量变了（要改先想清楚，它是契约）：\n  " + "\n  ".join(broken)


def _constants_imported_by_tests() -> dict[tuple[str, str], set[str]]:
    """测试从生产模块 import 的 ALL_CAPS 常量 → 用到它的测试文件。"""
    found: dict[tuple[str, str], set[str]] = {}
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith(PROD_HINT)
            ):
                for alias in node.names:
                    if CONST_NAME.match(alias.name):
                        found.setdefault((node.module, alias.name), set()).add(
                            path.relative_to(ROOT).as_posix()
                        )
    return found


def _literal_pins() -> set[tuple[str, str]]:
    """全仓已有的「字面量钉住」：``assert <常量> == <字面量>``。"""
    pins: set[tuple[str, str]] = set()
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        local: dict[str, tuple[str, str]] = {}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith(PROD_HINT)
            ):
                for alias in node.names:
                    if CONST_NAME.match(alias.name):
                        local[alias.asname or alias.name] = (node.module, alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert) or not isinstance(node.test, ast.Compare):
                continue
            if len(node.test.comparators) != 1:
                continue
            left, right = node.test.left, node.test.comparators[0]
            for a, b in ((left, right), (right, left)):
                if isinstance(a, ast.Name) and isinstance(b, ast.Constant):
                    key = local.get(a.id)
                    if key:
                        pins.add(key)
    return pins


def test_every_semantic_constant_used_by_tests_is_pinned_or_exempted():
    """⚠ **通则**：测试用到的语义常量，必须有字面量钉住（或在本文件显式豁免）。

    ⇒ 这一条把「空洞守卫」从**逐条复审**变成**结构性约束**：
    新增一个阈值/标记却忘了登记 ⇒ 这里立刻红。
    """
    imported = _constants_imported_by_tests()
    semantic = {k for k in imported if SEMANTIC_NAME.search(k[1])}
    pins = _literal_pins() | {(m, c) for m, c, _v, _w in PINNED}

    missing = sorted(
        key
        for key in semantic
        if key not in pins and key not in EXEMPTED
    )
    assert not missing, (
        "这些语义常量被测试 import，却**没有任何地方用字面量钉住**"
        "（改掉它们没人拦 ⇒ 空洞守卫）：\n  "
        + "\n  ".join(
            "%s.%s  (%s)"
            % (m, c, ", ".join(sorted(imported[(m, c)]))[:100])
            for m, c in missing
        )
    )


def test_every_exemption_has_a_reason():
    """⚠ 豁免必须写理由（不许裸豁免 ⇒ 否则「豁免」会变成垃圾桶）。"""
    bad = [key for key, reason in EXEMPTED.items() if not str(reason).strip()]
    assert not bad, "这些豁免没写理由：%s" % bad


def test_the_pinned_table_has_no_dead_entries():
    """⚠ 表里的条目必须**真的存在**（删了常量却忘了删表 ⇒ 表会腐烂）。"""
    missing = []
    for module_name, const_name, _expected, _why in PINNED:
        try:
            _resolve(module_name, const_name)
        except Exception:
            missing.append("%s.%s" % (module_name, const_name))
    assert not missing, "钉死表里有取不到的条目（常量被删/改名了？）：%s" % missing
