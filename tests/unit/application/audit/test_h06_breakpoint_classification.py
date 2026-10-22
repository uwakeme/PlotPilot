"""H-06 子项 A：宏观诊断的**断点分类**交 AI；「判不出」不再被说成「人设崩塌」。

改造前
------
1. **发现**靠 ``TRAIT_CONFLICT_RULES`` —— 一张写死 4 个人设标签的标签求交表；
   表外的人设**一个断点都发现不了**，而生产路径恰好只扫那 4 个标签
   ⇒ 诊断对象与本书角色无关。
2. **分类**靠 ``_classify_breakpoint`` 的 scar / 执念两条规则，
   **其余一律落 OOC** —— 而「其余」包含四种**信息不足**的情形
   （没有人物状态仓储 / 提不出角色 / 没有角色状态 / 没找到触发原因）
   ⇒ **「判不出」被说成「人设崩塌」**（落库后的假结论）。

改造后
------
· 新 CPMS 节点 ``planning-macro-breakpoint``：AI 看角色档案（人设 / 伤疤 / 执念）
  与最近的事件清单，判「是不是偏离、属于哪一类」；
  代码只做**不越界**校验（事件白名单 / 角色白名单 / verdict 封闭枚举 /
  有结论必须有依据）。
· 规则**判不出**的落 ``BreakpointType.UNASSESSED``（未评估），**不落 OOC**。
· AI 不可用 ⇒ 回落规则判定，且来源可分辨（``breakpoint_source``）。

全部是行为断言（结构性的一处用 AST 判定，不做源码文本匹配）。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.audit.dtos.macro_refactor_dto import (
    SOURCE_AI,
    SOURCE_NONE,
    SOURCE_RULE,
    LogicBreakpoint,
)
from application.audit.services.macro_breakpoint_classifier import (
    MacroBreakpointClassifier,
)
from application.audit.services.macro_breakpoint_schema import (
    SOURCE_NONE as SCHEMA_SOURCE_NONE,
    VERDICT_BREAKOUT,
    VERDICT_DEVIATION,
    VERDICT_NOT_DEVIATION,
    VERDICT_SCAR_TRIGGERED,
    VERDICT_UNCLEAR,
    normalize_classification,
)
from application.audit.services.macro_diagnosis_service import MacroDiagnosisService
from application.audit.services.macro_refactor_scanner import MacroRefactorScanner
from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.token_usage import TokenUsage
from domain.novel.value_objects.character_state import (
    BreakpointType,
    CharacterState,
    Motivation,
    Scar,
)

REPO_ROOT = Path(__file__).resolve().parents[4]


# ======================================================================
#  A. 扫描器层：「判不出」落未评估，不落 OOC
# ======================================================================


def _event(
    event_id="ev-1",
    tags=("动机:冲动",),
    summary="挚友死后，纪寒当街动手",
    entity_id=None,
):
    mutations = [{"entity_id": entity_id}] if entity_id else []
    return {
        "event_id": event_id,
        "chapter_number": 41,
        "event_summary": summary,
        "tags": list(tags),
        "mutations": mutations,
    }


def _state_repo(state=None):
    repo = Mock()
    repo.get = Mock(return_value=state)
    repo.get_by_novel = Mock(return_value=[state] if state else [])
    return repo


def _scanner(state=None, with_state_repo=True, events=None):
    event_repo = Mock()
    event_repo.list_up_to_chapter.return_value = events if events is not None else [_event()]
    scanner = MacroRefactorScanner(
        event_repo,
        character_state_repository=_state_repo(state) if with_state_repo else None,
    )
    return scanner


def test_no_character_state_repo_is_unassessed_not_ooc():
    """没有人物状态仓储 ⇒ **判不出** ⇒ 落 UNASSESSED，不落 OOC。"""
    breakpoints = _scanner(with_state_repo=False).scan_breakpoints("n-1", "冷酷")

    assert breakpoints, "规则表里「冷酷」确实有「动机:冲动」⇒ 应该扫出断点"
    assert breakpoints[0].breakpoint_type == BreakpointType.UNASSESSED
    assert breakpoints[0].breakpoint_type != BreakpointType.OOC


def test_character_id_not_extractable_is_unassessed():
    """提不出角色 ⇒ 判不出 ⇒ UNASSESSED。"""
    state = CharacterState(character_id="char-1", novel_id="n-1")
    breakpoints = _scanner(state=state).scan_breakpoints("n-1", "冷酷")

    assert breakpoints[0].breakpoint_type == BreakpointType.UNASSESSED


def test_missing_character_state_is_unassessed():
    """没有角色状态 ⇒ 判不出 ⇒ UNASSESSED。

    ⚠ 事件**必须带** ``entity_id``：角色提得出来，才走得到
    「没有角色状态」那条分支。否则提不出角色就被另一条未评估分支挡了
    ⇒ 这条守卫会变成空的（变异验证 M3 绿过一轮才发现）。
    """
    scanner = _scanner(state=None, events=[_event(entity_id="char-1")])

    breakpoints = scanner.scan_breakpoints("n-1", "冷酷")

    assert breakpoints[0].breakpoint_type == BreakpointType.UNASSESSED


def test_no_scar_and_no_motivation_is_unassessed():
    """伤疤/执念都没命中 ⇒ **代码判不出**有没有正当理由 ⇒ UNASSESSED。"""
    state = CharacterState(character_id="char-1", novel_id="n-1")
    scanner = _scanner(state=state, events=[_event(entity_id="char-1")])

    breakpoints = scanner.scan_breakpoints("n-1", "冷酷")

    assert breakpoints[0].breakpoint_type == BreakpointType.UNASSESSED


def test_scar_triggered_is_still_judged_by_the_rule_and_sourced_as_rule():
    """伤疤命中 ⇒ 规则判得出的仍然是规则判的（来源 = rule）。"""
    state = CharacterState(
        character_id="char-1",
        novel_id="n-1",
        scars=[Scar(impact="对信任极度恐惧", sensitivity_tags=["动机:冲动"], intensity=9)],
    )
    scanner = _scanner(state=state, events=[_event(entity_id="char-1")])

    breakpoints = scanner.scan_breakpoints("n-1", "冷酷")

    assert breakpoints[0].breakpoint_type == BreakpointType.SCAR_TRIGGERED
    assert breakpoints[0].breakpoint_source == SOURCE_RULE


def test_high_priority_motivation_is_still_breakout():
    """高优先级执念 ⇒ 突破（规则判得出的仍然是规则判的）。"""
    state = CharacterState(
        character_id="char-1",
        novel_id="n-1",
        motivations=[Motivation(description="为赵宇复仇", priority=9)],
    )
    scanner = _scanner(state=state, events=[_event(entity_id="char-1")])

    breakpoints = scanner.scan_breakpoints("n-1", "冷酷")

    assert breakpoints[0].breakpoint_type == BreakpointType.CHARACTER_BREAKOUT
    assert breakpoints[0].breakpoint_source == SOURCE_RULE


def test_unassessed_breakpoint_is_not_sourced_as_a_verdict():
    """未评估的断点，来源必须是「未评估」，不能看着像有结论。"""
    breakpoints = _scanner(with_state_repo=False).scan_breakpoints("n-1", "冷酷")

    assert breakpoints[0].breakpoint_source == SOURCE_NONE
    assert breakpoints[0].breakpoint_source != SOURCE_RULE
    assert breakpoints[0].breakpoint_source != SOURCE_AI


def test_unassessed_reason_says_it_is_not_a_conclusion():
    """未评估的断点，描述里必须说清「不下结论」——不许写成「崩塌」。"""
    breakpoints = _scanner(with_state_repo=False).scan_breakpoints("n-1", "冷酷")

    reason = breakpoints[0].reason
    assert "未评估" in reason
    assert "无合理原因的偏离" not in reason


# ======================================================================
#  B. schema 层：白名单 / 封闭枚举 / 有结论必须有依据
# ======================================================================


def _norm(verdicts, events=("ev-1",), characters=("char-1",)):
    return normalize_classification(
        {"verdicts": verdicts},
        valid_event_ids=list(events),
        valid_character_ids=list(characters),
    )


def test_ai_cannot_invent_events():
    """AI 不得凭空造事件（不在候选清单里的 event_id 一律丢弃）。"""
    result = _norm(
        [
            {
                "event_id": "ev-not-in-list",
                "character_id": "char-1",
                "verdict": VERDICT_DEVIATION,
                "reason": "编出来的事件",
            }
        ]
    )

    assert result.source == SCHEMA_SOURCE_NONE
    assert result.verdicts == {}


def test_ai_cannot_invent_characters():
    """AI 不得凭空造人（不在角色清单里的 character_id 一律丢弃）。"""
    result = _norm(
        [
            {
                "event_id": "ev-1",
                "character_id": "char-not-in-list",
                "verdict": VERDICT_DEVIATION,
                "reason": "编出来的角色",
            }
        ]
    )

    assert result.source == SCHEMA_SOURCE_NONE
    assert result.verdicts == {}


def test_verdict_must_be_in_the_closed_enum():
    """verdict 必须是封闭枚举里的那五个之一。"""
    result = _norm(
        [
            {
                "event_id": "ev-1",
                "character_id": "char-1",
                "verdict": "崩塌了",
                "reason": "不在枚举里",
            }
        ]
    )

    assert result.source == SCHEMA_SOURCE_NONE


def test_a_verdict_without_a_reason_is_rejected():
    """有结论必须有依据（reason 为空 ⇒ 丢弃）。"""
    result = _norm(
        [
            {
                "event_id": "ev-1",
                "character_id": "char-1",
                "verdict": VERDICT_DEVIATION,
                "reason": "",
            }
        ]
    )

    assert result.source == SCHEMA_SOURCE_NONE


def test_unclear_verdict_may_have_no_reason():
    """``unclear`` 是「不下结论」，允许没有依据。"""
    result = _norm(
        [
            {
                "event_id": "ev-1",
                "character_id": "char-1",
                "verdict": VERDICT_UNCLEAR,
                "reason": "",
            }
        ]
    )

    assert result.source == "ai"
    assert result.verdicts["ev-1"].verdict == VERDICT_UNCLEAR


def test_ai_saying_no_deviation_still_counts_as_answered():
    """AI 合法地说「都不偏离」⇒ 算判过了（**不回退**）。

    「模型说没有」≠「没评估」。
    """
    result = _norm(
        [
            {
                "event_id": "ev-1",
                "character_id": "char-1",
                "verdict": VERDICT_NOT_DEVIATION,
                "reason": "摘要只说他在处理公务",
            }
        ]
    )

    assert result.source == "ai"
    assert result.verdicts["ev-1"].verdict == VERDICT_NOT_DEVIATION
    assert result.deviation_event_ids() == []


# ======================================================================
#  C. 接线层：AI 的判定说了算，规则兜底
# ======================================================================


class _FakeGateway:
    """打桩网关：**校验**仍走真实契约（不变量是真被测的）。"""

    def __init__(self):
        self.variables = {}
        #: 渲染过的契约（用来断言「某个节点根本没被调用」）
        self.rendered_contracts = []

    def render(self, contract, variables):
        self.variables = dict(variables)
        self.rendered_contracts.append(contract)
        return SimpleNamespace(
            prompt=SimpleNamespace(system="sys", user="usr"),
            node_key=contract.node_key,
        )

    def validate_output(self, contract, payload):
        return contract.output_schema.model_validate(payload)


class _FakeScanner:
    def __init__(self, breakpoints=None, events=None, characters=None):
        self._bps = breakpoints if breakpoints is not None else []
        self._events = events if events is not None else []
        self._characters = characters if characters is not None else []

    def scan_breakpoints(self, novel_id, trait, conflict_tags=None):
        return list(self._bps)

    def list_candidate_events(self, novel_id, limit=30):
        return list(self._events)

    def list_character_profiles(self, novel_id):
        return list(self._characters)


def _bible_repo():
    char = SimpleNamespace(
        character_id=SimpleNamespace(value="char-1"),
        name="纪寒",
        personality="冷酷",
        description="背负旧仇的冷面剑客",
        core_belief="不信任何人",
    )
    repo = Mock()
    repo.get_by_novel_id.return_value = SimpleNamespace(characters=[char])
    return repo


def _llm(payload):
    """同一个桩服务两次 LLM 调用（分类 + 校准）。

    载荷同时满足两个契约：分类读 ``verdicts``，校准读 ``calibration``。
    """
    llm = Mock()
    llm.generate = AsyncMock(
        return_value=GenerationResult(
            json.dumps(payload, ensure_ascii=False),
            TokenUsage(input_tokens=1, output_tokens=1),
        )
    )
    return llm


def _service(scanner, llm, bible_repository=None):
    db = Mock()
    db.get_connection.return_value = Mock()
    db.execute = Mock()
    return MacroDiagnosisService(
        db,
        scanner,
        llm_service=llm,
        bible_repository=bible_repository,
    )


def _rule_breakpoint(event_id="ev-1", bp_type=BreakpointType.UNASSESSED):
    return LogicBreakpoint(
        event_id=event_id,
        chapter=41,
        reason="疑似冲突",
        tags=["动机:冲动"],
        breakpoint_type=bp_type,
        event_summary="挚友死后，纪寒当街动手",
        breakpoint_source=SOURCE_NONE,
    )


@pytest.fixture
def stub_gateways(monkeypatch):
    import application.audit.services.macro_breakpoint_classifier as classifier_mod
    import application.audit.services.macro_calibration as calibration_mod

    gateway = _FakeGateway()
    monkeypatch.setattr(classifier_mod, "get_prompt_gateway", lambda: gateway)
    monkeypatch.setattr(calibration_mod, "get_prompt_gateway", lambda: gateway)
    return gateway


@pytest.mark.asyncio
async def test_ai_verdict_overrides_the_rule_verdict(stub_gateways):
    """AI 判成「真崩塌」⇒ 断点类型用 AI 的，来源标 ai。"""
    scanner = _FakeScanner(
        breakpoints=[_rule_breakpoint()],
        events=[{"event_id": "ev-1", "chapter": 41, "summary": "当街动手", "tags": []}],
    )
    llm = _llm(
        {
            "verdicts": [
                {
                    "event_id": "ev-1",
                    "character_id": "char-1",
                    "verdict": VERDICT_DEVIATION,
                    "reason": "他一向冷静，这里没有任何理由",
                }
            ],
            "calibration": "",
            "reason": "",
        }
    )
    service = _service(scanner, llm, bible_repository=_bible_repo())

    result = await service.run_full_diagnosis_with_calibration("n-1", "测试触发")

    assert len(result.breakpoints) == 1
    assert result.breakpoints[0].breakpoint_type == BreakpointType.OOC
    assert result.breakpoints[0].breakpoint_source == SOURCE_AI


@pytest.mark.asyncio
async def test_ai_not_deviation_removes_the_breakpoint(stub_gateways):
    """AI 判成「不偏离」⇒ 这条**不是**断点（规则说的 OOC 也不算数）。"""
    scanner = _FakeScanner(
        breakpoints=[_rule_breakpoint(bp_type=BreakpointType.OOC)],
        events=[{"event_id": "ev-1", "chapter": 41, "summary": "当街动手", "tags": []}],
    )
    llm = _llm(
        {
            "verdicts": [
                {
                    "event_id": "ev-1",
                    "character_id": "char-1",
                    "verdict": VERDICT_NOT_DEVIATION,
                    "reason": "摘要里看不出与人设冲突",
                }
            ],
            "calibration": "",
            "reason": "",
        }
    )
    service = _service(scanner, llm, bible_repository=_bible_repo())

    result = await service.run_full_diagnosis_with_calibration("n-1", "测试触发")

    assert result.breakpoints == []


@pytest.mark.asyncio
async def test_ai_unclear_is_not_a_breakpoint_either(stub_gateways):
    """AI 说「判不出来」⇒ 也不是断点（但要**如实**落在「未评估」语义上）。"""
    scanner = _FakeScanner(
        breakpoints=[_rule_breakpoint()],
        events=[{"event_id": "ev-1", "chapter": 41, "summary": "…", "tags": []}],
    )
    llm = _llm(
        {
            "verdicts": [
                {
                    "event_id": "ev-1",
                    "character_id": "char-1",
                    "verdict": VERDICT_UNCLEAR,
                    "reason": "摘要太短",
                }
            ],
            "calibration": "",
            "reason": "",
        }
    )
    service = _service(scanner, llm, bible_repository=_bible_repo())

    result = await service.run_full_diagnosis_with_calibration("n-1", "测试触发")

    assert result.breakpoints == []


@pytest.mark.asyncio
async def test_ai_unavailable_keeps_the_rule_verdict(stub_gateways):
    """AI 不可用 ⇒ 沿用规则判定，来源保持规则/未评估（可分辨）。"""
    scanner = _FakeScanner(
        breakpoints=[_rule_breakpoint(bp_type=BreakpointType.SCAR_TRIGGERED)],
        events=[{"event_id": "ev-1", "chapter": 41, "summary": "…", "tags": []}],
    )
    llm = Mock()
    llm.generate = AsyncMock(side_effect=RuntimeError("upstream 502"))
    service = _service(scanner, llm, bible_repository=_bible_repo())

    result = await service.run_full_diagnosis_with_calibration("n-1", "测试触发")

    assert len(result.breakpoints) == 1
    assert result.breakpoints[0].breakpoint_type == BreakpointType.SCAR_TRIGGERED
    assert result.breakpoints[0].breakpoint_source == SOURCE_NONE


@pytest.mark.asyncio
async def test_ai_only_breakpoint_is_added(stub_gateways):
    """AI 判出规则没找到的偏离 ⇒ 补一条新断点（发现不再被 4 标签表卡死）。"""
    scanner = _FakeScanner(
        breakpoints=[],
        events=[
            {"event_id": "ev-9", "chapter": 52, "summary": "他突然慈悲", "tags": []}
        ],
    )
    llm = _llm(
        {
            "verdicts": [
                {
                    "event_id": "ev-9",
                    "character_id": "char-1",
                    "verdict": VERDICT_SCAR_TRIGGERED,
                    "reason": "旧伤被触发",
                }
            ],
            "calibration": "",
            "reason": "",
        }
    )
    service = _service(scanner, llm, bible_repository=_bible_repo())

    result = await service.run_full_diagnosis_with_calibration("n-1", "测试触发")

    assert len(result.breakpoints) == 1
    bp = result.breakpoints[0]
    assert bp.event_id == "ev-9"
    assert bp.chapter == 52
    assert bp.breakpoint_type == BreakpointType.SCAR_TRIGGERED
    assert bp.breakpoint_source == SOURCE_AI


@pytest.mark.asyncio
async def test_same_event_matching_several_traits_is_reported_once(stub_gateways):
    """同一条事件撞上多个人设标签 ⇒ 只报一次（breakpoint_count 不虚高）。

    ``TRAIT_CONFLICT_RULES`` 的四张表互相重叠（「行为:鲁莽」既在「冷酷」也在
    「谨慎」；「情绪:激动」同时在「冷酷」「理性」「温和」）⇒ 旧实现会把同一事件
    重复报 2~3 次，AI 的逐事件判定也对不上。
    """
    scanner = _FakeScanner(
        breakpoints=[_rule_breakpoint()],
        events=[],
    )
    llm = _llm({"verdicts": [], "calibration": "", "reason": ""})
    service = _service(scanner, llm, bible_repository=_bible_repo())

    result = await service.run_full_diagnosis_with_calibration("n-1", "测试触发")

    # 扫描会对 4 个人设标签各调一次；没有去重就会得到 4 条同 event_id 的断点
    assert len(result.breakpoints) == 1
    assert result.breakpoints[0].event_id == "ev-1"


@pytest.mark.asyncio
async def test_no_character_profiles_means_the_classifier_is_not_called(stub_gateways):
    """没有角色档案 ⇒ **不调**分类（不拿空角色表凑一个「看起来判过了」的结论）。

    而且角色白名单为空时，AI 返回的任何 ``character_id`` 都会被丢弃
    ⇒ 归属信息会**静默**丢失 ⇒ 这种情况下宁可判为未评估。
    """
    scanner = _FakeScanner(
        breakpoints=[_rule_breakpoint()],
        events=[{"event_id": "ev-1", "chapter": 41, "summary": "…", "tags": []}],
    )
    llm = _llm({"verdicts": [], "calibration": "", "reason": ""})
    db = Mock()
    db.get_connection.return_value = Mock()
    db.execute = Mock()
    service = MacroDiagnosisService(db, scanner, llm_service=llm, bible_repository=None)

    result = await service.run_full_diagnosis_with_calibration("n-1", "测试触发")

    from infrastructure.ai.prompt_contracts.macro_breakpoint import (
        MACRO_BREAKPOINT_CONTRACT,
    )

    rendered = {c.node_key for c in stub_gateways.rendered_contracts}
    assert "planning-macro-breakpoint" not in rendered, (
        "没有角色档案时不该调分类节点"
    )
    assert MACRO_BREAKPOINT_CONTRACT.node_key == "planning-macro-breakpoint"
    assert len(result.breakpoints) == 1
    assert result.breakpoints[0].breakpoint_source == SOURCE_NONE


@pytest.mark.asyncio
async def test_breakpoint_outside_the_ai_window_keeps_the_rule_verdict(stub_gateways):
    """AI 没看到的事件（候选窗口之外）⇒ 保留规则判定，不被抹掉。"""
    scanner = _FakeScanner(
        breakpoints=[_rule_breakpoint(event_id="ev-old")],
        events=[{"event_id": "ev-1", "chapter": 41, "summary": "…", "tags": []}],
    )
    llm = _llm(
        {
            "verdicts": [
                {
                    "event_id": "ev-1",
                    "character_id": "char-1",
                    "verdict": VERDICT_BREAKOUT,
                    "reason": "有正当理由",
                }
            ],
            "calibration": "",
            "reason": "",
        }
    )
    service = _service(scanner, llm, bible_repository=_bible_repo())

    result = await service.run_full_diagnosis_with_calibration("n-1", "测试触发")

    ids = {bp.event_id for bp in result.breakpoints}
    assert "ev-old" in ids


# ======================================================================
#  D. 节点注册 + 提示词包
# ======================================================================


def test_prompt_key_and_package_are_registered():
    """新节点 key 必须登记 + 有内置包（否则 seed 同步不到，调用必失败）。"""
    from infrastructure.ai.prompt_keys import ALL_KEYS, PLANNING_MACRO_BREAKPOINT
    from infrastructure.ai.prompt_seed.loader import load_seed_bundle

    assert PLANNING_MACRO_BREAKPOINT in ALL_KEYS
    _, prompts = load_seed_bundle()
    package_ids = {p.get("id") for p in prompts}
    assert PLANNING_MACRO_BREAKPOINT in package_ids


def test_classifier_calls_the_new_node(monkeypatch):
    """分类必须走新节点（不是别的节点名）。"""
    from infrastructure.ai.prompt_contracts.macro_breakpoint import (
        MACRO_BREAKPOINT_CONTRACT,
    )

    assert MACRO_BREAKPOINT_CONTRACT.node_key == "planning-macro-breakpoint"


def test_production_construction_sites_pass_the_bible_repository():
    """所有生产构造点都要传 ``bible_repository``。

    不传 ⇒ AI 拿不到角色的人设描述 ⇒ 分类通道等于白做
    （``character_states`` 那张表只有带伤疤/执念的角色才有行）。
    这正是 X-03 踩过的坑：节点「接上了」但从没真跑过。
    """
    missing = []
    for rel in ("application", "engine", "interfaces"):
        for path in (REPO_ROOT / rel).rglob("*.py"):
            try:
                tree = ast.parse(path.read_bytes().decode("utf-8"))
            except Exception:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name != "MacroDiagnosisService":
                    continue
                kwargs = {kw.arg for kw in node.keywords}
                if "bible_repository" not in kwargs:
                    missing.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not missing, "这些构造点没传 bible_repository：\n" + "\n".join(missing)


def test_no_rule_table_discovery_left_in_the_ai_path():
    """AI 通道的候选池**不经过** 4 标签规则表（AST 判调用，不判文本）。

    ⚠ 不用源码 ``contains`` —— 文件里的说明注释会提到那张表，
    文本匹配会**命中自己的注释** ⇒ 恒绿。
    """
    source = (
        REPO_ROOT / "application/audit/services/macro_diagnosis_service.py"
    ).read_bytes().decode("utf-8")
    tree = ast.parse(source)

    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name:
                called.add(name)

    assert "list_candidate_events" in called, (
        "AI 通道的候选事件必须整批交给 AI（不能被规则表筛过）"
    )
    assert "list_character_profiles" in called, (
        "AI 通道必须拿到角色档案"
    )
