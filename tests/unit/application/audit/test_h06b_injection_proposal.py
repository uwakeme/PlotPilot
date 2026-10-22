"""H-06 子项 B：宏观诊断的「系统叙事校准」注入经**提案确认**。

改造前
------
宏观诊断跑完就把校准指令写进 ``macro_diagnosis_results.context_patch``，
由 ``context_budget_allocator._get_diagnosis_breakpoints`` 读出来
**静默注入**生成上下文头部 ⇒ 诊断结论直接改写后续章节怎么写，
作者既**看不到**、也**没机会拒绝**。

登记表点出的两处缺口
--------------------
1. 缺「待确认」状态 —— 诊断结果只有 ``resolved``（已处理 ⇒ 停注入），
   而它与「批准注入」方向**相反** ⇒ 复用会让两件事共用一个布尔，语义打架；
2. 前端 ``macroRefactorApi`` 的 7 个方法**零 UI 消费者** ——
   提案接口建好了，没人看。

改造后
------
- 新列 ``injection_decision``：pending（待确认）/ approved（批准 ⇒ 注入）/
  rejected（否决 ⇒ 不注入）；
- 注入侧只认 ``approved``（受开关控制，**默认关闭** ⇒ 与改造前逐位一致）；
- 前端有面板：先看见校准文本，再批准或否决；
- **不变量「注入 token 上限」**：注入文本一律截到上限。
"""
from __future__ import annotations

import json
import pathlib
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.audit.services.macro_diagnosis_injection import (
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    decision_allows_injection,
    injection_max_chars,
    is_valid_decision,
    macro_diagnosis_injection_enabled,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
PERF = REPO_ROOT / "config/performance.yaml"
MIGRATIONS = REPO_ROOT / "infrastructure/persistence/database/migrations"
FRONTEND = REPO_ROOT / "frontend/src"


def _read(path: pathlib.Path) -> str:
    return path.read_bytes().decode("utf-8")


# =====================================================================
# 一、开关：默认关闭（逐位一致）
# =====================================================================


def test_the_injection_switch_defaults_to_off():
    """注入开关**默认关闭** ⇒ 与改造前逐位一致（照旧静默注入）。"""
    assert macro_diagnosis_injection_enabled() is False


def test_the_switch_lives_in_performance_yaml():
    text = _read(PERF)
    assert re.search(
        r"macro_diagnosis_injection:\s*\n\s*enabled:\s*false", text
    ), "注入开关的默认值必须是 false（关）"
    assert re.search(
        r"macro_diagnosis_injection:[\s\S]*?max_chars:\s*\d+", text
    ), "注入上限必须可配（登记表的不变量「注入 token 上限」）"


# =====================================================================
# 二、决定词表：方向必须与 resolved 相反且不可混用
# =====================================================================


def test_only_approved_and_rejected_are_valid_decisions():
    """合法决定只有两个；``pending`` 是初始状态，不是决定。"""
    assert is_valid_decision(DECISION_APPROVED) is True
    assert is_valid_decision(DECISION_REJECTED) is True
    assert is_valid_decision(DECISION_PENDING) is False
    assert is_valid_decision("resolved") is False
    assert is_valid_decision(None) is False
    assert is_valid_decision(1) is False


def test_only_approved_allows_injection():
    assert decision_allows_injection(DECISION_APPROVED) is True
    assert decision_allows_injection(DECISION_REJECTED) is False
    assert decision_allows_injection(DECISION_PENDING) is False


# =====================================================================
# 三、服务层：决定写进**自己那一列**
# =====================================================================


def _service():
    from application.audit.services.macro_diagnosis_service import MacroDiagnosisService

    db = Mock()
    db.get_connection.return_value = Mock()
    db.execute = Mock()
    db.fetch_one = Mock(return_value=None)
    db.fetch_all = Mock(return_value=[])
    scanner = Mock()
    return MacroDiagnosisService(db, scanner), db


def test_an_illegal_decision_is_rejected_without_touching_the_db():
    """非法决定 ⇒ 拒收**且不改库**（不许半途写进去）。"""
    svc, db = _service()

    assert svc.record_injection_decision("n-1", "d-1", "pending") is False
    assert svc.record_injection_decision("n-1", "d-1", "随便写的") is False
    db.execute.assert_not_called()


def test_the_decision_goes_to_its_own_column_not_to_resolved():
    """⚠ 决定必须写进 ``injection_decision``，**不许复用 ``resolved``**。

    ``resolved`` 的语义是「已处理 ⇒ 停注入」，与「批准注入」方向相反；
    共用一个布尔 ⇒ 两件事互相打架。
    """
    svc, db = _service()

    assert svc.record_injection_decision("n-1", "d-1", DECISION_APPROVED) is True

    sql, params = db.execute.call_args[0]
    assert "injection_decision" in sql
    assert "resolved = 1" not in sql
    assert params[0] == DECISION_APPROVED
    assert params[-2:] == ("d-1", "n-1")


def test_the_pending_list_only_returns_undecided_unresolved_rows():
    svc, db = _service()

    svc.list_pending_decisions("n-1")

    sql = db.fetch_all.call_args[0][0]
    assert "injection_decision = 'pending'" in sql
    assert "resolved = 0" in sql


# =====================================================================
# 四、注入闸门：关闭时逐位一致，打开时只认 approved
# =====================================================================


def _allocator_patch(monkeypatch, *, enabled: bool, patch: str = ""):
    """把分配器的注入函数跑一遍，返回它实际用的 SQL 与取回的文本。"""
    import application.audit.services.macro_diagnosis_injection as injection_mod
    from application.engine.services.context_budget_allocator import (
        ContextBudgetAllocator,
    )

    seen = {}

    class _Db:
        def fetch_one(self, sql, params):
            seen["sql"] = sql
            seen["params"] = params
            return {"context_patch": patch} if patch else None

    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda: _Db(),
    )
    monkeypatch.setattr(
        injection_mod, "macro_diagnosis_injection_enabled", lambda: enabled
    )
    allocator = object.__new__(ContextBudgetAllocator)
    text = ContextBudgetAllocator._get_diagnosis_breakpoints(allocator, "n-1", 1)
    return seen.get("sql", ""), text


def test_the_injection_is_silent_when_the_switch_is_off(monkeypatch):
    """关闭 ⇒ 查询里**没有**任何注入决定的过滤（与改造前逐位一致）。"""
    sql, text = _allocator_patch(monkeypatch, enabled=False, patch="校准指令")

    assert "injection_decision" not in sql
    assert text == "校准指令"


def test_the_injection_requires_approval_when_the_switch_is_on(monkeypatch):
    """打开 ⇒ 只注入作者**已批准**的诊断。"""
    sql, _ = _allocator_patch(monkeypatch, enabled=True, patch="校准指令")

    assert "injection_decision = 'approved'" in sql


def test_the_injected_text_is_capped(monkeypatch):
    """登记表的不变量「注入 token 上限」：注入文本不得超上限。"""
    cap = injection_max_chars()
    assert cap > 0

    _sql, text = _allocator_patch(
        monkeypatch, enabled=False, patch="校" * (cap * 3)
    )

    assert len(text) <= cap
    assert "已按上限截断" in text


# =====================================================================
# 五、迁移：新增列，不动 resolved
# =====================================================================


def test_the_migration_adds_the_injection_decision_column():
    files = sorted(MIGRATIONS.glob("*.sql"))
    hits = [
        path
        for path in files
        if "injection_decision" in _read(path)
        and "macro_diagnosis_results" in _read(path)
    ]
    assert hits, "没有迁移给 macro_diagnosis_results 加 injection_decision 列"

    text = _read(hits[-1])
    assert "ADD COLUMN injection_decision" in text
    assert "ADD COLUMN decided_at" in text
    assert "ADD COLUMN decided_by" in text


def test_the_migration_runs_after_the_table_is_created():
    """⚠ 迁移按**文件名字母序**执行 ⇒ 加列的文件必须排在建表文件**之后**。

    踩过的坑：迁移文件取名 ``016_...`` ⇒ 字母序排在所有 ``add_*`` 之前
    ⇒ 全新安装时 ``macro_diagnosis_results`` 还没建 ⇒ 「no such table」
    ⇒ 该迁移失败且**不被标记为已应用**（既有守卫
    ``test_fresh_install_migration_integrity`` 当场抓住）。
    """
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    create_index = names.index("add_macro_diagnosis_results.sql")
    assert (
        "add_macro_diagnosis_results_injection_decision.sql" in names
    ), "加列迁移的命名要能排在建表迁移之后"
    add_index = names.index("add_macro_diagnosis_results_injection_decision.sql")
    assert add_index > create_index, (
        "加列迁移排在了建表迁移之前 ⇒ 全新安装时会「no such table」"
    )


# =====================================================================
# 六、API：决定接口
# =====================================================================


@pytest.mark.asyncio
async def test_the_decision_endpoint_records_the_decision():
    """决定接口要把决定交给服务层（而不是自己改库）。"""
    from application.audit.dtos.macro_refactor_dto import InjectionDecisionRequest
    from interfaces.api.v1.audit.macro_refactor import decide_diagnosis_injection

    service = Mock()
    service.record_injection_decision = Mock(return_value=True)

    result = await decide_diagnosis_injection(
        "n-1",
        "d-1",
        InjectionDecisionRequest(decision=DECISION_APPROVED),
        service,
    )

    assert result["success"] is True
    assert result["decision"] == DECISION_APPROVED
    service.record_injection_decision.assert_called_once()
    assert (
        service.record_injection_decision.call_args.kwargs["decision"]
        == DECISION_APPROVED
    )


@pytest.mark.asyncio
async def test_the_decision_endpoint_reports_a_rejected_decision():
    """服务层拒收 ⇒ 接口必须**如实**返回失败（不许假装成功）。"""
    from application.audit.dtos.macro_refactor_dto import InjectionDecisionRequest
    from interfaces.api.v1.audit.macro_refactor import decide_diagnosis_injection

    service = Mock()
    service.record_injection_decision = Mock(return_value=False)

    result = await decide_diagnosis_injection(
        "n-1",
        "d-1",
        InjectionDecisionRequest(decision="随便写的"),
        service,
    )

    assert result["success"] is False


# =====================================================================
# 七、前端：提案要**有出口**（此前 7 个方法零 UI 消费者）
# =====================================================================


def test_the_frontend_has_a_panel_for_the_pending_diagnoses():
    """「注入经提案确认」必须有人能点 —— 前端要有面板。"""
    panel = FRONTEND / "components/panels/MacroRefactorPanel.vue"
    assert panel.exists(), "缺少宏观诊断提案面板"

    text = _read(panel)
    assert "macroRefactorApi" in text
    assert "listPendingDiagnoses" in text
    assert "decideDiagnosis" in text
    assert "approved" in text and "rejected" in text


def test_the_panel_is_mounted_in_the_workbench():
    """面板要被真的挂上去（建了组件没人引用 = 零出口）。"""
    bible = _read(FRONTEND / "components/panels/BiblePanel.vue")
    assert "MacroRefactorPanel" in bible
    assert "<MacroRefactorPanel" in bible


def test_the_api_client_exposes_the_decision_calls():
    tools = _read(FRONTEND / "api/tools.ts")
    assert "listPendingDiagnoses" in tools
    assert "decideDiagnosis" in tools
    assert "diagnosis/pending" in tools
    assert "/decision" in tools
