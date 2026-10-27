"""G：守护进程**单实例**（第 18 批）。

背景（生产实测，第 17 批）
------------------------
重启后端 ⇒ **旧守护进程活下来** ⇒ 两个守护进程同时跑同一本书
⇒ 同一章被**重复审计两次**（一次旧代码 ``risk_flags=0``、一次新代码 ``risk_flags=13``），
章后管线也跑了两遍。

根因：守护子进程的 PID 记录（``data/daemon.pid``）是**单槽**的
⇒ 别的 API 实例（Tauri 桌面端 / 第二个 dev server）启停时会**覆盖**或**清掉**它
⇒ 下一次启动期的「残留清理」读不到真正还活着的守护进程（实测报「未发现残留进程」）。

⚠ 为什么不能改成「按命令行找出所有 multiprocessing 子进程并杀掉」
   ——本仓库的 API 进程**自己**也会 spawn 一个 ``multiprocessing.Manager()`` 服务进程，
   它的命令行与守护子进程**同签名**
   ⇒ 无差别杀会杀掉**自己的**共享状态管理器。

⇒ 修法：**OS 级独占文件锁**（``infrastructure/runtime/daemon_lock.py``）
  ① 守护子进程启动时必须拿到锁，拿不到 ⇒ **自己退出**（绝不并行跑同一本书）；
  ② 锁文件里记着持有者 pid ⇒ 启动期能**精确**杀掉上一个实例留下的孤儿；
  ③ 锁由 OS 在进程退出时释放 ⇒ 进程被强杀也不留「假锁」。

⚠ 本文件的用例**真造进程**（multiprocessing spawn）——
   「跨进程互斥」「孤儿被杀」这两条性质**只能在真进程下验**（桩/mock 验不出来）。
"""
from __future__ import annotations

import json
import logging
import multiprocessing
import os
import pathlib
import time

import pytest

from infrastructure.runtime.daemon_lock import (
    DaemonLock,
    is_lock_held,
    owner_file_path,
    read_holder_metadata,
    read_holder_pid,
)
from interfaces.daemon_manager import cleanup_orphan_daemon, run_autopilot_daemon_process

logger = logging.getLogger(__name__)


# ── 子进程里的可 pickle 目标（Windows spawn 要求顶层函数） ────────────────


def _child_try_lock(lock_path: str, result_path: str) -> None:
    """在**另一个进程**里尝试拿锁，把结果写进文件。"""
    attempt = DaemonLock(pathlib.Path(lock_path)).acquire()
    pathlib.Path(result_path).write_text(
        json.dumps({"acquired": bool(attempt.acquired), "holder_pid": attempt.holder_pid}),
        encoding="utf-8",
    )


def _child_run_daemon(lock_path: str, log_path: str, stop_path: str, data_dir: str) -> None:
    """在**另一个进程**里跑真正的守护进程入口（拿不到锁就该立刻退出）。

    ⚠ 数据目录指到临时目录 —— 万一门闩坏了、子进程继续往下跑
       （``build_daemon()``），也**不许**碰到生产库。
    """
    os.environ["PLOTPILOT_PROD_DATA_DIR"] = data_dir
    os.environ["PLOTPILOT_FORCE_PROD_DATA"] = "1"

    # 模拟「停机信号已置位」——我们要的是它在**进入轮询之前**就退出
    stop_event = multiprocessing.Event()
    stop_event.set()
    try:
        run_autopilot_daemon_process(
            stop_event,
            logging.INFO,
            log_path,
            lock_path=lock_path,
        )
    except Exception as exc:  # noqa: BLE001
        pathlib.Path(stop_path).write_text(f"raised: {exc}", encoding="utf-8")
    else:
        pathlib.Path(stop_path).write_text("returned", encoding="utf-8")


def _child_sleep(seconds: float) -> None:
    time.sleep(seconds)


# ── 工具 ────────────────────────────────────────────────────────────────


def _spawn(target, *args):
    proc = multiprocessing.Process(target=target, args=args, daemon=True)
    proc.start()
    return proc


def _wait_for(predicate, timeout: float = 20.0, interval: float = 0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def lock_path(tmp_path) -> pathlib.Path:
    """**临时**锁文件路径 —— 绝不能碰生产数据目录（那把锁正被真的守护进程持有）。"""
    return tmp_path / "autopilot-daemon.lock"


# ── 1) 锁的跨进程互斥 ────────────────────────────────────────────────────


def test_the_lock_is_exclusive_across_processes(lock_path, tmp_path):
    """本进程持有锁时，**另一个进程**必须拿不到。

    ⚠ 这是「两个守护进程不并行」的地基 —— 桩/mock 验不出来，必须真进程。
    """
    lock = DaemonLock(lock_path)
    assert lock.acquire().acquired, "本进程没能拿到锁（测试前提失败）"
    try:
        result_path = tmp_path / "child_result.json"
        child = _spawn(_child_try_lock, str(lock_path), str(result_path))
        child.join(timeout=30)
        assert not child.is_alive(), "子进程没退出（拿锁的调用卡住了）"
        assert result_path.is_file(), "子进程没写出结果"
        data = json.loads(result_path.read_text(encoding="utf-8"))
        assert data["acquired"] is False, (
            f"另一个进程竟然拿到了同一把锁 ⇒ 两个守护进程可能并行跑同一本书：{data}"
        )
    finally:
        lock.release()


def test_the_lock_file_records_the_holder_pid(lock_path):
    """锁文件里必须写着持有者 pid —— 启动期要靠它**精确**杀掉孤儿。

    ⚠ 这正是 PID 记录（``daemon.pid``，单槽、会被别的实例擦掉）缺的那一环。
    """
    lock = DaemonLock(lock_path)
    assert lock.acquire().acquired
    try:
        meta = read_holder_metadata(lock_path)
        assert meta, "锁文件里没有持有者元数据"
        assert int(meta.get("pid") or 0) == os.getpid(), (
            f"锁文件里的 pid 不是持有者自己：{meta}"
        )
        assert read_holder_pid(lock_path) == os.getpid()
    finally:
        lock.release()


def test_the_lock_is_released_when_the_holder_exits(lock_path, tmp_path):
    """持有者进程退出 ⇒ 锁**自动**释放（OS 级）⇒ 下一个守护进程能接管。

    ⚠ 用真进程验：进程一死，锁就松了（不需要谁去清）。
    """
    result_path = tmp_path / "child_result.json"
    child = _spawn(_child_try_lock, str(lock_path), str(result_path))
    child.join(timeout=30)
    assert not child.is_alive()
    data = json.loads(result_path.read_text(encoding="utf-8"))
    assert data["acquired"] is True, f"子进程第一次竟然没拿到锁：{data}"
    assert not is_lock_held(lock_path), "持有者退出后锁仍是占用状态"


# ── 2) 守护进程入口必须尊重这把锁 ────────────────────────────────────────


def test_a_daemon_child_refuses_to_start_when_the_lock_is_held(lock_path, tmp_path):
    """锁被占着 ⇒ 守护子进程必须**立刻退出**，不进入轮询。

    ⚠ 观测口径是**子进程自己的日志**（它 ``setup_logging`` 到传入的 log_file），
       不是「它退出了」（坏掉的门闩也可能让它很快崩掉 ⇒ 那样是假绿）。
    """
    log_path = tmp_path / "daemon.log"
    marker = tmp_path / "child_marker.txt"

    lock = DaemonLock(lock_path)
    assert lock.acquire().acquired, "本进程没能拿到锁（测试前提失败）"
    try:
        child = _spawn(
            _child_run_daemon,
            str(lock_path),
            str(log_path),
            str(marker),
            str(tmp_path / "child-data"),
        )
        child.join(timeout=45)
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)
            pytest.fail("守护子进程在锁被占用时没有退出（它进了轮询？）")
    finally:
        lock.release()

    text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    assert "单实例锁被占用" in text, (
        "守护子进程没有因为拿不到锁而退出 ⇒ 两个守护进程可能同时跑同一本书。\n"
        f"子进程日志：\n{text[-1500:]}"
    )


# ── 3) 启动期要能精确杀掉上一个实例留下的孤儿 ──────────────────────────


def _plant_holder(lock_path: pathlib.Path, pid: int) -> None:
    """把「锁的持有者 = pid」**植进去**（模拟上一个实例留下的记录）。

    ⚠ 植的是**持有者信息文件**，不是锁文件本身 ——
       Windows 的字节锁会让别的进程读不到锁文件（实测 PermissionError）
       ⇒ 持有者信息必须与锁文件分开。
    """
    owner_file_path(lock_path).write_text(
        json.dumps({"pid": int(pid), "owner_pid": 1, "started_at": time.time()}),
        encoding="utf-8",
    )


def test_startup_cleanup_kills_the_live_lock_holder(lock_path, tmp_path):
    """锁文件里记着一个**活着的** pid ⇒ 启动期必须把它杀掉。

    ⚠ 场景：上一个 API 实例被强杀 ⇒ 它的守护进程变孤儿（父没了，但它还活着）
       ⇒ 新实例启动时必须先把它杀掉，否则两个守护进程同时跑同一本书。
    """
    sleeper = _spawn(_child_sleep, 300.0)
    try:
        _plant_holder(lock_path, sleeper.pid)
        killed = cleanup_orphan_daemon(logger, lock_path=lock_path)
        assert killed == 1, f"没有杀掉孤儿守护进程（killed={killed}）"
        assert _wait_for(lambda: not sleeper.is_alive(), timeout=20), (
            "孤儿守护进程还活着 ⇒ 重启后端仍会有两个守护进程并行"
        )
    finally:
        if sleeper.is_alive():
            sleeper.terminate()
            sleeper.join(timeout=10)


def test_startup_cleanup_ignores_a_dead_holder(lock_path):
    """锁文件里的 pid 已经不在了（陈旧记录）⇒ 不许乱杀任何进程。"""
    _plant_holder(lock_path, 999999)
    killed = cleanup_orphan_daemon(logger, lock_path=lock_path)
    assert killed == 0, f"陈旧记录不该触发任何清理（killed={killed}）"


def test_startup_cleanup_does_nothing_without_a_lock_file(tmp_path):
    """没有锁文件 ⇒ 没有孤儿可清（返回 0，不抛异常）。"""
    assert cleanup_orphan_daemon(logger, lock_path=tmp_path / "missing.lock") == 0


# ── 4) 接线：start() 必须**先**清孤儿，再拉起新的守护进程 ────────────────


def test_the_manager_cleans_the_orphan_before_starting_a_new_daemon():
    """``start()`` 的第一步必须是清孤儿 ⇒ 顺序错了就会新旧并行。

    ⚠ 观测手段：让清理器抛哨兵异常 —— 若它在起进程**之前**调用，
       进程工厂根本不会被碰到（``calls == ["cleanup"]``）。
    """
    from interfaces.api.settings import BackendSettings
    from interfaces.daemon_manager import AutopilotDaemonManager

    calls: list[str] = []

    class _Sentinel(Exception):
        pass

    def _cleaner(*_args, **_kwargs):
        calls.append("cleanup")
        raise _Sentinel()

    manager = AutopilotDaemonManager(
        log_level=20,
        log_file="logs/test.log",
        shared_state_provider=lambda: {},
        settings_provider=lambda: BackendSettings(disable_auto_daemon=False),
        process_factory=lambda **kwargs: calls.append("process"),
        orphan_daemon_cleaner=_cleaner,
    )

    with pytest.raises(_Sentinel):
        manager.start()

    assert calls == ["cleanup"], (
        f"清理孤儿必须发生在起进程之前（否则新旧守护进程会短暂并行）：{calls}"
    )


def test_the_manager_cleans_the_orphan_even_when_the_auto_daemon_is_disabled():
    """``DISABLE_AUTO_DAEMON=1`` 也不许跳过清孤儿。

    ⚠ 孤儿是「**上一个实例**」留下的 ⇒ 与「本实例要不要自动起守护进程」无关。
    """
    from interfaces.api.settings import BackendSettings
    from interfaces.daemon_manager import AutopilotDaemonManager

    calls: list[str] = []
    manager = AutopilotDaemonManager(
        log_level=20,
        log_file="logs/test.log",
        shared_state_provider=lambda: {},
        settings_provider=lambda: BackendSettings(disable_auto_daemon=True),
        process_factory=lambda **kwargs: calls.append("process"),
        orphan_daemon_cleaner=lambda *a, **k: (calls.append("cleanup"), 0)[1],
    )

    manager.start()

    assert calls == ["cleanup"], f"禁用自动守护进程时也不该跳过清孤儿：{calls}"
