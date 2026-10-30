"""Autopilot daemon process lifecycle management."""
from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict

from application.core.config.config_loader import get_config
from application.core.config.runtime_settings_utils import positive_float, section_value
from infrastructure.runtime.daemon_lock import DaemonLock, is_lock_held, read_holder_pid
from interfaces.api.settings import BackendSettings, get_backend_settings


logger = logging.getLogger(__name__)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

#: **测试期间不许扫真进程**（第 63 批）。
#:
#: 为什么需要：单测里 ``TestClient(app).get("/health")`` **会跑 app 的 lifespan**
#: ⇒ ``BackendLifecycle.startup()`` ⇒ ``cleanup_orphans()``
#: ⇒ 真的 ``cleanup_orphan_python_processes()`` ⇒ 扫**真进程表**
#: ⇒ 按命令行匹配把**正在运行的** API（``interfaces.main``）与守护子进程
#: （PID 记录）**一起 taskkill**。
#: ⇒ ⇒ 2026-10-30 实测：**跑一次门禁，正在跑的后端就没了**（无任何日志、无崩溃事件）。
#:
#: ⇒ 打开这个变量 ⇒ 两个清扫入口都直接返回 ⇒ **测试套件永远不会杀真进程**。
#: （要验清扫真实行为的单测自己 ``monkeypatch.delenv`` 掉它。）
SKIP_ORPHAN_CLEANUP_ENV = "PLOTPILOT_SKIP_ORPHAN_CLEANUP"


def _orphan_cleanup_skipped() -> bool:
    """清扫是否被开关跳过。"""
    value = os.environ.get(SKIP_ORPHAN_CLEANUP_ENV, "")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class DaemonStatus:
    running: bool
    pid: int | None


@dataclass(frozen=True)
class DaemonLifecycleSettings:
    error_sleep_seconds: float = 10.0
    powershell_list_timeout_seconds: float = 15.0
    wmic_list_timeout_seconds: float = 8.0
    orphan_taskkill_timeout_seconds: float = 5.0
    graceful_join_timeout_seconds: float = 2.0
    terminate_join_timeout_seconds: float = 1.0
    sigkill_join_timeout_seconds: float = 1.0
    windows_taskkill_timeout_seconds: float = 3.0
    #: 拿不到单实例锁时的**重试窗口**（第 18 批 · G）
    #: ⇒ 旧守护进程正在退出时，新守护进程等它把锁放掉，而不是直接放弃。
    daemon_lock_retry_seconds: float = 10.0


def get_daemon_lifecycle_settings() -> DaemonLifecycleSettings:
    daemon = getattr(get_config(), "daemon", None)
    section = getattr(daemon, "lifecycle", None)
    defaults = DaemonLifecycleSettings()
    return DaemonLifecycleSettings(
        error_sleep_seconds=positive_float(
            section_value(section, "error_sleep_seconds", defaults.error_sleep_seconds),
            defaults.error_sleep_seconds,
        ),
        powershell_list_timeout_seconds=positive_float(
            section_value(
                section,
                "powershell_list_timeout_seconds",
                defaults.powershell_list_timeout_seconds,
            ),
            defaults.powershell_list_timeout_seconds,
        ),
        wmic_list_timeout_seconds=positive_float(
            section_value(section, "wmic_list_timeout_seconds", defaults.wmic_list_timeout_seconds),
            defaults.wmic_list_timeout_seconds,
        ),
        orphan_taskkill_timeout_seconds=positive_float(
            section_value(
                section,
                "orphan_taskkill_timeout_seconds",
                defaults.orphan_taskkill_timeout_seconds,
            ),
            defaults.orphan_taskkill_timeout_seconds,
        ),
        graceful_join_timeout_seconds=positive_float(
            section_value(
                section,
                "graceful_join_timeout_seconds",
                defaults.graceful_join_timeout_seconds,
            ),
            defaults.graceful_join_timeout_seconds,
        ),
        terminate_join_timeout_seconds=positive_float(
            section_value(
                section,
                "terminate_join_timeout_seconds",
                defaults.terminate_join_timeout_seconds,
            ),
            defaults.terminate_join_timeout_seconds,
        ),
        sigkill_join_timeout_seconds=positive_float(
            section_value(
                section,
                "sigkill_join_timeout_seconds",
                defaults.sigkill_join_timeout_seconds,
            ),
            defaults.sigkill_join_timeout_seconds,
        ),
        windows_taskkill_timeout_seconds=positive_float(
            section_value(
                section,
                "windows_taskkill_timeout_seconds",
                defaults.windows_taskkill_timeout_seconds,
            ),
            defaults.windows_taskkill_timeout_seconds,
        ),
        daemon_lock_retry_seconds=positive_float(
            section_value(
                section,
                "daemon_lock_retry_seconds",
                defaults.daemon_lock_retry_seconds,
            ),
            defaults.daemon_lock_retry_seconds,
        ),
    )


@dataclass(frozen=True)
class DaemonSupervisorSettings:
    """守护进程监督者的口径（第 63 批）。

    全部来自 ``config/performance.yaml`` 的 ``daemon.supervisor`` 段。
    """

    #: 总闸：关掉 ⇒ 监督者完全不启动（守护进程死了也不管）。
    enabled: bool = True
    #: 多久看一次守护进程还活着。
    poll_seconds: float = 15.0
    #: 第一次拉起前的等待（之后指数退避）。
    initial_backoff_seconds: float = 30.0
    #: 退避上限。
    max_backoff_seconds: float = 300.0
    #: 连续拉起多少次之后放弃（不再自动拉起，但仍继续记 error）。
    max_restarts: int = 5
    #: 守护进程稳定存活多久之后，退避计数清零。
    reset_after_seconds: float = 1800.0


def get_daemon_supervisor_settings() -> DaemonSupervisorSettings:
    """读 ``daemon.supervisor`` 段（读不到 ⇒ 用默认值）。"""
    daemon = getattr(get_config(), "daemon", None)
    section = section_value(daemon, "supervisor", None)
    defaults = DaemonSupervisorSettings()
    return DaemonSupervisorSettings(
        enabled=bool(section_value(section, "enabled", defaults.enabled)),
        poll_seconds=positive_float(
            section_value(section, "poll_seconds", defaults.poll_seconds),
            defaults.poll_seconds,
        ),
        initial_backoff_seconds=positive_float(
            section_value(
                section, "initial_backoff_seconds", defaults.initial_backoff_seconds
            ),
            defaults.initial_backoff_seconds,
        ),
        max_backoff_seconds=positive_float(
            section_value(section, "max_backoff_seconds", defaults.max_backoff_seconds),
            defaults.max_backoff_seconds,
        ),
        max_restarts=max(
            1, int(section_value(section, "max_restarts", defaults.max_restarts))
        ),
        reset_after_seconds=positive_float(
            section_value(section, "reset_after_seconds", defaults.reset_after_seconds),
            defaults.reset_after_seconds,
        ),
    )


class DaemonSupervisor:
    """守护进程死了自动拉起（带退避 + 上限 + 显式日志）。

    为什么需要它（2026-10-30 实测）
    --------------------------------
    守护子进程被**外部静默硬杀**过：无日志、无 traceback、Windows 事件日志无记录
    ⇒ API 还活着、``/health`` 仍 200，但**自动驾驶静默停摆**（书不写了），
    而界面上完全看不出异常（前端从不读 ``daemon_process.running``）。

    三条安全约束（少一条都会闯祸）
    ------------------------------
    1. **只在单实例锁空闲时才拉起** —— 否则 ``manager.start()`` 里的
       ``cleanup_orphan_daemon()`` 会去杀**另一个实例**的守护进程
       ⇒ 两个实例互抢、抖动。
    2. **退避 + 次数上限** —— 防重启风暴（被反复杀时不会变成拉锯）。
    3. **停止后不再拉起** —— 关机流程里绝不能把守护进程复活。

    ``_check_once`` 的返回值（动作名，便于测试与日志）::

        alive             守护进程活着 ⇒ 什么都不做
        locked_elsewhere  守护进程不在，但锁被别人持有 ⇒ 不拉起（不是我们的事）
        backoff           退避窗口内 ⇒ 先等等
        exhausted         超过拉起次数上限 ⇒ 不再拉起（但继续记 error）
        restarted         拉起成功
        failed            拉起失败
        stopped           监督者已被要求停止
    """

    def __init__(
        self,
        manager: "AutopilotDaemonManager",
        settings: DaemonSupervisorSettings,
        logger_: logging.Logger | None = None,
        *,
        lock_probe: Callable[[], bool] = is_lock_held,
        clock: Callable[[], float] = time.time,
        state_publisher: Callable[[Dict[str, Any]], None] | None = None,
    ) -> None:
        self._manager = manager
        self._settings = settings
        self._logger = logger_ or logger
        self._lock_probe = lock_probe
        self._clock = clock
        #: 状态发布口（第 64 批）：把「重启过几次 / 上次错误」写进共享内存
        #: ⇒ ``/autopilot/status`` 能带出去 ⇒ 前端看得见。
        self._state_publisher = state_publisher
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._restart_attempts = 0
        self._next_allowed_at = 0.0
        self._alive_since: float | None = None
        self._last_restart_at: float | None = None
        self._last_error: str = ""

    # ── 生命周期 ────────────────────────────────────────────────────────

    def start(self) -> None:
        """启动监督线程（已在跑 ⇒ 什么都不做）。"""
        if not self._settings.enabled:
            self._logger.info("守护进程监督者已禁用（daemon.supervisor.enabled=false）")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="DaemonSupervisor", daemon=True
        )
        self._thread.start()
        self._logger.info(
            "守护进程监督者已启动（轮询 %.0fs，拉起上限 %d 次）",
            self._settings.poll_seconds,
            self._settings.max_restarts,
        )

    def stop(self) -> None:
        """停止监督线程（停止后**绝不再拉起**守护进程）。"""
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    # ── 状态（给 /health 用）────────────────────────────────────────────

    def state(self) -> Dict[str, Any]:
        return {
            "enabled": self._settings.enabled,
            "running": self._thread is not None and self._thread.is_alive(),
            "restart_attempts": self._restart_attempts,
            "last_restart_at": self._last_restart_at,
            "last_error": self._last_error,
        }

    # ── 主循环 ──────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self._stop_event.wait(self._settings.poll_seconds):
                return
            try:
                self._check_once()
            except Exception as exc:  # noqa: BLE001 —— 监督者自己不能崩
                self._logger.error("守护进程监督者自身异常: %s", exc, exc_info=True)

    def _check_once(self) -> str:
        """检查一次守护进程存活状态，返回动作名（并把状态发布出去）。"""
        action = self._decide()
        self._publish_state()
        return action

    def _publish_state(self) -> None:
        """把当前状态发布出去（发布失败不许拖垮监督者）。"""
        if self._state_publisher is None:
            return
        try:
            self._state_publisher(self.state())
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("发布监督者状态失败（忽略）: %s", exc)

    def _decide(self) -> str:
        """判断这一轮该做什么（返回动作名）。"""
        if self._manager.status().running:
            if self._alive_since is None:
                self._alive_since = self._clock()
            self._maybe_reset_backoff()
            return "alive"

        # 守护进程不在了
        self._alive_since = None

        if self._stop_event.is_set():
            return "stopped"

        # 🔒 锁被别人持有 ⇒ 另一个实例的守护进程在跑 ⇒ 不许抢
        if self._lock_probe():
            self._logger.info(
                "守护进程不在了，但单实例锁被占用（别的实例在跑守护进程）⇒ 不拉起"
            )
            return "locked_elsewhere"

        now = self._clock()
        if now < self._next_allowed_at:
            return "backoff"

        if self._restart_attempts >= self._settings.max_restarts:
            self._logger.error(
                "守护进程已连续拉起失败 %d 次（上限 %d）⇒ 停止自动拉起，需人工介入",
                self._restart_attempts,
                self._settings.max_restarts,
            )
            return "exhausted"

        self._restart_attempts += 1
        self._logger.error("检测到守护进程已死（第 %d 次拉起）", self._restart_attempts)
        try:
            self._manager.start()
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            self._logger.error("拉起守护进程失败: %s", exc, exc_info=True)
            self._schedule_next()
            return "failed"

        self._last_error = ""
        self._last_restart_at = now
        self._logger.info("守护进程已重新拉起（第 %d 次）", self._restart_attempts)
        self._schedule_next()
        return "restarted"

    def _schedule_next(self) -> None:
        """排下一次允许拉起的时间（指数退避，封顶 ``max_backoff_seconds``）。"""
        delay = min(
            self._settings.initial_backoff_seconds
            * (2 ** max(0, self._restart_attempts - 1)),
            self._settings.max_backoff_seconds,
        )
        self._next_allowed_at = self._clock() + delay

    def _maybe_reset_backoff(self) -> None:
        """守护进程稳定存活够久 ⇒ 退避计数清零（下次真死时从头再来）。"""
        if self._restart_attempts == 0 or self._alive_since is None:
            return
        if self._clock() - self._alive_since >= self._settings.reset_after_seconds:
            self._restart_attempts = 0
            self._next_allowed_at = 0.0


def is_expected_daemon_shutdown_exception(exc: BaseException) -> bool:
    """Treat hot-reload and process-stop interruptions as normal daemon exits."""
    current = exc
    visited = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, (KeyboardInterrupt, asyncio.CancelledError)):
            return True
        current = current.__cause__ or current.__context__
    return False


def run_autopilot_daemon_process(
    stop_event,
    log_level: int,
    log_file: str,
    stream_queue=None,
    ai_queue=None,
    shared_state=None,
    persistence_queue=None,
    lock_path: str | None = None,
) -> None:
    """Run the autopilot daemon in an isolated child process."""
    from interfaces.api.middleware.logging_config import setup_logging

    process_logger = logging.getLogger(__name__)
    lifecycle_settings = get_daemon_lifecycle_settings()
    setup_logging(level=log_level, log_file=log_file)

    # 🔒 单实例锁（第 18 批 · G）：**同一份数据只允许一个守护进程**。
    # ⚠ 为什么必须有：守护子进程的 PID 记录是单槽的 ⇒ 别的 API 实例（Tauri /
    #   第二个 dev server）启停时会覆盖或清掉它 ⇒ 重启后端时**旧守护进程活下来**
    #   ⇒ 两个守护进程同时跑同一本书（实测：同一章被审计两次，一次旧代码一次新代码）。
    # ⚠ 锁由**操作系统**在进程退出时释放 ⇒ 进程被强杀也不留「假锁」。
    daemon_lock = DaemonLock(Path(lock_path) if lock_path else None)
    lock_attempt = daemon_lock.acquire()
    if not lock_attempt.acquired:
        # 旧守护进程可能正在退出（锁还没被 OS 释放）⇒ 短暂重试，别急着放弃。
        retry_deadline = time.time() + lifecycle_settings.daemon_lock_retry_seconds
        while time.time() < retry_deadline and not stop_event.is_set():
            time.sleep(0.5)
            lock_attempt = daemon_lock.acquire()
            if lock_attempt.acquired:
                break
    if not lock_attempt.acquired:
        process_logger.warning(
            "已有守护进程在运行（单实例锁被占用，持有者 PID=%s），本进程退出：%s",
            lock_attempt.holder_pid,
            lock_attempt.detail,
        )
        return
    process_logger.info("守护进程已拿到单实例锁（锁文件=%s）", daemon_lock.path)

    if stream_queue is not None:
        from application.engine.services.streaming_bus import inject_stream_queue

        inject_stream_queue(stream_queue)
        process_logger.info("守护进程：流式队列已注入")

    if ai_queue is not None:
        try:
            from application.engine.services.ai_stream_bus import inject_ai_stream_queue

            inject_ai_stream_queue(ai_queue)
            process_logger.info("守护进程：AI 事件队列已注入")
        except Exception as exc:
            process_logger.warning("AI 事件队列注入失败（可忽略）: %s", exc)

    if shared_state is not None:
        try:
            import sys

            sys.modules["__shared_state"] = shared_state
            from application.engine.services.shared_state_repository import (
                get_shared_state_repository,
                inject_shared_dict,
            )

            inject_shared_dict(shared_state)
            process_logger.info("守护进程：共享状态字典已注入")

            from application.engine.services.state_publisher import get_state_publisher

            get_state_publisher()
            process_logger.info("守护进程：状态发布器已初始化")
        except Exception as exc:
            process_logger.warning("共享状态注入失败（可忽略）: %s", exc)

    if persistence_queue is not None:
        try:
            from application.engine.services.persistence_queue import inject_persistence_queue

            inject_persistence_queue(persistence_queue)
            process_logger.info("守护进程：持久化队列已注入")
        except Exception as exc:
            process_logger.warning("持久化队列注入失败（可忽略）: %s", exc)

    try:
        from application.engine.services.novel_stop_signal import inject_novel_stop_events

        inject_novel_stop_events()
    except Exception as exc:
        process_logger.debug("小说停止信号模块初始化失败（可忽略）: %s", exc)

    try:
        from scripts.start_daemon import build_daemon

        daemon = build_daemon()
        process_logger.info("守护进程已启动（独立进程），开始轮询...")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        process_logger.info("守护进程：持久化事件循环已创建")

        while not stop_event.is_set():
            try:
                try:
                    from application.engine.services.streaming_bus import streaming_bus

                    streaming_bus.consume_stop_signals()
                except Exception:
                    pass

                try:
                    from application.engine.services.shared_state_repository import (
                        get_shared_state_repository,
                    )

                    get_shared_state_repository().update_daemon_heartbeat()
                except Exception:
                    pass

                active_novels = daemon._get_active_novels()
                if active_novels:
                    for novel in active_novels:
                        if stop_event.is_set():
                            break
                        loop.run_until_complete(daemon._process_novel(novel))

                stop_event.wait(timeout=daemon.poll_interval)

            except BaseException as exc:
                if stop_event.is_set() or is_expected_daemon_shutdown_exception(exc):
                    process_logger.info("守护进程在停止/热重载期间中断，正常退出")
                    break
                process_logger.error("守护进程异常: %s", exc, exc_info=True)
                stop_event.wait(timeout=lifecycle_settings.error_sleep_seconds)

    except BaseException as exc:
        if stop_event.is_set() or is_expected_daemon_shutdown_exception(exc):
            process_logger.info("守护进程收到停止信号，正常退出")
        else:
            process_logger.error("守护进程初始化失败: %s", exc, exc_info=True)
    finally:
        process_logger.info("守护进程已停止")
        try:
            daemon_lock.release()
        except Exception as exc:  # noqa: BLE001 —— 释放失败不该影响退出
            process_logger.debug(f"释放守护进程单实例锁失败（可忽略）: {exc}")


# ---------------------------------------------------------------------------
# 残留进程识别
#
# 只认「命令行明确表达了运行 PlotPilot 意图」的进程，绝不做子串匹配。
#
# 为什么子串匹配危险（Windows 实测，2026-10）：
#   本仓库 venv 路径 = D:\WorkSpace\projects\PlotPilot\.venv\Scripts\python.exe
#   uv 启动的 venv python 是一个**常驻启动器进程**，其命令行首 token 就是这条路径。
#   于是 `"plotpilot" in cmdline.lower()` 对下列进程一律为真：
#     · 用户在终端跑的任意脚本 / REPL
#     · 与后端并发运行的 `uv run pytest ...`、`uv run python xxx.py`
#   启动时一次 taskkill /F 会把它们全部杀掉，而日志显示为「清理残留进程」。
#
# 因此 argv[0]（可执行文件路径）永不参与匹配，命中条件收敛为：
#   1. 进程名是 plotpilot-backend.exe（Tauri 打包版后端，名称本身即专用标识）
#   2. 参数里出现 interfaces.main（uvicorn interfaces.main:app / python interfaces/main.py）
#   3. 参数里出现 start_daemon.py（独立生产入口）
#   4. 参数里有 -m PlotPilot（python -m PlotPilot serve）
# ---------------------------------------------------------------------------
_BACKEND_EXE_NAME = "plotpilot-backend.exe"
_BACKEND_ARG_MARKERS = ("interfaces.main", "start_daemon.py")
_BACKEND_MODULE_NAME = "plotpilot"
_SPAWN_FORK_MARKER = "--multiprocessing-fork"

# multiprocessing 派生的守护子进程命令行形如：
#   python.exe -c "from multiprocessing.spawn import spawn_main; spawn_main(parent_pid=…, pipe_handle=…)" --multiprocessing-fork
# 它不含上面任何标识，且实测命令行里始终是基础解释器路径（不含仓库名），
# 所以关键字匹配对它本就无效——只能靠 start() 写下的 PID 记录来清理。


@dataclass(frozen=True)
class _ProcessRow:
    """一条进程枚举结果。parent_pid 为 None 表示枚举源没提供（wmic 回退路径）。"""

    pid: int
    name: str
    cmdline: str
    parent_pid: int | None = None


def strip_executable_token(cmdline: str) -> str:
    """去掉命令行首 token（可执行文件路径），只留下参数部分。

    路径里带仓库名是常态（本仓库 venv 路径即含 "plotpilot"），所以必须先剥离
    再匹配，否则解释器路径本身就会把每个用该 venv 的进程都算进来。
    """
    text = cmdline.strip()
    if not text:
        return ""
    if text.startswith('"'):
        closing = text.find('"', 1)
        if closing == -1:
            return ""
        return text[closing + 1 :].strip()
    _, separator, tail = text.partition(" ")
    return tail.strip() if separator else ""


def is_plotpilot_backend_command(cmdline: str) -> bool:
    """命令行是否明确表达了「运行 PlotPilot 后端」的意图。

    只看参数部分，绝不看解释器路径或裸子串。
    """
    args = strip_executable_token(cmdline)
    if not args:
        return False
    low = args.lower()
    if any(marker in low for marker in _BACKEND_ARG_MARKERS):
        return True
    tokens = [token for token in args.replace('"', " ").split() if token]
    lowered = [token.lower() for token in tokens]
    for flag, target in zip(lowered, lowered[1:]):
        if flag == "-m" and target.replace("\\", "/").rsplit("/", 1)[-1] == _BACKEND_MODULE_NAME:
            return True
    return False


def _is_backend_process(row: _ProcessRow) -> bool:
    """按进程名或命令行判定是否 PlotPilot 后端进程。"""
    if (row.name or "").strip().lower() == _BACKEND_EXE_NAME:
        return True
    return is_plotpilot_backend_command(row.cmdline)


_PS_LIST_SCRIPT = r"""$ErrorActionPreference = 'SilentlyContinue'
Get-CimInstance Win32_Process | ForEach-Object {
  $nl = ([string]$_.Name).ToLowerInvariant()
  if ($nl -notin @('python.exe','python3.exe','pythonw.exe','plotpilot-backend.exe')) { return }
  $cl = if ($null -eq $_.CommandLine) { '' } else { [string]$_.CommandLine }
  $cl = $cl -replace "`t", ' '
  [Console]::Out.WriteLine($_.ProcessId.ToString() + [char]9 + $_.ParentProcessId.ToString() + [char]9 + $_.Name + [char]9 + $cl)
}
"""


def _parse_powershell_rows(stdout: str) -> list[_ProcessRow]:
    rows: list[_ProcessRow] = []
    # 不能对整行 strip：命令行为空时行尾那一个分隔符会被吃掉，字段数就不够了。
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid_text = parts[0].strip()
        if not pid_text.isdigit():
            continue
        parent_text = parts[1].strip()
        rows.append(
            _ProcessRow(
                pid=int(pid_text),
                name=parts[2].strip(),
                cmdline=parts[3].strip() if len(parts) > 3 else "",
                parent_pid=int(parent_text) if parent_text.isdigit() else None,
            )
        )
    return rows


def _list_via_powershell(lifecycle_settings: DaemonLifecycleSettings) -> list[_ProcessRow]:
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _PS_LIST_SCRIPT,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=lifecycle_settings.powershell_list_timeout_seconds,
        creationflags=NO_WINDOW,
    )
    if result.returncode != 0:
        return []
    return _parse_powershell_rows(result.stdout)


def _list_via_wmic(lifecycle_settings: DaemonLifecycleSettings) -> list[_ProcessRow]:
    """wmic 回退路径。

    wmic 已在新版 Windows 移除，且它不给进程名，所以这里 name 留空——
    ``plotpilot-backend.exe`` 的按名匹配只在 PowerShell 路径生效。
    """
    result = subprocess.run(
        [
            "wmic",
            "process",
            "where",
            "name='python.exe' or name='python3.exe' or name='plotpilot-backend.exe'",
            "get",
            "processid,commandline",
        ],
        capture_output=True,
        text=True,
        timeout=lifecycle_settings.wmic_list_timeout_seconds,
        creationflags=NO_WINDOW,
    )
    if result.returncode != 0:
        return []
    rows: list[_ProcessRow] = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line or "CommandLine" in line:
            continue
        for part in reversed(line.split()):
            if part.isdigit():
                rows.append(_ProcessRow(pid=int(part), name="", cmdline=line))
                break
    return rows


def _list_python_processes(lifecycle_settings: DaemonLifecycleSettings, log: logging.Logger) -> list[_ProcessRow]:
    try:
        rows = _list_via_powershell(lifecycle_settings)
    except OSError as exc:
        log.debug("PowerShell 枚举进程不可用: %s", exc)
        rows = []
    except subprocess.TimeoutExpired:
        log.warning("PowerShell 枚举进程超时，尝试 wmic")
        rows = []
    if rows:
        return rows
    try:
        return _list_via_wmic(lifecycle_settings)
    except OSError as exc:
        log.debug("wmic 枚举进程不可用: %s", exc)
    except subprocess.TimeoutExpired:
        log.warning("wmic 枚举进程超时")
    return []


def _protected_pids(rows: list[_ProcessRow]) -> set[int]:
    """自身 + 所有祖先 PID，永远不碰。

    只排除 current_pid 是不够的：清理发生在启动期，父进程往往正是拉起本后端的
    shell / IDE / uvicorn 父进程。
    """
    by_pid = {row.pid: row for row in rows}
    protected = {os.getpid()}
    current = os.getpid()
    visited: set[int] = set()
    while current in by_pid and current not in visited:
        visited.add(current)
        parent_pid = by_pid[current].parent_pid
        if not parent_pid or parent_pid <= 0:
            break
        protected.add(parent_pid)
        current = parent_pid
    return protected


def _daemon_pid_path() -> Path:
    from application.paths import resolve_runtime_data_path

    return resolve_runtime_data_path("daemon.pid")


def record_daemon_pid(pid: int) -> None:
    """把守护子进程 PID 落盘，供启动期清理孤儿进程使用。"""
    try:
        _daemon_pid_path().write_text(
            json.dumps({"pid": int(pid), "owner_pid": os.getpid()}),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("记录守护进程 PID 失败（可忽略）: %s", exc)


def clear_daemon_pid() -> None:
    """清掉守护进程 PID 记录。"""
    try:
        _daemon_pid_path().unlink(missing_ok=True)
    except Exception as exc:
        logger.debug("清理守护进程 PID 记录失败（可忽略）: %s", exc)


def _recorded_daemon_pid() -> int | None:
    try:
        path = _daemon_pid_path()
        if not path.is_file():
            return None
        pid = int(json.loads(path.read_text(encoding="utf-8"))["pid"])
    except Exception:
        return None
    return pid if pid > 0 else None


def _taskkill(row: _ProcessRow, lifecycle_settings: DaemonLifecycleSettings, log: logging.Logger, reason: str) -> int:
    try:
        log.info("清理残留进程 PID=%s（%s）: %s...", row.pid, reason, row.cmdline[:80])
        subprocess.run(
            ["taskkill", "/F", "/PID", str(row.pid)],
            capture_output=True,
            timeout=lifecycle_settings.orphan_taskkill_timeout_seconds,
            creationflags=NO_WINDOW,
        )
        return 1
    except Exception as exc:
        log.warning("清理进程 %s 失败: %s", row.pid, exc)
        return 0


def cleanup_orphan_python_processes(logger_: logging.Logger | None = None) -> None:
    """清理上一次运行残留的 PlotPilot 后端 / 守护子进程（仅 Windows 调用）。

    两条命中路径：
      1. 进程名或命令行明确表达了运行 PlotPilot 后端（见 is_plotpilot_backend_command）
      2. 上一实例写下的守护子进程 PID，且该进程确实是 multiprocessing 派生子进程

    自身与所有祖先进程一律跳过；解释器路径含 "plotpilot" 不构成命中理由。
    """
    log = logger_ or logger
    if _orphan_cleanup_skipped():
        log.info(
            "已按 %s 跳过残留进程清扫（测试期间：不许杀掉开发者正在跑的后端）",
            SKIP_ORPHAN_CLEANUP_ENV,
        )
        return
    lifecycle_settings = get_daemon_lifecycle_settings()
    log.info("检查残留进程（当前 PID=%s）...", os.getpid())

    try:
        rows = _list_python_processes(lifecycle_settings, log)
    except Exception as exc:
        log.warning("枚举 Python 进程失败，跳过残留清理: %s", exc)
        return

    protected = _protected_pids(rows)
    killed_count = 0

    for row in rows:
        if row.pid in protected or not _is_backend_process(row):
            continue
        killed_count += _taskkill(row, lifecycle_settings, log, reason="命令行匹配")

    recorded_pid = _recorded_daemon_pid()
    if recorded_pid is not None and recorded_pid not in protected:
        recorded_row = next((row for row in rows if row.pid == recorded_pid), None)
        if recorded_row is not None and _SPAWN_FORK_MARKER in recorded_row.cmdline.lower():
            killed_count += _taskkill(recorded_row, lifecycle_settings, log, reason="守护子进程 PID 记录")

    if killed_count > 0:
        log.info("已清理 %s 个残留进程", killed_count)
    else:
        log.info("未发现残留进程")


def cleanup_orphan_daemon(
    logger_: logging.Logger | None = None,
    lock_path: str | os.PathLike[str] | None = None,
) -> int:
    """终止「上一个实例留下的守护进程」（第 18 批 · G）。

    为什么单列一条路径
    ------------------
    守护子进程的 PID 记录（``daemon.pid``）是**单槽**的 ⇒ 别的 API 实例
    （Tauri 桌面端 / 第二个 dev server / 测试实例）启停时会**覆盖**或**清掉**它
    ⇒ 靠它找不到真正还活着的孤儿 ⇒ 实测「重启后端 ⇒ 两个守护进程同时跑同一本书」。

    ⇒ 改看**单实例锁**里记的持有者 pid：
      * 锁是 OS 级文件锁 ⇒ 进程退出即释放 ⇒ 不会留「假锁」；
      * 只有**我们的**守护进程会写这个文件 ⇒ 认得准，
        不会像「按命令行乱杀 mp 子进程」那样误杀 API 自己的 ``multiprocessing.Manager()``
        （它与守护子进程命令行**同签名**）。

    返回被杀掉的进程数。
    """
    log = logger_ or logger
    if _orphan_cleanup_skipped():
        log.info(
            "已按 %s 跳过孤儿守护进程清理（测试期间：不许杀掉正在运行的守护进程）",
            SKIP_ORPHAN_CLEANUP_ENV,
        )
        return 0
    lifecycle_settings = get_daemon_lifecycle_settings()

    holder_pid = read_holder_pid(Path(lock_path) if lock_path else None)
    if holder_pid is None:
        return 0
    if holder_pid == os.getpid():
        return 0

    try:
        rows = _list_python_processes(lifecycle_settings, log)
    except Exception as exc:  # noqa: BLE001 —— 枚举失败就跳过，不阻断启动
        log.warning("枚举 Python 进程失败，跳过孤儿守护进程清理: %s", exc)
        return 0

    holder_row = next((row for row in rows if row.pid == holder_pid), None)
    if holder_row is None:
        log.info("单实例锁里的守护进程 PID=%s 已不存在（陈旧记录），无需清理", holder_pid)
        return 0

    log.warning(
        "发现上一个实例遗留的守护进程 PID=%s（%s），终止它"
        "（否则两个守护进程会同时跑同一本书）",
        holder_pid,
        holder_row.cmdline[:80],
    )
    return _taskkill(holder_row, lifecycle_settings, log, reason="单实例锁持有者")


class AutopilotDaemonManager:
    """Owns the API process view of the autopilot daemon child process."""

    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        log_level: int,
        log_file: str,
        shared_state_provider: Callable[[], dict],
        settings_provider: Callable[[], BackendSettings] = get_backend_settings,
        lifecycle_settings_provider: Callable[[], DaemonLifecycleSettings] = get_daemon_lifecycle_settings,
        process_factory: Callable[..., Any] = multiprocessing.Process,
        event_factory: Callable[..., Any] = multiprocessing.Event,
        pid_recorder: Callable[[int], None] = record_daemon_pid,
        pid_recorder_clear: Callable[[], None] = clear_daemon_pid,
        orphan_daemon_cleaner: Callable[..., int] = cleanup_orphan_daemon,
        supervisor_settings_provider: Callable[[], DaemonSupervisorSettings] = (
            get_daemon_supervisor_settings
        ),
        supervisor_factory: Callable[["AutopilotDaemonManager"], "DaemonSupervisor"] | None = None,
        supervisor_state_publisher: Callable[[Dict[str, Any]], None] | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._log_level = log_level
        self._log_file = log_file
        self._shared_state_provider = shared_state_provider
        self._settings_provider = settings_provider
        self._lifecycle_settings_provider = lifecycle_settings_provider
        self._process_factory = process_factory
        self._event_factory = event_factory
        self._pid_recorder = pid_recorder
        self._pid_recorder_clear = pid_recorder_clear
        self._orphan_daemon_cleaner = orphan_daemon_cleaner
        self._supervisor_settings_provider = supervisor_settings_provider
        #: 监督者的构造口（测试注入假实现 ⇒ 守卫不许起真线程、不许碰真锁）。
        self._supervisor_factory = supervisor_factory
        #: 监督者状态的发布口（第 64 批 ⇒ 写共享内存 ⇒ /status 带出 ⇒ 前端可见）。
        self._supervisor_state_publisher = supervisor_state_publisher
        self._supervisor: DaemonSupervisor | None = None
        self.process = None
        self.stop_event = None

    def status(self) -> DaemonStatus:
        running = self.process is not None and self.process.is_alive()
        return DaemonStatus(
            running=running,
            pid=self.process.pid if self.process else None,
        )

    def _ensure_supervisor(self) -> DaemonSupervisor:
        """拿到（并按需创建）监督者。"""
        if self._supervisor is None:
            if self._supervisor_factory is not None:
                self._supervisor = self._supervisor_factory(self)
            else:
                self._supervisor = DaemonSupervisor(
                    self,
                    self._supervisor_settings_provider(),
                    self._logger,
                    state_publisher=self._supervisor_state_publisher,
                )
        return self._supervisor

    def supervisor_state(self) -> Dict[str, Any]:
        """监督者状态（给 /health 用；守护进程被外部杀掉时这里能看出来）。"""
        if self._supervisor is None:
            return {
                "enabled": bool(self._supervisor_settings_provider().enabled),
                "running": False,
                "restart_attempts": 0,
                "last_restart_at": None,
                "last_error": "",
            }
        return self._supervisor.state()

    def cleanup_orphans(self) -> None:
        cleanup_orphan_python_processes(self._logger)

    def cleanup_orphan_daemon(self) -> int:
        """杀掉上一个实例留下的守护进程（第 18 批 · G）。"""
        return cleanup_orphan_daemon(self._logger)

    def start(self) -> None:
        if self.process is not None and self.process.is_alive():
            self._logger.warning("守护进程已在运行，跳过重复启动")
            return

        # 🔒 先清掉上一个实例留下的守护进程（第 18 批 · G）：
        #   不清 ⇒ 旧守护进程活下来 ⇒ 两个守护进程同时跑同一本书。
        # ⚠ 放在 DISABLE_AUTO_DAEMON 判断**之前**：孤儿是「上一个实例」留下的，
        #   与「本实例要不要自动起守护进程」无关。
        self._orphan_daemon_cleaner()

        if self._settings_provider().disable_auto_daemon:
            self._logger.info("守护进程自动启动已禁用（DISABLE_AUTO_DAEMON=1）")
            return

        from application.engine.services.streaming_bus import init_streaming_bus

        stream_queue = init_streaming_bus()

        from application.engine.services.ai_stream_bus import init_ai_stream_bus

        ai_queue = init_ai_stream_bus()

        shared_state = self._shared_state_provider()

        from application.engine.services.shared_state_repository import (
            init_shared_state_repository,
        )

        shared_state_repo = init_shared_state_repository(shared_state)
        self._logger.info("共享状态仓库已初始化")

        from application.engine.services.state_bootstrap import bootstrap_state

        bootstrap_stats = bootstrap_state()
        self._logger.info("状态已从 DB 加载到共享内存: %s", bootstrap_stats)

        from application.engine.services.query_service import init_query_service

        init_query_service(shared_state_repo)
        self._logger.info("查询服务已初始化")

        from application.engine.services.persistence_queue import (
            get_persistence_queue,
            initialize_persistence_queue,
            register_persistence_handlers,
        )

        persistence_queue = initialize_persistence_queue()
        register_persistence_handlers()

        queue = get_persistence_queue()
        if not queue.is_consumer_running():
            queue.start_consumer()
            self._logger.info("持久化消费者线程已启动（单一写入者模式）")
        else:
            self._logger.debug("持久化消费者已在启动早期就绪（守护进程阶段不重复启动）")

        self.stop_event = self._event_factory()
        self.process = self._process_factory(
            target=run_autopilot_daemon_process,
            args=(
                self.stop_event,
                self._log_level,
                self._log_file,
                stream_queue,
                ai_queue,
                shared_state,
                persistence_queue,
            ),
            name="AutopilotDaemon",
            daemon=True,
        )
        self.process.start()
        # 守护子进程命令行不含任何可识别标识，只能靠 PID 记录在下次启动时清理。
        self._pid_recorder(self.process.pid)
        self._logger.info("守护进程已创建并启动（独立进程模式，流式队列 + AI 事件队列 + 共享状态 + 持久化队列已传递）")
        # 🔒 监督者：守护进程被外部杀掉时自动拉起（第 63 批）。
        # ⚠ 放在**守护进程真的起来之后** —— `DISABLE_AUTO_DAEMON=1` 时不起守护进程，
        #   也就没有东西要监督。
        self._ensure_supervisor().start()

    def stop(self) -> None:
        # 🔒 先停监督者 —— 否则它会在关机流程里把守护进程复活（第 63 批）。
        if self._supervisor is not None:
            self._supervisor.stop()
            self._supervisor = None
        daemon_pid = self.process.pid if self.process else None

        try:
            from application.engine.services.persistence_queue import shutdown_persistence_queue_if_initialized

            shutdown_persistence_queue_if_initialized()
        except Exception as exc:
            self._logger.debug("停止持久化消费者失败（可忽略）: %s", exc)

        try:
            from application.engine.services.streaming_bus import streaming_bus

            streaming_bus.publish_stop_signal("__all__")
        except Exception as exc:
            self._logger.debug("发布全局停止信号失败（可忽略）: %s", exc)

        if self.stop_event:
            self._logger.info("正在停止守护进程...")
            self.stop_event.set()

        if self.process and self.process.is_alive():
            lifecycle_settings = self._lifecycle_settings_provider()
            self.process.join(timeout=lifecycle_settings.graceful_join_timeout_seconds)
            if self.process.is_alive():
                self._logger.warning("守护进程未在超时时间内停止，强制终止")
                self.process.terminate()
                self.process.join(timeout=lifecycle_settings.terminate_join_timeout_seconds)
                if self.process.is_alive():
                    self._logger.warning("守护进程仍未停止，使用 SIGKILL")
                    try:
                        os.kill(self.process.pid, signal.SIGKILL)
                        self.process.join(timeout=lifecycle_settings.sigkill_join_timeout_seconds)
                    except Exception as exc:
                        self._logger.error("强制终止守护进程失败: %s", exc)
            else:
                self._logger.info("守护进程已成功停止")

        if os.name == "nt" and daemon_pid:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(daemon_pid)],
                    capture_output=True,
                    timeout=self._lifecycle_settings_provider().windows_taskkill_timeout_seconds,
                    creationflags=NO_WINDOW,
                )
                self._logger.info("Windows: 已通过 taskkill 终止守护进程 PID=%s", daemon_pid)
            except Exception as exc:
                self._logger.debug("taskkill 终止守护进程失败（可能已退出）: %s", exc)

        self.process = None
        self.stop_event = None
        self._pid_recorder_clear()

        if os.name == "nt":
            self.cleanup_orphans()

    def restart(self) -> None:
        self.stop()
        self.start()
        self._logger.info("守护进程已因配置变更重启")
