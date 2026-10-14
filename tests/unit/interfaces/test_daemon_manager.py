import logging

from interfaces.api.settings import BackendSettings
from interfaces.daemon_manager import (
    AutopilotDaemonManager,
    DaemonLifecycleSettings,
    DaemonStatus,
    is_expected_daemon_shutdown_exception,
)


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


def test_daemon_process_injects_ai_stream_queue(monkeypatch):
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
    daemon_manager.run_autopilot_daemon_process(
        stop_event=type("E", (), {"is_set": lambda self: True})(),
        log_level=logging.INFO,
        log_file="",
        ai_queue=sentinel,
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
        process_factory=_process_factory,
        event_factory=FakeEvent,
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
