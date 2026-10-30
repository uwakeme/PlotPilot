# -*- coding: utf-8 -*-
"""第 63 批：守护进程监督者（死了自动拉起）+ 门禁不许杀真守护进程。

为什么需要这两条
----------------
1. **门禁本身就在杀守护进程**（2026-10-30 插桩定位）：
   ``tests/unit/interfaces/test_daemon_manager.py`` 里的 5 处
   ``AutopilotDaemonManager(...)`` **没注入** ``orphan_daemon_cleaner`` 桩
   ⇒ ``manager.start()`` 的第一步 ``self._orphan_daemon_cleaner()`` 走的是
   **真的** ``cleanup_orphan_daemon()`` ⇒ 它读**真锁** ⇒ 把**正在运行的**
   守护进程 taskkill 掉 ⇒ **跑一次门禁，自动驾驶就静默停摆**
   （API 还活着、``/health`` 仍 200、界面完全无感）。
   ⚠ ``start()`` 里「先清孤儿」排在 ``disable_auto_daemon`` 判断**之前**
   ⇒ 连「禁用自动守护进程」那条测试也会杀掉真守护进程。
2. **守护进程被外部杀掉时没人拉起** ⇒ 补一个监督者：带退避、带上限、
   锁被别人持有时**不抢**、关机流程里**不复活**。

本文件的守卫：
- 监督者的**真行为**（拉起 / 不拉起 / 退避 / 上限 / 停止后不拉起）；
- **防回归**：AST 扫描整个 ``tests/`` 目录 —— 任何构造
  ``AutopilotDaemonManager(...)`` 的地方**必须**注入
  ``orphan_daemon_cleaner`` 与 ``supervisor_factory`` 两个桩；
- **反装饰品**：``daemon.supervisor`` 配置的每个键都真的被读。
"""
from __future__ import annotations

import ast
import logging
import pathlib
from types import SimpleNamespace

import pytest

from interfaces.daemon_manager import (
    AutopilotDaemonManager,
    DaemonSupervisor,
    DaemonSupervisorSettings,
    get_daemon_supervisor_settings,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TESTS_ROOT = REPO_ROOT / "tests"


# ─────────────────────────── 测试替身 ───────────────────────────


class _FakeManager:
    """守护进程管理器的替身（只被监督者调用 ``status()`` / ``start()``）。"""

    def __init__(self, running: bool = False):
        self.running = running
        self.start_calls = 0
        self.fail = False

    def status(self):
        return SimpleNamespace(running=self.running, pid=4242)

    def start(self):
        self.start_calls += 1
        if self.fail:
            raise RuntimeError("拉起失败（测试用）")
        self.running = True


class _FakeLogger:
    def __init__(self):
        self.errors: list[str] = []
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg, *args, **kwargs):
        self.errors.append(str(msg) % args if args else str(msg))

    def info(self, msg, *args, **kwargs):
        self.infos.append(str(msg) % args if args else str(msg))

    def warning(self, msg, *args, **kwargs):
        self.warnings.append(str(msg) % args if args else str(msg))

    def debug(self, msg, *args, **kwargs):
        pass


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _supervisor(
    manager,
    *,
    lock_held: bool = False,
    clock=None,
    settings=None,
):
    return DaemonSupervisor(
        manager,
        settings or DaemonSupervisorSettings(),
        _FakeLogger(),
        lock_probe=lambda: lock_held,
        clock=clock or _Clock(),
    )


# ─────────────────────── 监督者：真行为 ───────────────────────


def test_alive_daemon_is_never_restarted():
    """守护进程活着 ⇒ 监督者什么都不做（不许无故重启）。"""
    manager = _FakeManager(running=True)
    supervisor = _supervisor(manager)

    assert supervisor._check_once() == "alive"
    assert manager.start_calls == 0


def test_dead_daemon_is_restarted_when_the_lock_is_free():
    """守护进程死了 + 锁空闲 ⇒ 拉起一次（这正是被外部杀掉时的兜底）。"""
    manager = _FakeManager(running=False)
    supervisor = _supervisor(manager, lock_held=False)

    assert supervisor._check_once() == "restarted"
    assert manager.start_calls == 1
    assert manager.running is True


def test_dead_daemon_is_not_restarted_when_another_instance_holds_the_lock():
    """锁被别人持有 ⇒ **不拉起**。

    ⚠ 为什么不许抢：``manager.start()`` 的第一步是「清掉上一个实例的守护进程」
    ⇒ 抢锁会把**另一个 API 实例**的守护进程杀掉 ⇒ 两个实例互抢、抖动。
    """
    manager = _FakeManager(running=False)
    supervisor = _supervisor(manager, lock_held=True)

    assert supervisor._check_once() == "locked_elsewhere"
    assert manager.start_calls == 0


def test_a_failed_restart_is_backed_off():
    """拉起失败 ⇒ 进退避窗口 ⇒ 紧接着再检查**不再拉起**（防重启风暴）。"""
    manager = _FakeManager(running=False)
    manager.fail = True
    clock = _Clock()
    supervisor = _supervisor(manager, clock=clock)

    assert supervisor._check_once() == "failed"
    assert manager.start_calls == 1

    # 退避窗口内 ⇒ 不再拉起
    assert supervisor._check_once() == "backoff"
    assert manager.start_calls == 1

    # 退避窗口过去 ⇒ 允许再试一次
    clock.advance(supervisor._settings.initial_backoff_seconds + 1)
    assert supervisor._check_once() == "failed"
    assert manager.start_calls == 2


def test_restarts_stop_at_the_configured_cap():
    """连续失败到上限 ⇒ 不再拉起（但仍记 error ⇒ 不是静默放弃）。"""
    manager = _FakeManager(running=False)
    manager.fail = True
    clock = _Clock()
    settings = DaemonSupervisorSettings(max_restarts=2)
    supervisor = _supervisor(manager, clock=clock, settings=settings)
    log = supervisor._logger

    for _ in range(2):
        assert supervisor._check_once() == "failed"
        clock.advance(settings.initial_backoff_seconds * 10 + 1)

    assert supervisor._check_once() == "exhausted"
    assert manager.start_calls == 2
    assert any("上限" in line for line in log.errors)


def test_supervisor_does_not_restart_after_stop():
    """**停止后绝不再拉起**（关机流程里不许把守护进程复活）。"""
    manager = _FakeManager(running=False)
    supervisor = _supervisor(manager)

    supervisor.stop()

    assert supervisor._check_once() == "stopped"
    assert manager.start_calls == 0


def test_failed_restart_is_recorded_in_the_state():
    """拉起失败必须留痕（状态里能看到错误文本）。"""
    manager = _FakeManager(running=False)
    manager.fail = True
    supervisor = _supervisor(manager)

    supervisor._check_once()

    state = supervisor.state()
    assert state["restart_attempts"] == 1
    assert "拉起失败" in state["last_error"]
    assert state["running"] is False  # 没起线程（直接调 _check_once）


def test_manager_stop_stops_the_supervisor_before_the_daemon():
    """``manager.stop()`` 必须**先**停监督者 ⇒ 关机时不会被复活。"""

    class _FakeSupervisor:
        def __init__(self):
            self.stopped = 0

        def stop(self):
            self.stopped += 1

        def start(self):
            pass

        def state(self):
            return {}

    supervisor = _FakeSupervisor()

    class _Process:
        pid = 999999

        def is_alive(self):
            return False

        def join(self, timeout=None):
            pass

        def terminate(self):
            pass

    manager = AutopilotDaemonManager(
        logger=logging.getLogger(__name__),
        log_level=logging.INFO,
        log_file="",
        shared_state_provider=lambda: {},
        settings_provider=lambda: SimpleNamespace(disable_auto_daemon=True),
        process_factory=lambda **kwargs: _Process(),
        event_factory=lambda: SimpleNamespace(is_set=lambda: True, set=lambda: None),
        pid_recorder=lambda pid: None,
        pid_recorder_clear=lambda: None,
        orphan_daemon_cleaner=lambda *a, **k: 0,
        supervisor_factory=lambda _m: supervisor,
    )
    manager.process = _Process()
    # ⚠ 监督者是**懒创建**的（``start()`` 里才建）⇒ 这里直接装上，
    #   模拟「守护进程起来过、监督者也在跑」的状态。
    manager._supervisor = supervisor

    manager.stop()

    assert supervisor.stopped == 1
    assert manager._supervisor is None


def test_disabled_auto_daemon_starts_neither_daemon_nor_supervisor():
    """``DISABLE_AUTO_DAEMON=1`` ⇒ 不起守护进程 ⇒ 也就没有东西要监督。"""
    created: list[str] = []

    manager = AutopilotDaemonManager(
        logger=logging.getLogger(__name__),
        log_level=logging.INFO,
        log_file="",
        shared_state_provider=lambda: {},
        settings_provider=lambda: SimpleNamespace(disable_auto_daemon=True),
        process_factory=lambda **kwargs: created.append("process"),
        event_factory=lambda: SimpleNamespace(is_set=lambda: True, set=lambda: None),
        pid_recorder=lambda pid: None,
        pid_recorder_clear=lambda: None,
        orphan_daemon_cleaner=lambda *a, **k: 0,
        supervisor_factory=lambda _m: created.append("supervisor"),
    )

    manager.start()

    assert created == []
    assert manager.process is None
    assert manager._supervisor is None


# ─────────────────────── 配置：反装饰品 ───────────────────────


def test_every_supervisor_config_key_is_actually_read():
    """``daemon.supervisor`` 的每个键都真的被读（不许有装饰品键）。"""
    import yaml

    perf = yaml.safe_load(
        (REPO_ROOT / "config" / "performance.yaml").read_text(encoding="utf-8-sig")
    )
    section = ((perf or {}).get("daemon") or {}).get("supervisor") or {}
    assert section, "config/performance.yaml 里没有 daemon.supervisor 段"

    fields = {f.name for f in DaemonSupervisorSettings.__dataclass_fields__.values()}
    assert set(section) == fields, (
        f"配置键与 DaemonSupervisorSettings 字段对不上："
        f"多余={set(section) - fields} 缺少={fields - set(section)}"
    )

    # 真读得到（不是只写在 yaml 里）
    settings = get_daemon_supervisor_settings()
    assert settings.poll_seconds == section["poll_seconds"]
    assert settings.max_restarts == section["max_restarts"]


# ─────────────────────── 防回归：门禁不许杀真进程 ───────────────────────


def _manager_constructions():
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        # ⚠ ``utf-8-sig``：带 BOM 的文件用 ``utf-8`` 读会让 ``ast.parse``
        # 直接抛 ``SyntaxError: invalid non-printable character U+FEFF``
        # （import 机制容忍 BOM，工具链不容忍 ⇒ 扫描必须自己吃 BOM）。
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "AutopilotDaemonManager":
                yield path, node


def test_tests_never_build_the_manager_without_isolation_stubs():
    """⚠ 构造 ``AutopilotDaemonManager(...)`` **必须**注入两个桩。

    为什么是硬要求（2026-10-30 实测）：
    ``manager.start()`` 的第一步是 ``self._orphan_daemon_cleaner()``
    （默认 = 真的 ``cleanup_orphan_daemon()``）⇒ 它读**真锁**并 taskkill
    **正在运行的**守护进程 ⇒ **跑一次门禁就静默停摆**。
    ⚠ 这一步排在 ``disable_auto_daemon`` 判断**之前** ⇒ 连「禁用自动守护进程」
    的那条测试也会杀掉真守护进程。

    ⇒ 少任何一个桩，门禁就会重新开始杀进程 ⇒ 这条守卫不许被绕开。
    """
    missing = []
    for path, node in _manager_constructions():
        keywords = {kw.arg for kw in node.keywords}
        absent = {"orphan_daemon_cleaner", "supervisor_factory"} - keywords
        if absent:
            rel = path.relative_to(REPO_ROOT)
            missing.append(f"{rel}:{node.lineno} 缺 {sorted(absent)}")

    assert not missing, (
        "这些 AutopilotDaemonManager 构造没注入隔离桩"
        f"（会让门禁杀掉正在运行的守护进程）：{missing}"
    )


# ─────────────────────── /health 出口 ───────────────────────


def test_health_payload_exposes_the_supervisor_state():
    """``/health`` 必须带出监督者状态（守护进程被杀掉时能看出来）。"""
    from fastapi.testclient import TestClient

    from interfaces.api.settings import BackendSettings
    from interfaces.main import create_app

    app = create_app(
        BackendSettings(frontend_dir=pathlib.Path(REPO_ROOT / "frontend" / "dist"))
    )
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert "daemon_supervisor" in payload
    assert set(payload["daemon_supervisor"]) >= {
        "enabled",
        "running",
        "restart_attempts",
        "last_error",
    }
