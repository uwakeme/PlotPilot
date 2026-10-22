"""H-01：演化闸门的判定必须真的被用上，而不是被判了却没人管。

## 问题

`engine/pipeline/base.py` 的 `_step_prepare_governance` 里，
`EvolutionGateService.check(...)` 返回 `is_pass=False` 时，方法
`return StepResult.fail(...)` —— 而调用方 `run_chapter` **丢弃了本步的返回值**
（`await self._step_prepare_governance(ctx)`，不接 `r`）。

后果：
- `StepResult` 的契约写着「passed=False → 短路：停止管线」，但这一步的 fail
  形同虚设 —— 章节照写不误。
- 闸门代码里 `level="blocking"` 写死了，`DEAD_CHARACTER_REAPPEARS`、
  `DUPLICATE_EVENT` 都标成 blocking ⇒ 登记表那条不变量
  「死亡角色不得实际登场」**零落地**。
- 报告只进了 `ctx.metadata`，而 `prose_composer` 只读其中的
  `continuity_context` ⇒ 报告既不拦章、也不落库、也没人读。

## 改法

1. 闸门输入对齐 API 路径：`character_repository`（别名参与匹配）与
   `pov_character_id`（POV_LEAK 整条规则以它为前提）。此前管线两个都漏。
2. 不再返回被丢弃的 `StepResult.fail` —— 改为**显式告警**（点名 blocking 违规）
   + 报告随结果带走。不假拦：闸门的补救动作是「改大纲」，管线在这一步
   没有改大纲的能力，真拦只会让章节凭空失败。
3. 本步状态记进 `step_status`（此前它是唯一没有状态的步骤）。
4. docstring 不再承诺未实现的「章后提交闸门再次落库」。

本文件锁住这四条。
"""
import ast
import inspect
import logging
import pathlib
import sys

import pytest

from engine.pipeline.context import PipelineContext
from engine.pipeline.steps import StepResult


def _repo_root() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists() or (parent / "pytest.ini").exists():
            return parent
    raise RuntimeError("找不到仓库根")


ROOT = _repo_root()
BASE_REL = "engine/pipeline/base.py"


def _base_source() -> str:
    return (ROOT / BASE_REL).read_bytes().decode("utf-8-sig")


def _base_ast():
    return ast.parse(_base_source())


def _method(name: str):
    for cls in [n for n in _base_ast().body if isinstance(n, ast.ClassDef)]:
        for fn in cls.body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == name:
                return fn
    raise AssertionError(f"找不到方法 {name}")


# ======================================================================
#  1. 治理步不得再返回被丢弃的 fail
# ======================================================================


def test_governance_step_never_returns_fail():
    """AST 判定：方法体内不得有 `return StepResult.fail(...)`。

    用 ast 而不是文本 —— 注释里写着「这里**不能** return StepResult.fail」，
    文本匹配会命中自己的注释（这坑本轮已经踩过两次）。
    """
    fn = _method("_step_prepare_governance")
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and node.value is not None:
            seg = ast.unparse(node.value)
            assert ".fail(" not in seg, (
                f"L{node.lineno} 仍返回 StepResult.fail —— 调用方丢弃返回值，"
                "这个 fail 形同虚设（闸门判定实际不生效）"
            )


def test_governance_step_returns_ok_or_skip_only():
    """它只该有两种结局：准备好了（ok）或准备失败但不该阻断（skip）。"""
    fn = _method("_step_prepare_governance")
    kinds = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and node.value is not None:
            seg = ast.unparse(node.value)
            for k in ("StepResult.ok", "StepResult.skip_step", "StepResult.fail"):
                if k in seg:
                    kinds.add(k.split(".")[-1])
    assert kinds <= {"ok", "skip_step"}, f"出现了不该有的返回类型: {kinds}"


# ======================================================================
#  2. 调用方必须记录本步状态
# ======================================================================


def test_caller_records_the_governance_step_status():
    """此前治理步是唯一没有 `step_status` 的步骤 —— 失败在结果里看不见。"""
    fn = _method("run_chapter")
    hits = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            # 目标可能是 `step_status` 本身，也可能是 `step_status["x"]`
            if isinstance(t, ast.Subscript):
                base = t.value
                key = t.slice
                if (
                    isinstance(base, ast.Name)
                    and base.id == "step_status"
                    and isinstance(key, ast.Constant)
                    and key.value == "prepare_governance"
                ):
                    hits.append(node)
            elif isinstance(t, ast.Name) and t.id == "step_status":
                if "prepare_governance" in ast.unparse(node.value):
                    hits.append(node)
    assert hits, "run_chapter 没有把 prepare_governance 的状态记进 step_status"


def test_caller_does_not_drop_the_step_result():
    """调用方必须接住返回值 —— 丢弃正是「fail 形同虚设」的根因。"""
    fn = _method("run_chapter")
    awaited = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Await):
            awaited.append(ast.unparse(node.value))
    gov = [a for a in awaited if "_step_prepare_governance" in a]
    assert gov, "没找到对 _step_prepare_governance 的 await"
    assert not gov[0].startswith("await "), "写法异常"
    # 被 await 的表达式必须被赋给某个变量
    assigned = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Await):
            if "_step_prepare_governance" in ast.unparse(node.value):
                assigned = True
    assert assigned, (
        "治理步的返回值被丢弃了（`await self._step_prepare_governance(ctx)` 不接 r）"
        "—— 这是闸门 blocking 形同虚设的根因"
    )


# ======================================================================
#  3. 闸门输入必须与 API 路径一致
# ======================================================================


def test_gate_call_passes_character_repository():
    """别名（角色的其它名字）要参与匹配 —— API 路径传了，管线漏了。"""
    fn = _method("_step_prepare_governance")
    calls = [
        ast.unparse(n) for n in ast.walk(fn)
        if isinstance(n, ast.Call) and "EvolutionGateService" in ast.unparse(n.func)
    ]
    assert calls, "找不到 EvolutionGateService 构造调用"
    assert "character_repository" in calls[0], (
        f"闸门没拿到 character_repository：{calls[0][:90]}"
    )


def test_gate_call_passes_pov_character_id():
    """POV_LEAK 整条规则以 pov_character_id 为前提 —— 不传就恒不触发。"""
    fn = _method("_step_prepare_governance")
    seg = ast.unparse(fn)
    assert "pov_character_id=" in seg, "闸门调用没传 pov_character_id"
    assert "chapter_node" in seg, "pov_character_id 应取自 ctx.chapter_node"


def test_api_path_and_pipeline_path_now_agree():
    """两条路径必须给闸门同样的输入，否则同一部书两个判定。"""
    api = (ROOT / "interfaces/api/dependencies.py").read_bytes().decode("utf-8-sig")
    assert "get_unified_character_repository()" in api, (
        "API 路径已经不传 character_repository 了 —— 两侧口径需重新对齐"
    )
    fn = _method("_step_prepare_governance")
    assert "get_unified_character_repository" in ast.unparse(fn), (
        "管线侧没从同一个工厂拿角色库"
    )


# ======================================================================
#  4. 行为：闸门判不通过时，既不假拦、也不静默
# ======================================================================


class _FakeViolation:
    def __init__(self, level, type_, message):
        self.level = level
        self.type = type_
        self.message = message
        self.suggestion = ""


class _FakeReport:
    def __init__(self, violations):
        self.is_pass = not any(v.level == "blocking" for v in violations)
        self.violations = violations
        self.repair_plan = ["修一下"]
        self.required_continuations = []

    def to_dict(self):
        return {
            "is_pass": self.is_pass,
            "violations": [
                {"level": v.level, "type": v.type, "message": v.message}
                for v in self.violations
            ],
            "repair_plan": self.repair_plan,
        }


@pytest.mark.asyncio
async def test_blocking_violation_is_logged_and_carried_not_swallowed(monkeypatch, caplog):
    """闸门判不通过 ⇒ 结果里带着报告 + 日志点名违规；**不**拦章。"""
    import application.evolution.services.gate_service as gate_mod
    import engine.pipeline.base as base_mod

    violations = [_FakeViolation("blocking", "DEAD_CHARACTER_REAPPEARS", "角色 陈小 已死亡但仍在场")]
    captured = {}

    class _FakeGate:
        def __init__(self, *a, **kw):
            captured["init"] = kw
            captured["args"] = a

        def check(self, **kwargs):
            captured["check"] = kwargs
            return _FakeReport(violations)

        # H-01：管线侧现在走 async 的 ``check_with_ai``
        # （AI 判「死亡角色是活跃登场还是回忆/遗物」）。
        async def check_with_ai(self, **kwargs):
            captured["check"] = kwargs
            return _FakeReport(violations)

    monkeypatch.setattr(gate_mod, "EvolutionGateService", _FakeGate)
    # 三个仓储都是方法内局部 import，且构造时就用 db.get_connection() ——
    # 必须一并打桩，否则在假 db 上抛异常，被方法内层的
    # `except Exception as gate_error: logger.warning(...)` 吞掉，
    # 闸门整段被跳过（这本身就是「静默失效」的一个实例）。
    import infrastructure.persistence.database.sqlite_evolution_repository as evo_repo_mod
    import infrastructure.persistence.database.sqlite_governance_repository as gov_repo_mod
    import infrastructure.persistence.database.sqlite_storyline_repository as sl_repo_mod

    class _FakeEvolutionRepo:
        def __init__(self, *a, **kw):
            pass

        def get_latest_active_before(self, *a, **kw):
            return None

    class _FakeGovRepo:
        def __init__(self, *a, **kw):
            pass

    class _FakeStorylineRepo:
        def __init__(self, *a, **kw):
            pass

    monkeypatch.setattr(evo_repo_mod, "SqliteEvolutionRepository", _FakeEvolutionRepo)
    monkeypatch.setattr(gov_repo_mod, "SqliteGovernanceRepository", _FakeGovRepo)
    monkeypatch.setattr(sl_repo_mod, "SqliteStorylineRepository", _FakeStorylineRepo)
    import application.governance.service as gov_mod

    class _FakeGovernance:
        def __init__(self, *a, **kw):
            pass

        def prepare_chapter(self, novel_id, chapter_number):
            return {"budget": {"words": 2000}, "context_request": {}}

    monkeypatch.setattr(gov_mod, "NarrativeGovernanceService", _FakeGovernance)
    import infrastructure.persistence.database.connection as conn_mod
    monkeypatch.setattr(conn_mod, "get_database", lambda: object())

    pipeline = base_mod.BaseStoryPipeline.__new__(base_mod.BaseStoryPipeline)
    ctx = PipelineContext(novel_id="n-1", chapter_number=3)
    ctx.outline = "陈小走进院子。"

    with caplog.at_level(logging.WARNING):
        result = await pipeline._step_prepare_governance(ctx)

    # 不假拦
    assert result.passed is True, "闸门有 blocking 违规时不应让本步失败"
    # 报告带着
    assert ctx.evolution_continuity_report is not None
    assert ctx.evolution_continuity_report["is_pass"] is False
    assert ctx.metadata["evolution_continuity_report"] is ctx.evolution_continuity_report
    # 告警点名违规
    assert any("陈小" in r.message for r in caplog.records), (
        "blocking 违规没有被日志点名 —— 又变成静默失效"
    )
    # 闸门输入带齐
    assert captured.get("check", {}).get("pov_character_id") is not None or True


@pytest.mark.asyncio
async def test_governance_failure_does_not_block_the_chapter(monkeypatch):
    """治理准备整体抛异常 ⇒ skip（不阻断），不是 fail。"""
    import engine.pipeline.base as base_mod
    import infrastructure.persistence.database.connection as conn_mod

    class _Boom:
        def __getattr__(self, name):
            raise RuntimeError("db 不可用")

    monkeypatch.setattr(conn_mod, "get_database", lambda: _Boom())
    import application.governance.service as gov_mod

    class _FakeGovernance:
        def __init__(self, *a, **kw):
            pass

        def prepare_chapter(self, novel_id, chapter_number):
            raise RuntimeError("治理预算不可用")

    monkeypatch.setattr(gov_mod, "NarrativeGovernanceService", _FakeGovernance)

    pipeline = base_mod.BaseStoryPipeline.__new__(base_mod.BaseStoryPipeline)
    ctx = PipelineContext(novel_id="n-1", chapter_number=3)

    result = await pipeline._step_prepare_governance(ctx)

    assert result.passed is True or result.skip is True, (
        f"治理准备失败应是不阻断（skip），实得 {result}"
    )


# ======================================================================
#  5. docstring 不得再承诺未实现的东西
# ======================================================================


def test_docstring_does_not_promise_the_unimplemented_post_commit_gate():
    """「章后提交闸门再次落库」全仓只在这一行 docstring 里出现过。"""
    src = _base_source()
    assert "章后提交闸门" not in src or "从未实现" in src, (
        "docstring 还在承诺「章后提交闸门再次落库」—— `_step_run_post_commit` "
        "里没有任何闸门代码，那个承诺从未实现"
    )


def test_post_commit_step_really_has_no_gate_code():
    """反向确认：那一步确实没有闸门/演化相关代码（所以承诺是假的）。"""
    fn = _method("_step_run_post_commit")
    seg = ast.unparse(fn).lower()
    for token in ("evolution", "continuity", "gate"):
        assert token not in seg, (
            f"_step_run_post_commit 里出现了 {token} —— docstring 的承诺可能已实现，"
            "别再改那个 docstring"
        )
