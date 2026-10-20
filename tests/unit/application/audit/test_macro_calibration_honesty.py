"""H-06：宏观诊断的叙事校准不许由代码代写；断点判定必须有人物状态仓储。

改造前
------
1. ``macro_diagnosis_service.build_silent_context_patch`` 用**代码写死的三套模板句**
   生成「系统叙事校准」短指令，落进 ``macro_diagnosis_results.context_patch``，
   再由 ``context_budget_allocator`` 读出来静默注入生成上下文头部 ——
   「后续生成须强化「X」所要求的行为倾向」「保持情绪张力，不要强行拉回原人设」
   「应延续情绪强度，不要立即恢复冷静」。**代码替编辑决定了接下来怎么写。**
2. ``MacroRefactorScanner`` 在三处生产构造点（daemon_host / dependencies ×2）
   **都没传** ``character_state_repository`` ⇒ Scar 感知与执念判定永不触发
   ⇒ 所有断点一律被判成 OOC（Breakout / Scar 两套判定空转）。

改造后
------
· 校准指令由 LLM 写（``planning-macro-calibration``）；LLM 没给 / 调用失败
  ⇒ **不注入任何东西**（代码不代写）。
· scanner 三处构造点补传人物状态仓储。

全部是行为断言（结构性的两处用 AST 精确判定，不做文本匹配）。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.audit.dtos.macro_refactor_dto import LogicBreakpoint
from application.audit.services.macro_diagnosis_service import MacroDiagnosisService
from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.token_usage import TokenUsage

REPO_ROOT = Path(__file__).resolve().parents[4]


def _breakpoint(**kw):
    payload = {
        "event_id": "ev-1",
        "chapter": 41,
        "reason": "与「冷酷」人设冲突",
        "tags": ["动机:冲动"],
        "event_summary": "挚友死后，纪寒当街动手",
    }
    payload.update(kw)
    return LogicBreakpoint(**payload)


class _FakeScanner:
    def __init__(self, breakpoints=None):
        self._bps = breakpoints if breakpoints is not None else [_breakpoint()]
        self.calls = []

    def scan_breakpoints(self, novel_id, trait, conflict_tags=None):
        self.calls.append((novel_id, trait, conflict_tags))
        return list(self._bps)


def _service(scanner=None, llm_service=None):
    db = Mock()
    db.get_connection.return_value = Mock()
    return MacroDiagnosisService(db, scanner or _FakeScanner(), llm_service=llm_service)


def _llm(payload):
    llm = Mock()
    llm.generate = AsyncMock(
        return_value=GenerationResult(
            json.dumps(payload, ensure_ascii=False),
            TokenUsage(input_tokens=1, output_tokens=1),
        )
    )
    return llm


def _saved_patch(service, **kwargs):
    """跑一次诊断并取回落库的 context_patch。"""
    calls = []
    service.db.execute = Mock(side_effect=lambda sql, params: calls.append(params))
    return service, calls


# ======================================================================
#  A. 校准指令只来自 LLM
# ======================================================================


async def test_llm_calibration_reaches_the_stored_context_patch():
    """LLM 写的校准指令就是落库的 context_patch。"""
    llm = _llm({"calibration": "第 41 章起不要把他拉回冷酷……", "reason": "伤疤触发"})
    service = _service(llm_service=llm)
    calls = []
    service.db.execute = Mock(side_effect=lambda sql, params: calls.append(params))

    await service.run_full_diagnosis_with_calibration("n-1", "测试触发")

    # INSERT 参数顺序：id, novel_id, trigger_reason, trait, conflict_tags,
    #                  breakpoints, breakpoint_count, status, error_message,
    #                  context_patch, total_words_at_run
    patch = calls[0][9]
    assert "第 41 章起不要把他拉回冷酷" in str(patch)


async def test_llm_failure_means_no_context_patch():
    """判定调用失败 ⇒ 不注入任何东西（代码不代写）。"""
    llm = Mock()
    llm.generate = AsyncMock(side_effect=RuntimeError("upstream 502"))
    service = _service(llm_service=llm)
    calls = []
    service.db.execute = Mock(side_effect=lambda sql, params: calls.append(params))

    await service.run_full_diagnosis_with_calibration("n-1", "测试触发")

    patch = calls[0][9]
    assert not patch or patch is None


async def test_undecided_llm_means_no_context_patch():
    """LLM 判不出来（空 calibration）⇒ 不注入。"""
    llm = _llm({"calibration": "", "reason": "信息不足，未给校准"})
    service = _service(llm_service=llm)
    calls = []
    service.db.execute = Mock(side_effect=lambda sql, params: calls.append(params))

    await service.run_full_diagnosis_with_calibration("n-1", "测试触发")

    patch = calls[0][9]
    assert not patch or patch is None


def test_sync_diagnosis_writes_no_context_patch():
    """同步版（没有 LLM）⇒ 落库 context_patch 为空。

    它不再用代码代写的模板句生成校准。
    """
    service = _service()
    calls = []
    service.db.execute = Mock(side_effect=lambda sql, params: calls.append(params))

    service.run_full_diagnosis("n-1", "测试触发")

    patch = calls[0][9]
    assert not patch or patch is None


def test_code_written_calibration_generator_is_gone():
    """代码代写校准句的生成器必须已删除（否则它还会被用回）。"""
    import application.audit.services.macro_diagnosis_service as mod

    assert not hasattr(mod, "build_silent_context_patch"), (
        "代码代写的校准句生成器还在 —— 它随时可能被重新接回去"
    )


def test_code_written_template_phrases_are_not_in_the_stored_patch():
    """落库的校准指令里不得出现那三句代码代写的话。"""
    service = _service()
    calls = []
    service.db.execute = Mock(side_effect=lambda sql, params: calls.append(params))

    service.run_full_diagnosis("n-1", "测试触发")

    patch = str(calls[0][9] or "")
    for phrase in (
        "后续生成须强化",
        "保持情绪张力，不要强行拉回原人设",
        "应延续情绪强度，不要立即恢复冷静",
    ):
        assert phrase not in patch, f"校准指令里又出现了代码代写的话：{phrase!r}"


# ======================================================================
#  B. 断点判定必须有人物状态仓储
# ======================================================================


def _production_files():
    for rel in ("application", "engine", "interfaces"):
        for path in (REPO_ROOT / rel).rglob("*.py"):
            yield path


def test_scanner_construction_sites_pass_character_state_repository():
    """所有生产构造点都要传人物状态仓储。

    不传 ⇒ Scar 感知与执念判定永不触发 ⇒ 断点一律判成 OOC。
    """
    missing = []
    for path in _production_files():
        try:
            tree = ast.parse(path.read_bytes().decode("utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "MacroRefactorScanner":
                continue
            kwargs = {kw.arg for kw in node.keywords}
            if "character_state_repository" not in kwargs:
                missing.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not missing, "这些构造点没传 character_state_repository：\n" + "\n".join(missing)


def test_scanner_fills_the_event_summary_into_the_breakpoint():
    """扫描出来的断点必须带事件摘要 —— LLM 判断语义靠它。"""
    from application.audit.services.macro_refactor_scanner import MacroRefactorScanner

    event_repo = Mock()
    event_repo.list_up_to_chapter.return_value = [
        {
            "event_id": "ev-1",
            "chapter_number": 41,
            "event_summary": "挚友死后，纪寒当街动手",
            "tags": ["动机:冲动"],
        }
    ]
    scanner = MacroRefactorScanner(event_repo)

    breakpoints = scanner.scan_breakpoints("n-1", "冷酷")

    assert breakpoints, "没扫出断点 —— 规则表里「冷酷」确实有「动机:冲动」"
    assert breakpoints[0].event_summary == "挚友死后，纪寒当街动手"


def test_breakpoint_carries_the_event_summary():
    """断点要带事件摘要 —— LLM 判断「崩塌还是伤疤应激」需要看到发生了什么。"""
    bp = _breakpoint()
    assert bp.event_summary == "挚友死后，纪寒当街动手"
    assert bp.to_dict()["event_summary"] == "挚友死后，纪寒当街动手"