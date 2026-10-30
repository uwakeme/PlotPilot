import logging
import os

from interfaces.api.settings import BackendSettings
from interfaces.daemon_manager import (
    AutopilotDaemonManager,
    DaemonLifecycleSettings,
    DaemonStatus,
    _ProcessRow,
    cleanup_orphan_python_processes,
    is_expected_daemon_shutdown_exception,
    is_plotpilot_backend_command,
    strip_executable_token,
)


class _FakeSupervisor:
    """监督者替身：只记录 start/stop，不起真线程、不碰真锁。"""

    def __init__(self, *_args, **_kwargs):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def state(self):
        return {
            "enabled": False,
            "running": False,
            "restart_attempts": 0,
            "last_restart_at": None,
            "last_error": "",
        }


class FakeEvent:
    def __init__(self):
        self.was_set = False

    def set(self):
        self.was_set = True


class FakeProcess:
    pid = 1234

    def __init__(self, *, alive=True):
        self.alive = alive
        self.join_calls = []
        self.terminated = False

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.join_calls.append(timeout)

    def terminate(self):
        self.terminated = True
        self.alive = False


def test_expected_daemon_shutdown_exception_detects_chained_interrupt():
    exc = RuntimeError("wrapper")
    exc.__cause__ = KeyboardInterrupt()

    assert is_expected_daemon_shutdown_exception(exc) is True


def test_daemon_manager_status_reads_process_state():
    process = FakeProcess(alive=True)
    manager = AutopilotDaemonManager(
        log_level=20,
        log_file="logs/test.log",
        shared_state_provider=lambda: {},
        # ⚠ 必须注入桩（第 63 批）：默认实现会**读真锁**并 taskkill 掉
        #   **正在运行的**守护进程 ⇒ 跑一次门禁就静默停摆。
        orphan_daemon_cleaner=lambda *a, **k: 0,
        # ⚠ 也不许起真监督线程（它会轮询并可能拉起守护进程）。
        supervisor_factory=_FakeSupervisor,
        lifecycle_settings_provider=lambda: DaemonLifecycleSettings(
            graceful_join_timeout_seconds=0.25,
            terminate_join_timeout_seconds=0.5,
        ),
    )
    manager.process = process

    assert manager.status() == DaemonStatus(running=True, pid=1234)


def test_daemon_manager_start_respects_disable_auto_daemon():
    calls = []
    manager = AutopilotDaemonManager(
        log_level=20,
        log_file="logs/test.log",
        shared_state_provider=lambda: {},
        # ⚠ 必须注入桩（第 63 批）：默认实现会**读真锁**并 taskkill 掉
        #   **正在运行的**守护进程 ⇒ 跑一次门禁就静默停摆。
        orphan_daemon_cleaner=lambda *a, **k: 0,
        # ⚠ 也不许起真监督线程（它会轮询并可能拉起守护进程）。
        supervisor_factory=_FakeSupervisor,
        settings_provider=lambda: BackendSettings(disable_auto_daemon=True),
        process_factory=lambda **kwargs: calls.append(kwargs),
    )

    manager.start()

    assert calls == []
    assert manager.process is None


def test_daemon_manager_stop_signals_and_terminates_stuck_process(monkeypatch):
    process = FakeProcess(alive=True)
    event = FakeEvent()
    manager = AutopilotDaemonManager(
        log_level=20,
        log_file="logs/test.log",
        shared_state_provider=lambda: {},
        # ⚠ 必须注入桩（第 63 批）：默认实现会**读真锁**并 taskkill 掉
        #   **正在运行的**守护进程 ⇒ 跑一次门禁就静默停摆。
        orphan_daemon_cleaner=lambda *a, **k: 0,
        # ⚠ 也不许起真监督线程（它会轮询并可能拉起守护进程）。
        supervisor_factory=_FakeSupervisor,
        lifecycle_settings_provider=lambda: DaemonLifecycleSettings(
            graceful_join_timeout_seconds=0.25,
            terminate_join_timeout_seconds=0.5,
        ),
    )
    manager.process = process
    manager.stop_event = event
    monkeypatch.setattr("interfaces.daemon_manager.os.name", "posix")

    manager.stop()

    assert event.was_set is True
    assert process.terminated is True
    assert process.join_calls == [0.25, 0.5]
    assert manager.process is None
    assert manager.stop_event is None


def test_daemon_process_injects_ai_stream_queue(monkeypatch, tmp_path):
    """子进程入口收到 ai_queue 时必须注入 ai_stream_bus。

    `build_daemon` 是 `run_autopilot_daemon_process` **内部** import 的，
    且调用点在 `while not stop_event.is_set()` 之前——所以必须 patch
    `scripts.start_daemon.build_daemon`，patch `daemon_manager.build_daemon`
    不生效。让它抛错只是为了在注入之后立刻退出，不进入轮询循环
    （该异常会被外层 `except BaseException` 吞掉，所以这里不能断言抛出的异常）。
    """
    import scripts.start_daemon as start_daemon_module
    from interfaces import daemon_manager

    injected = {}
    monkeypatch.setattr(
        "application.engine.services.ai_stream_bus.inject_ai_stream_queue",
        lambda q: injected.setdefault("queue", q),
    )
    monkeypatch.setattr(
        "interfaces.api.middleware.logging_config.setup_logging",
        lambda **kwargs: None,
    )

    def _boom():
        raise RuntimeError("stop-after-injection")

    monkeypatch.setattr(start_daemon_module, "build_daemon", _boom)

    sentinel = object()
    # ⚠ 必须用**临时锁**（第 63 批）：原先不传 lock_path ⇒ 用的是**真锁**
    #   ⇒ 这条测试会去抢真锁（抢不到就提前退出 ⇒ 断言假失败；抢到了更糟：
    #   测试进程会变成真守护进程的持有者）。
    daemon_manager.run_autopilot_daemon_process(
        stop_event=type("E", (), {"is_set": lambda self: True})(),
        log_level=logging.INFO,
        log_file="",
        ai_queue=sentinel,
        lock_path=str(tmp_path / "inject-test.lock"),
    )

    assert injected.get("queue") is sentinel


def test_daemon_manager_start_passes_ai_queue_to_child(monkeypatch):
    """start() 必须按 (stream_queue, ai_queue, shared_state, persistence_queue) 的位置传参。

    位置传参写错不会报错，只会让 shared_state 落进 ai_queue 槽位、静默污染总线，
    所以这里直接把 args 元组钉死。
    """
    from interfaces import daemon_manager

    stream_queue = object()
    ai_queue = object()
    shared_state = {"k": "v"}
    persistence_queue = object()

    class _Queue:
        def is_consumer_running(self):
            return True

    monkeypatch.setattr(
        "application.engine.services.streaming_bus.init_streaming_bus",
        lambda: stream_queue,
    )
    monkeypatch.setattr(
        "application.engine.services.ai_stream_bus.init_ai_stream_bus",
        lambda: ai_queue,
    )
    monkeypatch.setattr(
        "application.engine.services.shared_state_repository.init_shared_state_repository",
        lambda state: object(),
    )
    monkeypatch.setattr(
        "application.engine.services.state_bootstrap.bootstrap_state",
        lambda: {},
    )
    monkeypatch.setattr(
        "application.engine.services.query_service.init_query_service",
        lambda repo: None,
    )
    monkeypatch.setattr(
        "application.engine.services.persistence_queue.initialize_persistence_queue",
        lambda: persistence_queue,
    )
    monkeypatch.setattr(
        "application.engine.services.persistence_queue.register_persistence_handlers",
        lambda: None,
    )
    monkeypatch.setattr(
        "application.engine.services.persistence_queue.get_persistence_queue",
        lambda: _Queue(),
    )

    captured = {}

    class _StartedProcess(FakeProcess):
        def start(self):
            captured["started"] = True

    def _process_factory(**kwargs):
        captured.update(kwargs)
        return _StartedProcess()

    manager = AutopilotDaemonManager(
        log_level=20,
        log_file="logs/test.log",
        shared_state_provider=lambda: shared_state,
        # ⚠ 必须注入桩（第 63 批）：默认实现会**读真锁**并 taskkill 掉
        #   **正在运行的**守护进程 ⇒ 跑一次门禁就静默停摆。
        orphan_daemon_cleaner=lambda *a, **k: 0,
        # ⚠ 也不许起真监督线程（它会轮询并可能拉起守护进程）。
        supervisor_factory=_FakeSupervisor,
        process_factory=_process_factory,
        event_factory=FakeEvent,
        # 必须注入桩：默认实现会往真实 data/daemon.pid 写一个假 PID，
        # 之后真启动时可能据此 taskkill 到无关进程。
        pid_recorder=lambda pid: None,
        pid_recorder_clear=lambda: None,
    )

    manager.start()

    assert captured["started"] is True
    assert captured["target"] is daemon_manager.run_autopilot_daemon_process
    assert captured["args"] == (
        manager.stop_event,
        20,
        "logs/test.log",
        stream_queue,
        ai_queue,
        shared_state,
        persistence_queue,
    )


# ---------------------------------------------------------------------------
# 残留进程识别
#
# 下面的命令行全部来自 2026-10-14 在本机实测枚举的真实输出，不是手写编的。
# uv 启动的 venv python 是一个常驻启动器进程，其命令行首 token 就是仓库内的
# .venv 路径；旧实现用 `"plotpilot" in cmdline` 匹配，会把这些全部 taskkill 掉。
# ---------------------------------------------------------------------------
_UV_SHIM = (
    r'"D:\WorkSpace\projects\PlotPilot\.venv\Scripts\python.exe" '
    r'-c "import time; time.sleep(14)"'
)
_UV_REAL = (
    r'"C:\Users\wake\AppData\Roaming\uv\python\cpython-3.14-windows-x86_64-none\python.exe"  '
    r'-c "import time; time.sleep(14)"'
)
_SCRIPT_SHIM = (
    r'"D:\WorkSpace\projects\PlotPilot\.venv\Scripts\python.exe" '
    r".superpowers\sdd\2026-10-14\_tools\enum_python.py"
)
_SCRIPT_REAL = (
    r'"C:\Users\wake\AppData\Roaming\uv\python\cpython-3.14-windows-x86_64-none\python.exe"  '
    r".superpowers\sdd\2026-10-14\_tools\enum_python.py"
)
_SPAWN_CHILD = (
    r'"C:\Users\wake\AppData\Roaming\uv\python\cpython-3.14-windows-x86_64-none\python.exe" '
    r'"-c" "from multiprocessing.spawn import spawn_main; '
    r'spawn_main(parent_pid=29620, pipe_handle=348)" "--multiprocessing-fork"'
)
_UVICORN_APP = (
    r'"D:\WorkSpace\projects\PlotPilot\.venv\Scripts\python.exe" '
    r"-m uvicorn interfaces.main:app --host 127.0.0.1 --port 8005"
)
_UVICORN_EXE = (
    r'"D:\WorkSpace\projects\PlotPilot\.venv\Scripts\uvicorn.exe" interfaces.main:app --reload'
)
_MODULE_SERVE = (
    r'"C:\Users\wake\AppData\Roaming\uv\python\cpython-3.14-windows-x86_64-none\python.exe"  '
    r"-m PlotPilot serve"
)
_START_DAEMON = (
    r'"D:\WorkSpace\projects\PlotPilot\.venv\Scripts\python.exe" scripts\start_daemon.py'
)

# 这些命令行都含仓库路径或 uv 基础解释器路径，绝不能被当成残留后端杀掉。
_MUST_NOT_MATCH = (_UV_SHIM, _UV_REAL, _SCRIPT_SHIM, _SCRIPT_REAL, _SPAWN_CHILD)


def test_venv_path_in_commandline_is_not_a_backend_signature():
    """解释器路径含 "plotpilot" 不构成命中理由（uv 启动器 / 任意脚本）。"""
    for cmdline in _MUST_NOT_MATCH:
        assert is_plotpilot_backend_command(cmdline) is False, cmdline


def test_backend_command_signatures_are_recognized():
    assert is_plotpilot_backend_command(_UVICORN_APP) is True
    assert is_plotpilot_backend_command(_UVICORN_EXE) is True
    assert is_plotpilot_backend_command(_MODULE_SERVE) is True
    assert is_plotpilot_backend_command(_START_DAEMON) is True


def test_bare_keyword_in_argument_is_not_enough():
    """只提到仓库名、但没表达运行后端意图的参数不应命中。

    例如用户在自己仓库里跑了个叫 plotpilot_notes.py 的脚本。
    """
    assert (
        is_plotpilot_backend_command(r'"C:\tools\python.exe" plotpilot_notes.py') is False
    )
    assert is_plotpilot_backend_command(r'"C:\tools\python.exe" serve') is False


def test_strip_executable_token_handles_quoted_and_bare_forms():
    assert strip_executable_token(_UVICORN_EXE) == "interfaces.main:app --reload"
    assert strip_executable_token(r"C:\tools\python.exe script.py") == "script.py"
    assert strip_executable_token('"C:\\tools\\python.exe"') == ""


def test_strip_executable_token_on_empty_input():
    assert strip_executable_token("") == ""
    assert strip_executable_token("   ") == ""


def _install_process_probe(monkeypatch, rows, recorded_pid=None):
    """把进程枚举 / taskkill / PID 记录全部换成桩，返回被 taskkill 的 PID 列表。"""
    killed: list[int] = []

    def _fake_run(args, *a, **kw):
        if args and args[0] == "taskkill":
            killed.append(int(args[args.index("/PID") + 1]))
        return None

    monkeypatch.setattr("interfaces.daemon_manager._list_python_processes", lambda s, log: rows)
    monkeypatch.setattr("interfaces.daemon_manager.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "interfaces.daemon_manager.get_daemon_lifecycle_settings",
        lambda: DaemonLifecycleSettings(),
    )
    monkeypatch.setattr(
        "interfaces.daemon_manager._recorded_daemon_pid", lambda: recorded_pid
    )
    # ⚠ 第 63 批：测试套件默认打开 `PLOTPILOT_SKIP_ORPHAN_CLEANUP`
    #   （不许扫真进程）⇒ 这几条要验**清扫真实行为** ⇒ 自己把它删掉。
    monkeypatch.delenv("PLOTPILOT_SKIP_ORPHAN_CLEANUP", raising=False)
    return killed


def test_cleanup_kills_leftover_backend_but_not_venv_script(monkeypatch):
    """核心回归：只杀后端，同环境下并发跑的 uv 任务必须活着。"""
    rows = [
        _ProcessRow(pid=1001, name="python.exe", cmdline=_SCRIPT_SHIM, parent_pid=500),
        _ProcessRow(pid=1002, name="python.exe", cmdline=_UV_SHIM, parent_pid=500),
        _ProcessRow(pid=1003, name="python.exe", cmdline=_UVICORN_APP, parent_pid=500),
        _ProcessRow(pid=1004, name="plotpilot-backend.exe", cmdline="", parent_pid=500),
    ]
    killed = _install_process_probe(monkeypatch, rows)

    cleanup_orphan_python_processes()

    assert killed == [1003, 1004]


def test_cleanup_never_kills_self_or_ancestors(monkeypatch):
    """自身与祖先即使命令行命中也不能杀。"""
    self_pid = os.getpid()
    fake_parent = 999_001
    rows = [
        _ProcessRow(pid=self_pid, name="python.exe", cmdline=_UVICORN_APP, parent_pid=fake_parent),
        _ProcessRow(pid=fake_parent, name="python.exe", cmdline=_UVICORN_APP, parent_pid=0),
        _ProcessRow(pid=1003, name="python.exe", cmdline=_UVICORN_APP, parent_pid=500),
    ]
    killed = _install_process_probe(monkeypatch, rows)

    cleanup_orphan_python_processes()

    assert killed == [1003]


def test_cleanup_kills_recorded_daemon_child(monkeypatch):
    """上一实例记录的守护子进程 PID 仍能被清理（它没有任何命令行标识）。"""
    rows = [
        _ProcessRow(pid=2002, name="python.exe", cmdline=_SPAWN_CHILD, parent_pid=1999),
        _ProcessRow(pid=1003, name="python.exe", cmdline=_UVICORN_APP, parent_pid=500),
    ]
    killed = _install_process_probe(monkeypatch, rows, recorded_pid=2002)

    cleanup_orphan_python_processes()

    assert killed == [1003, 2002]


def test_cleanup_ignores_recorded_pid_when_no_longer_a_spawn_child(monkeypatch):
    """PID 被系统回收给别人后，记录里的 PID 不能被误杀。"""
    rows = [
        _ProcessRow(pid=2002, name="python.exe", cmdline="C:\\other\\python.exe other_app.py", parent_pid=1999),
        _ProcessRow(pid=1003, name="python.exe", cmdline=_UVICORN_APP, parent_pid=500),
    ]
    killed = _install_process_probe(monkeypatch, rows, recorded_pid=2002)

    cleanup_orphan_python_processes()

    assert killed == [1003]


def test_daemon_manager_records_and_clears_pid(monkeypatch):
    """start() 落盘守护子进程 PID，stop() 清掉它。"""
    from interfaces import daemon_manager

    started: list[str] = []
    recorded: list[int] = []
    cleared: list[bool] = []

    class _P(FakeProcess):
        pid = 7777

        def start(self):
            started.append("started")

    monkeypatch.setattr(
        "application.engine.services.streaming_bus.init_streaming_bus", lambda: object()
    )
    monkeypatch.setattr(
        "application.engine.services.ai_stream_bus.init_ai_stream_bus", lambda: object()
    )
    monkeypatch.setattr(
        "application.engine.services.shared_state_repository.init_shared_state_repository",
        lambda state: object(),
    )
    monkeypatch.setattr(
        "application.engine.services.state_bootstrap.bootstrap_state", lambda: {}
    )
    monkeypatch.setattr(
        "application.engine.services.query_service.init_query_service", lambda repo: None
    )
    monkeypatch.setattr(
        "application.engine.services.persistence_queue.initialize_persistence_queue",
        lambda: object(),
    )
    monkeypatch.setattr(
        "application.engine.services.persistence_queue.register_persistence_handlers",
        lambda: None,
    )

    class _Q:
        def is_consumer_running(self):
            return True

    monkeypatch.setattr(
        "application.engine.services.persistence_queue.get_persistence_queue", lambda: _Q()
    )

    manager = AutopilotDaemonManager(
        log_level=20,
        log_file="logs/test.log",
        shared_state_provider=lambda: {},
        # ⚠ 必须注入桩（第 63 批）：默认实现会**读真锁**并 taskkill 掉
        #   **正在运行的**守护进程 ⇒ 跑一次门禁就静默停摆。
        orphan_daemon_cleaner=lambda *a, **k: 0,
        # ⚠ 也不许起真监督线程（它会轮询并可能拉起守护进程）。
        supervisor_factory=_FakeSupervisor,
        process_factory=lambda **kw: _P(),
        event_factory=FakeEvent,
        pid_recorder=recorded.append,
        pid_recorder_clear=lambda: cleared.append(True),
    )
    manager.start()
    assert started == ["started"]
    assert recorded == [7777]

    monkeypatch.setattr("interfaces.daemon_manager.os.name", "posix")
    manager.stop()
    assert cleared == [True]
