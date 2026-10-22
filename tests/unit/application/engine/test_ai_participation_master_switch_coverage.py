"""X-02 补强：总闸必须**双向**覆盖 —— 「会打模型 ⇒ 一定接了闸」。

为什么补这个
------------
第 3 批复审（B1）实测发现：X-02 的总闸对本批新增的 AI 能力**完全无效** ——
``AI_PARTICIPATION_FEATURES`` 里只有 5 项，全是本批之前就有的；
本批新增的 10 个 AI 调用点**一个都没登记**，运行时探针显示
「总闸关掉，10/10 仍打模型」。

根因是**守卫只验单向**：原守卫断言「登记了就要接线」
（``wired == set(AI_PARTICIPATION_FEATURES)``），
**不验「新能力必须登记」** ⇒ 新增能力时绿灯照过。

本文件补上反向那一半，并加一层**行为级**验证。

三层守卫
--------
A1  **静态·分类完备**：每个会打模型的生产模块都必须被归入
    「AI 参与能力（可关）」或「核心生成链路（不可关）」**两类之一**。
    ⚠ 归类必须**显式**——新模块没人归类 ⇒ 报红。
A2  **静态·能力必须接闸**：「AI 参与能力」类的每个模块都必须有闸
    （直接调 ``ai_participation_enabled``，或走「settings 模块」间接接闸）。
B   **行为·正反对照**：总闸关闭 ⇒ **模型一次都不许被调用**；
    同一套输入下总闸打开 ⇒ **模型确实被调用**（正对照）。
    ⚠ 没有正对照的话，这条会在「渲染链路本来就坏」时**假绿**。

⚠ 本文件**不**假装覆盖「AI 输出质量」——那需要真实模型，不在本门禁内。
"""
from __future__ import annotations

import ast
import importlib
import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.engine.services.ai_participation_settings import (
    AI_PARTICIPATION_CAPABILITY_MODULES,
    AI_PARTICIPATION_CORE_PIPELINE_MODULES,
    AI_PARTICIPATION_FEATURES,
    AI_PARTICIPATION_SETTINGS_MEDIATED,
    AI_PARTICIPATION_UNGOVERNED_MODULES,
    ai_participation_enabled,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
SCAN_DIRS = ("application", "engine", "interfaces", "infrastructure", "domain")


# ───────────────────────── 扫描生产代码 ─────────────────────────


def _iter_production_modules():
    for dirname in SCAN_DIRS:
        for path in sorted((REPO_ROOT / dirname).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _calls_model(tree: ast.AST) -> bool:
    """这个模块会不会真的发起模型调用？

    判据：**属性调用** ``<名字里带 llm 的东西>.generate(...)``。
    只看 ``ast.Call`` + ``ast.Attribute``，不靠字符串匹配 ⇒
    注释里写 ``llm.generate(...)`` 不会误判。
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "generate":
            continue
        base = func.value
        name = getattr(base, "attr", None) or getattr(base, "id", None)
        if name and "llm" in name.lower():
            return True
    return False


def _wired_feature_ids(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "ai_participation_enabled":
            continue
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            out.add(node.args[0].value)
    return out


def _negated_gate_ids(node: ast.AST) -> set[str]:
    """表达式里「**取反**」的 ``ai_participation_enabled("x")`` 的 id 集合。

    只认取反的形状（``not ai_participation_enabled("x")``）——
    闸门的语义是「关掉 ⇒ 走这个分支 ⇒ 提前返回」。
    """
    out: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.UnaryOp) or not isinstance(child.op, ast.Not):
            continue
        call = child.operand
        if not isinstance(call, ast.Call):
            continue
        name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
        if name != "ai_participation_enabled":
            continue
        if (
            call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ):
            out.add(call.args[0].value)
    return out


def _all_gate_ids(node: ast.AST) -> set[str]:
    """表达式里出现的**全部** ``ai_participation_enabled("x")`` 的 id。"""
    out: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = getattr(child.func, "id", None) or getattr(child.func, "attr", None)
        if name != "ai_participation_enabled":
            continue
        if (
            child.args
            and isinstance(child.args[0], ast.Constant)
            and isinstance(child.args[0].value, str)
        ):
            out.add(child.args[0].value)
    return out


def _has_model_call(node: ast.AST) -> bool:
    """这段代码里有没有「真的发起模型调用」。

    判据：属性调用 ``xxx.generate(...)``，或调用一个**名字里带 llm**的方法
    （如 ``self._score_chapter_llm(...)``）—— 后者是**传递调用**的直接形态。
    """
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if not isinstance(func, ast.Attribute):
            continue
        attr = func.attr
        if attr == "generate":
            return True
        if "llm" in attr.lower():
            return True
    return False


def _short_circuiting_gate_ids(tree: ast.AST) -> set[str]:
    """**闸门真的挡住了模型调用**的 feature id。

    第 3 批二次复审 N1：闸门只写了一句日志、**没有 return** ⇒
    总闸关掉后照样打模型，而日志说「按未评估处理」⇒ **日志在撒谎**。
    ⇒ 「文件里出现过 ``ai_participation_enabled(...)``」这种**存在性**检查
    抓不住它。

    闸门有两种**形状**，都要认：
      - **短路型**：``if not enabled("x"): return <兜底>``（模型调用在 return 之后）
      - **准入型**：``if ... and enabled("x"): <模型调用>``（模型调用在闸门分支内）
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        negated = _negated_gate_ids(node.test)
        if not _all_gate_ids(node.test):
            continue
        # 形状 A：取反的闸门 + body 里有 return
        if any(isinstance(child, ast.Return) for child in ast.walk(node)):
            out |= negated
        # 形状 B：准入的闸门 + body 里有模型调用
        if _has_model_call(node):
            out |= (_all_gate_ids(node.test) - negated)
    return out


def _capability_class_names() -> set[str]:
    """能力模块里定义的**类名**集合（用于识别传递调用）。"""
    names: set[str] = set()
    for rel in AI_PARTICIPATION_CAPABILITY_MODULES:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names.add(node.name)
    return names


def _transitively_model_calling(tree: ast.AST, capability_classes: set[str]) -> bool:
    """这个模块**间接**能不能打到模型？

    第 3 批二次复审 S3：探测器原先只认**直接** ``llm*.generate(...)``
    ⇒ 「本模块不调模型、但调的另一个模块调」的**传递调用**逃过了分类
    （复审的 N3 就是这么漏掉的：``voice_drift_service`` 调
    ``llm_voice_analysis_service``）。

    ⇒ 现在：模块里**引用了任一能力类** ⇒ 视为「传递调用模型」。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in capability_classes:
            return True
        if isinstance(node, ast.Attribute) and node.attr in capability_classes:
            return True
    return False


def _model_calling_modules() -> dict[str, Path]:
    """（直接或传递）会打模型的生产模块 → 路径。"""
    capability_classes = _capability_class_names()
    out: dict[str, Path] = {}
    for path in _iter_production_modules():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if _calls_model(tree) or _transitively_model_calling(tree, capability_classes):
            out[_rel(path)] = path
    return out


# ───────────────────────── A1 · 分类完备 ─────────────────────────


def test_every_model_calling_module_is_classified():
    """每个会打模型的生产模块都必须**被归类**。

    三类：「AI 参与能力」（可关，且必须有闸）、「核心生成链路」（不可关）、
    「已知缺口」（还没接闸，每条都写明为什么）。
    ⚠ 归类必须显式 —— 新增一个会打模型的模块却没人归类 ⇒ 这条报红。
    机械地靠启发式分类做不到（写书主干的模型调用与「可选判断」在代码上同形）。
    """
    classified = (
        AI_PARTICIPATION_CAPABILITY_MODULES
        | AI_PARTICIPATION_CORE_PIPELINE_MODULES
        | set(AI_PARTICIPATION_UNGOVERNED_MODULES)
    )
    unclassified = sorted(set(_model_calling_modules()) - classified)
    assert not unclassified, (
        "这些模块会打模型，却没被归入「AI 参与能力」「核心生成链路」或「已知缺口」：\n"
        + "\n".join(f"  - {rel}" for rel in unclassified)
        + "\n修法：在 ai_participation_settings.py 的三张表里各归一类"
        "（能力类必须接总闸；核心链路类请写清为什么不受总闸管辖）。"
    )


def test_the_known_gaps_are_explicit_and_documented():
    """已知缺口每条都要写清「为什么不接、什么时候接」。

    这张表**只该变短，不该变长** ⇒ 空理由条目会报红。
    """
    for rel, reason in AI_PARTICIPATION_UNGOVERNED_MODULES.items():
        assert (REPO_ROOT / rel).is_file(), f"缺口表指向的模块不存在：{rel}"
        assert len(reason.strip()) >= 20, f"缺口 {rel} 没写清原因"


def test_the_three_classifications_do_not_overlap():
    capability = AI_PARTICIPATION_CAPABILITY_MODULES
    core = AI_PARTICIPATION_CORE_PIPELINE_MODULES
    gaps = set(AI_PARTICIPATION_UNGOVERNED_MODULES)
    assert not (capability & core), "同一个模块不能既是「可关的能力」又是「不可关的核心链路」"
    assert not (capability & gaps), "同一个模块不能既是「已接闸」又是「已知缺口」"
    assert not (core & gaps), "同一个模块不能既是「核心链路」又是「已知缺口」"


def test_classified_modules_actually_exist():
    for rel in (
        AI_PARTICIPATION_CAPABILITY_MODULES | AI_PARTICIPATION_CORE_PIPELINE_MODULES
    ):
        assert (REPO_ROOT / rel).is_file(), f"归类表指向的模块不存在：{rel}"


# ───────────────────────── A2 · 能力类必须接闸 ─────────────────────────


def test_every_capability_module_has_a_gate():
    """「AI 参与能力」类的每个模块都必须接闸。

    这是 B1 的核心：新增一个 AI 能力却忘了登记/接线时，这条守卫必须报红。

    ⚠ 第 3 批二次复审 S1/N1：这条**曾经只是存在性检查**
    （文件里出现过 ``ai_participation_enabled("x")`` 就算过）
    ⇒ 放行了「只打日志、没有 return」的闸门（N1：闸门关掉仍每章打模型）。
    ⇒ 现在要求**闸门后面真的 return**（见 ``_short_circuiting_gate_ids``）。
    """
    mediated = {caller for caller, _holder, _feature in AI_PARTICIPATION_SETTINGS_MEDIATED}
    ungated: list[str] = []
    log_only: list[str] = []

    for rel in sorted(AI_PARTICIPATION_CAPABILITY_MODULES):
        if rel in mediated:
            continue
        path = REPO_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        wired = _wired_feature_ids(tree)
        if not wired:
            ungated.append(rel)
            continue
        short_circuit = _short_circuiting_gate_ids(tree)
        if not (wired & short_circuit):
            log_only.append(rel)

    assert not ungated, (
        "这些「AI 参与能力」模块没接 X-02 总闸 ⇒ 总闸关不掉它们：\n"
        + "\n".join(f"  - {rel}" for rel in ungated)
        + "\n修法：在模块入口加 ai_participation_enabled(\"<feature_id>\")，"
        "并把 feature id 登记进 AI_PARTICIPATION_FEATURES。"
    )
    assert not log_only, (
        "这些模块的闸门**只写了日志、没有 return** ⇒ 总闸关掉后照样打模型"
        "（而日志却说「按未评估处理」⇒ 日志在撒谎）：\n"
        + "\n".join(f"  - {rel}" for rel in log_only)
        + "\n修法：闸门的 if 分支里必须 return。"
    )


def test_every_mediated_entry_is_registered_and_really_read():
    """中介表里的每一项：id 必须在册，且持闸模块真的调了它。

    否则「中介」只是登记在案的空头支票。

    中介三元组 = (会打模型的模块, 持闸的模块, feature id)。
    """
    for caller, holder, feature in AI_PARTICIPATION_SETTINGS_MEDIATED:
        assert feature in AI_PARTICIPATION_FEATURES, (
            f"{caller} mediated 的 {feature!r} 不在 AI_PARTICIPATION_FEATURES 里"
        )
        assert caller in AI_PARTICIPATION_CAPABILITY_MODULES, (
            f"中介表里的调用方 {caller} 不在「AI 参与能力」表里"
        )
        holder_path = REPO_ROOT / holder
        assert holder_path.is_file(), f"中介表指向的持闸模块不存在：{holder}"
        src = holder_path.read_text(encoding="utf-8")
        assert f'ai_participation_enabled("{feature}")' in src, (
            f"{holder} 声称给 {caller} mediated {feature!r}，但源码里没有读总闸的调用"
        )


def test_no_feature_is_mediated_and_directly_gated_at_the_same_time():
    """同一个 feature 不许既**直接接闸**又**走中介**（两条路径容易只改一条）。

    ⚠ 例外：中介三元组里的**持闸模块本身**直接接这个 feature 是正常的
    （那就是同一道闸，不是两条路）—— 第 3 批二次复审 N3 把
    ``voice_llm_mode`` 的闸从 ``base.py`` 收进 ``voice_drift_service.py``
    后就属于这种情况。
    """
    holders = {feature: holder for _caller, holder, feature in AI_PARTICIPATION_SETTINGS_MEDIATED}
    conflicts: list[str] = []
    for rel in sorted(AI_PARTICIPATION_CAPABILITY_MODULES):
        path = REPO_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for feature in _short_circuiting_gate_ids(tree):
            holder = holders.get(feature)
            if holder is None:
                continue
            if holder == rel:
                continue  # 同一道闸
            conflicts.append(f"{feature}: {rel} 与中介持闸方 {holder}")

    assert not conflicts, (
        "这些 feature 既在别处直接接闸、又走中介 ⇒ 关闸有两条路径：\n"
        + "\n".join(f"  - {item}" for item in conflicts)
    )


# ───────────────────────── B · 行为级正反对照 ─────────────────────────


class _FakeGateway:
    """打桩网关：只保证 ``render`` 不炸（行为断言看的是**有没有调模型**）。"""

    def render(self, contract, variables):
        return SimpleNamespace(
            prompt=SimpleNamespace(system="sys", user="usr"),
            node_key=getattr(contract, "node_key", ""),
        )

    def validate_output(self, contract, payload):  # pragma: no cover - 不该走到
        return payload


class _SpyLLM:
    def __init__(self):
        self.calls = 0

    async def generate(self, prompt, config):
        self.calls += 1
        return SimpleNamespace(content="{}")


#: (module, class, method, 真实入参) —— 入参必须是**能让它真的走到模型**的形状，
#: 否则正对照会失败（那就变成「探针无效」而不是「能力没接线」）。
def _emotion_ledger():
    from engine.core.value_objects.emotion_ledger import EmotionLedger

    return EmotionLedger()


def _foreshadow_candidates():
    from application.world.services.foreshadow_selection_schema import ForeshadowCandidate

    return [
        ForeshadowCandidate(id="f1", description="那把剑是谁的", planted_in_chapter=1),
    ]


def _refactor_request():
    from application.audit.dtos.macro_refactor_dto import RefactorProposalRequest

    return RefactorProposalRequest(
        event_id="e1",
        author_intent="想让主角面对选择",
        current_event_summary="主角在城里遇到追兵",
        current_tags=["追兵"],
    )


BEHAVIOUR_CASES = [
    ("application.blueprint.services.structure_proposal", "StructureProposalService", "propose",
     {"novel_id": "n-1", "target_chapters": 300}),
    ("application.blueprint.services.first_act_planner", "FirstActPlanner", "propose",
     {"novel_id": "n-1"}),
    ("application.blueprint.services.target_budget_proposal", "TargetBudgetProposalService", "propose",
     {"novel_id": "n-1"}),
    ("application.blueprint.services.act_volume_placement_service", "ActVolumePlacementService", "declare",
     {"novel_id": "n-1"}),
    ("application.blueprint.services.finale_intent_service", "FinaleIntentService", "resolve",
     {"novel_id": "n-1", "act_number": 1, "total_acts": 3, "target_chapters": 100}),
    ("application.engine.services.planning_depth_assessor", "PlanningDepthAssessor", "assess",
     {"genre": "都市", "premise": "一个故事", "target_chapters": 100}),
    ("application.engine.services.rhythm_quota_planner", "RhythmQuotaPlanner", "plan",
     {"genre": "都市", "chapter_number": 3}),
    ("application.evolution.services.dead_character_checker", "DeadCharacterChecker", "check",
     {"outline": "大纲", "candidates": [{"character_id": "c1", "name": "甲"}]}),
    ("application.character.services.character_ner", "CharacterNerService", "extract",
     {"novel_id": "n-1", "outline": "甲在城里"}),
    ("application.character.services.character_belief_check", "CharacterBeliefChecker", "check",
     {"character_name": "甲", "action": "拔剑"}),
    ("application.character.services.character_context_selection", "CharacterContextSelector", "select",
     {"candidates": [{"character_id": "c1", "name": "甲", "fact_ids": ["f1"]}]}),
    ("application.world.services.plot_point_classifier", "PlotPointClassifier", "classify",
     {"chapter_number": 1}),
    ("application.audit.services.anti_ai_pattern_induction", "AntiAiPatternInducer", "induce",
     {"chapter_number": 1, "content": "一段正文"}),
    ("application.audit.services.conflict_action_extraction", "ConflictActionExtractor", "extract",
     {"outline": "大纲", "entity_names": ["甲"]}),
    ("application.audit.services.wuxia_style_audit", "WuxiaStyleAuditor", "audit",
     {"content": "一段正文"}),
    ("application.audit.services.macro_breakpoint_classifier", "MacroBreakpointClassifier", "classify",
     {"characters": [{"character_id": "c1", "name": "甲"}],
      "events": [{"event_id": "e1", "summary": "遇袭"}]}),
    # ── 二次复审 S2：下面几个此前**没有**行为覆盖 ⇒
    #   N1（只打日志的闸）、N2（空选择覆盖兜底）本可被抓到。
    ("engine.infrastructure.memory.emotion_ledger_extractor", "EmotionLedgerExtractor", "extract_deltas",
     # ⚠ 正文必须 ≥100 字（少于则服务**提前返回**，压根不打模型 ⇒ 正对照会失效）
     {"chapter_content": (
         "甲在城里走了一整章，路上遇见了乙，两人站在巷口说了很久的话，风很大，"
         "甲把手插在袖子里，想着昨夜没有说出口的那句话，又想着明天要不要真的走进"
         "那扇门，脚步慢了下来。乙说城东的桥要拆了，甲说拆了也没关系，反正他"
         "本来就不打算过河。两人又聊起了去年冬天的事，谁也没提自己为什么在这里。"
     ),
      "chapter_number": 3, "current_ledger": _emotion_ledger()}),
    ("application.world.services.foreshadow_selection", "ForeshadowSelectionPlanner", "select",
     {"chapter_number": 3, "candidates": _foreshadow_candidates()}),
    ("application.audit.services.macro_refactor_proposal_service", "MacroRefactorProposalService",
     "generate_proposal",
     {"request": _refactor_request()}),
]


async def _run_service(monkeypatch, module_path: str, class_name: str, method_name: str, kwargs: dict):
    module = importlib.import_module(module_path)
    monkeypatch.setattr(module, "get_prompt_gateway", lambda: _FakeGateway(), raising=False)
    # ⚠ 有的能力走**直连** CPMS 的 ``render_required_prompt``（不是网关）⇒ 也要打桩，
    #    否则渲染会因缺 DB 模板而抛错 ⇒ 探针压根到不了模型调用 ⇒ 正对照失效。
    if hasattr(module, "render_required_prompt"):
        monkeypatch.setattr(
            module,
            "render_required_prompt",
            lambda contract, variables: SimpleNamespace(
                system="sys", user="usr", node_key=getattr(contract, "node_key", "")
            ),
            raising=False,
        )
    # ⚠ 有的能力还有**自己的逐条开关**（如 E-06 的 `rhythm_quota.enabled`，
    #   管「配额文本送不送进正文提示词」）。它关着时规划器**不会打模型**
    #   （第 3 批复审 S13 的修法）⇒ 正对照要把它打开，否则探针无效。
    if hasattr(module, "rhythm_quota_delivery_enabled"):
        monkeypatch.setattr(module, "rhythm_quota_delivery_enabled", lambda: True)
    spy = _SpyLLM()
    service = getattr(module, class_name)(spy)
    try:
        await getattr(service, method_name)(**kwargs)
    except Exception:  # noqa: BLE001
        # 模型调用之后的解析失败不影响「有没有调模型」这个断言；
        # 正对照会把「压根没调到模型」的情况报出来。
        pass
    return spy


def _set_master(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(
        "application.engine.services.ai_participation_settings.get_config",
        lambda: SimpleNamespace(
            ai_participation={"master_enabled": enabled, "disabled_features": []}
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_path,class_name,method_name,kwargs",
    BEHAVIOUR_CASES,
    ids=[f"{c[1]}.{c[2]}" for c in BEHAVIOUR_CASES],
)
async def test_master_switch_off_stops_the_model_call(
    monkeypatch, module_path, class_name, method_name, kwargs
):
    """总闸关闭 ⇒ 这个能力**一次模型都不许调**。

    正对照：同一套输入下总闸打开时**确实**调了模型。
    ⚠ 正对照是这条断言有意义的前提 —— 少了它，
    「渲染链路本来就坏 ⇒ 模型压根没被调」也会让本条变绿。
    """
    _set_master(monkeypatch, True)
    on_spy = await _run_service(
        monkeypatch, module_path, class_name, method_name, kwargs
    )
    assert on_spy.calls >= 1, (
        f"{module_path}.{class_name}.{method_name} 正对照失败："
        "总闸开着时模型也没被调用 ⇒ 这条守卫的探针无效（不是能力没接线）"
    )

    _set_master(monkeypatch, False)
    off_spy = await _run_service(
        monkeypatch, module_path, class_name, method_name, kwargs
    )
    assert off_spy.calls == 0, (
        f"{module_path}.{class_name}.{method_name} 总闸关闭后仍调了模型 "
        f"{off_spy.calls} 次 ⇒ 总闸对它无效"
    )


@pytest.mark.asyncio
async def test_master_switch_off_does_not_block_unrelated_features(monkeypatch):
    """总闸只关「登记在册的能力」，**不是**把整个 AI 通道掐死。

    防止「为了让守卫变绿，把所有能力都登记进去」这种反向作弊。
    """
    _set_master(monkeypatch, False)
    assert ai_participation_enabled("chapter_mode") is False
    # 未登记的 id 一律拒绝（这是设计，不是副作用）
    assert ai_participation_enabled("根本不存在的特征") is False


# ───────────────── 介户型能力：行为落在「effective enabled」上 ─────────────────

#: (settings 模块, feature id) —— 这几项的闸折进各自的 ``enabled`` 字段，
#: 调用方看 ``enabled`` 决定要不要打模型 ⇒ 行为断言落在**开关**上。
SETTINGS_MEDIATED_FEATURES = [
    ("application.engine.services.chapter_mode_settings", "chapter_mode"),
    ("application.engine.services.chapter_target_settings", "chapter_target"),
    ("application.engine.services.context_weight_settings", "context_weights"),
]


@pytest.mark.parametrize("settings_module,feature", SETTINGS_MEDIATED_FEATURES)
def test_the_settings_mediated_features_respect_the_master_switch(
    monkeypatch, settings_module, feature
):
    """介户型能力：总闸关 ⇒ 它们的**有效开关**必须是 False。

    第 3 批二次复审 S2：这几项此前**没有**行为覆盖 ⇒
    「总闸关掉却照样打模型」这类问题抓不到。

    ⚠ 正对照：逐条开关本身开着时，总闸**开着** ⇒ 有效开关必须是 True
    （否则这条断言会恒真）。
    """
    import importlib

    module = importlib.import_module(settings_module)
    getter = next(
        name for name in dir(module)
        if name.startswith("get_") and name.endswith("_runtime_settings")
    )
    section_name = feature

    # 正对照：逐条开关开 + 总闸开 ⇒ 有效开关 True
    monkeypatch.setattr(
        module, "get_config",
        lambda: SimpleNamespace(
            **{section_name: {"enabled": True}},
            ai_participation={"master_enabled": True},
        ),
    )
    monkeypatch.setattr(
        "application.engine.services.ai_participation_settings.get_config",
        lambda: SimpleNamespace(ai_participation={"master_enabled": True}),
    )
    assert getattr(module, getter)().enabled is True, "正对照失败：逐条开关本身没生效"

    # 总闸关 ⇒ 有效开关必须 False
    monkeypatch.setattr(
        module, "get_config",
        lambda: SimpleNamespace(
            **{section_name: {"enabled": True}},
            ai_participation={"master_enabled": False},
        ),
    )
    monkeypatch.setattr(
        "application.engine.services.ai_participation_settings.get_config",
        lambda: SimpleNamespace(ai_participation={"master_enabled": False}),
    )
    assert getattr(module, getter)().enabled is False, (
        f"{feature} 的有效开关没受总闸约束 ⇒ 总闸关掉它照样打模型"
    )


def test_the_feature_table_has_no_duplicates():
    ids = list(AI_PARTICIPATION_FEATURES)
    assert len(ids) == len(set(ids)), f"AI_PARTICIPATION_FEATURES 有重复项：{ids}"


def test_the_features_without_a_fallback_are_declared_and_documented():
    """「关闸 ⇒ 回到改造前行为」这句话**对少数能力不成立** ⇒ 必须显式声明。

    第 3 批三次复审 R4：``plot_point_classifier``（F-08）与
    ``character_belief_check``（H-03）在改造时**刻意删掉了代码侧替代物**
    （阈值表 / 关键词表）⇒ 没有可回落的东西 ⇒ 关闸 = 该能力**消失**。

    ⇒ 这份名单必须：
    1. 全部在册（不许写不存在的 id）；
    2. 在总闸模块的 docstring 里被**点名**（否则下一个人读不到）。
    """
    from application.engine.services.ai_participation_settings import (
        FEATURES_WITHOUT_CODE_FALLBACK,
    )

    for feature in FEATURES_WITHOUT_CODE_FALLBACK:
        assert feature in AI_PARTICIPATION_FEATURES, (
            f"{feature} 不在 AI_PARTICIPATION_FEATURES 里"
        )

    settings_source = (
        REPO_ROOT / "application/engine/services/ai_participation_settings.py"
    ).read_text(encoding="utf-8")
    for feature in FEATURES_WITHOUT_CODE_FALLBACK:
        assert feature in settings_source, (
            f"{feature} 没在总闸模块里被点名 ⇒ 「关闸=回到改造前行为」的表述会误导"
        )


def test_the_feature_table_ids_are_snake_case():
    """id 命名约定：snake_case（配置里要手写，别让拼写错误混进来）。"""
    bad = [f for f in AI_PARTICIPATION_FEATURES if not re.fullmatch(r"[a-z][a-z0-9_]*", f)]
    assert not bad, f"feature id 不符合 snake_case 约定：{bad}"
