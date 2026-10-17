"""DAG 层的「不许再谎报」守卫。

背景
----
`application/engine/dag/` 曾经是第二套执行引擎（LangGraph 编排 + 事件聚合 +
守护进程）。它从未真正投入使用：2026-05-16 00:08:55 引入真实实现，7 秒后被同一次
批量导入里的下一个 commit（`fc5de029`「后端路由精简为纯展示层」）拆成纯展示层。
执行面此后一直是死代码，却有三个出口在对外**报告它可用**：

1. `POST /dag/{id}/run`  —— `return {"status": "started"}`，什么都没启动
2. `POST /dag/{id}/stop` —— `return {"status": "stopped"}`，没有东西在运行
3. `GET  /dag/health/dag` —— `all(c.get("status") != "error" ...)`，而两个 check
   字典都不带 `status` 字段 ⇒ 恒真 ⇒ **永远报 ok**，degraded 分支不可达

第 3 条尤其阴险：它不是写死的常量，而是「算出来的空转」，看起来完全合理。

2026-10-17 的处置：删除 9 个死模块与只覆盖它们的 9 个测试文件，保留仍在生产
使用的 `models` / `registry` / `plan`；三个出口改为如实反映现状。

本文件钉住这个处置，防止退化：
- run/stop 必须真的返回 501（不能又被改回 200 + 假成功）
- health 必须显式声明执行面不可用
- health 的总体判定**必须能真的翻成 degraded**（第 4 条最关键：它对着「恒真」
  这个具体缺陷，而不是对着当前实现）
- 死符号不得从 `application.engine.dag` 复活

注意 `dag_routes.router` 自带 `prefix="/dag"`，所以这里挂载时只补 `/api/v1`。
"""
import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from interfaces.api.v1.engine.dag import dag_routes

# 已删除的执行面模块名（相对 application.engine.dag 的第一段）
REMOVED_MODULE_NAMES = {
    "daemon_runner", "engine", "event_aggregator", "ipc_adapter",
    "error_classifier", "version_manager", "validator", "prompt_validator",
}

DEAD_SYMBOLS = [
    "DAGEngine", "DAGExecutionError", "DAGDaemonRunner", "EngineSelector",
    "DAGValidator", "DAGVersionManager", "NodeEventAggregator", "IPCAdapter",
    "NodeErrorClassifier", "ErrorClassification", "ErrorLevel", "RetryStrategy",
    "PromptTemplateValidator",
]


@pytest.fixture
def client():
    """只挂 DAG 路由的最小应用 —— 不必为了测三个端点而启动整个后端。"""
    app = FastAPI()
    app.include_router(dag_routes.router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


def test_run_endpoint_reports_not_implemented_instead_of_fake_success(client):
    """"已启动"是最有害的返回值：调用方（前端 dagRunStore）会据此置 running。"""
    resp = client.post("/api/v1/dag/novel-1/run")
    assert resp.status_code == 501, (
        f"/run 返回了 {resp.status_code}，期望 501。"
        f"若它返回 200 + started，调用方会得到「已启动」的假象。"
        f"实际响应体：{resp.text[:200]}"
    )
    assert "未启用" in resp.text


def test_stop_endpoint_reports_not_implemented(client):
    """没有在运行的东西，就不能说「已停止」。"""
    resp = client.post("/api/v1/dag/novel-1/stop")
    assert resp.status_code == 501, (
        f"/stop 返回了 {resp.status_code}，期望 501。响应体：{resp.text[:200]}"
    )


def test_health_declares_the_execution_layer_unavailable(client):
    """health 必须显式带上能力缺口，而不是只给一个笼统的 ok。"""
    resp = client.get("/api/v1/dag/health/dag")
    assert resp.status_code == 200
    checks = resp.json().get("checks", {})
    assert "execution" in checks, (
        f"health 响应里没有 execution 项，读者会以为整层都可用：{list(checks)}"
    )
    assert checks["execution"]["status"] == "unavailable", (
        f"execution 状态应为 unavailable，实际 {checks['execution'].get('status')!r}"
    )


def test_health_rollup_can_actually_reach_degraded(client, monkeypatch):
    """**关键的一条**：总体判定必须真的会翻红。

    旧实现是 `all(c.get("status") != "error" ...)`，而 check 字典里没有 `status`
    键 —— `.get()` 恒返回 None，恒不等于 "error" ⇒ 恒真。这条测试清空注册表以
    制造一个真实的失败态；若判定仍是恒真就会红。

    只断言「当前返回 ok」是测不出这类缺陷的：空转判定在**正常态**下与正确判定
    的输出完全一样。必须先制造失败，再看它翻不翻。
    """
    from application.engine.dag.registry import NodeRegistry

    monkeypatch.setattr(NodeRegistry, "all_types", staticmethod(lambda: []))

    body = client.get("/api/v1/dag/health/dag").json()
    assert body["status"] == "degraded", (
        f"注册表为空时 health 仍报 {body.get('status')!r} —— 总体判定是恒真的，"
        f"这正是被删除的旧实现缺陷（原 `c.get('status') != 'error'`）。"
        f"实际响应：{body}"
    )
    assert body["checks"]["node_registry"]["status"] == "error"


def test_health_rollup_stays_ok_when_the_layer_is_intact(client):
    """对照组：清空是异常态，未清空就是正常态。两个方向都要钉住。"""
    body = client.get("/api/v1/dag/health/dag").json()
    assert body["status"] == "ok"
    assert body["checks"]["node_registry"]["status"] == "ok"
    assert body["checks"]["execution"]["status"] == "unavailable"


def test_read_only_dag_endpoints_still_work():
    """删除执行面不能伤到展示面 —— DAG 层的存活价值全在只读端点上。"""
    app = FastAPI()
    app.include_router(dag_routes.router, prefix="/api/v1")
    c = TestClient(app, raise_server_exceptions=False)

    for path in ["/api/v1/dag/registry/types",
                 "/api/v1/dag/registry/linkage",
                 "/api/v1/dag/novel-1"]:
        resp = c.get(path)
        assert resp.status_code == 200, f"只读端点 {path} 回归成 {resp.status_code}"


def test_removed_symbols_are_not_reexported():
    """死符号不得从包根复活（它们只会重新制造一批无人调用的导出）。"""
    import application.engine.dag as dag_pkg

    alive = [s for s in DEAD_SYMBOLS if hasattr(dag_pkg, s)]
    assert not alive, f"这些已删除的执行面符号又出现在包命名空间里：{alive}"

    exported = [s for s in DEAD_SYMBOLS if s in getattr(dag_pkg, "__all__", [])]
    assert not exported, f"__all__ 里仍然导出已删除的符号：{exported}"


def test_no_module_under_dag_imports_the_removed_execution_modules():
    """结构性守卫：dag 包里不得再出现对已删模块的 import。

    用 ast 解析**模块路径**，不用字符串包含 —— 否则
    `from application.engine.dag.models import ...` 会因为路径里有
    "application.engine" 而被误判命中 "engine"，
    `from pydantic import model_validator` 也会被 "validator" 命中。
    """
    import application.engine.dag as dag_pkg

    dag_dir = Path(dag_pkg.__file__).parent
    pkg_prefix = "application.engine.dag."
    offenders = []

    for py in sorted(dag_dir.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imported = []
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
                # from X import Y —— Y 也可能是子模块
                imported += [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            for mod in imported:
                if not mod.startswith(pkg_prefix):
                    continue
                head = mod[len(pkg_prefix):].split(".")[0]
                if head in REMOVED_MODULE_NAMES:
                    offenders.append(f"{py.relative_to(dag_dir)}:{node.lineno} -> {mod}")

    assert not offenders, f"dag 包里又出现了对已删模块的 import：{offenders}"


def test_removed_modules_are_actually_gone_from_disk():
    """文件系统层面钉住一次 —— 注释里写「已删除」不是证据。"""
    import application.engine.dag as dag_pkg

    dag_dir = Path(dag_pkg.__file__).parent
    expected_absent = [
        "daemon_runner.py", "engine.py", "event_aggregator.py",
        "ipc_adapter.py", "error_classifier.py", "version_manager.py",
        "validator.py", "prompt_validator.py", "nodes/prop_nodes.py",
    ]
    still_there = [n for n in expected_absent if (dag_dir / n).exists()]
    assert not still_there, f"这些死模块还在磁盘上：{still_there}"
