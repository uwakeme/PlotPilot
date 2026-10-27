"""守护进程的**单实例锁**（第 18 批 · G）。

问题（生产实测）
----------------
守护子进程的 PID 记录（``data/daemon.pid``）是**单槽**的 ——
任何另一个 API 实例（Tauri 桌面端 / 第二个 dev server / 测试实例）启停时
都会**覆盖**或**清掉**它 ⇒ 下一次启动期的「残留清理」读不到真正还活着的守护进程
⇒ **两个守护进程同时跑同一本书**。

实测后果：同一章被**重复审计两次**（一次旧代码 ⇒ ``risk_flags=0``，
一次新代码 ⇒ ``risk_flags=13``），章后管线也跑了两遍。
而启动期的清理明明「在干活」（同一份日志里能看到它靠 PID 记录杀过一个守护子进程），
只是那次的记录**已经被别的实例擦掉了** ⇒ 报「未发现残留进程」。

⚠ 为什么**不能**改成「按命令行找出所有 multiprocessing 子进程并杀掉」
--------------------------------------------------------------------
本仓库的 API 进程**自己**也会 spawn 一个 ``multiprocessing.Manager()`` 服务进程，
它的命令行与守护子进程**同签名**
（``python -c "from multiprocessing.spawn import spawn_main…" --multiprocessing-fork``）
⇒ 无差别杀会杀掉**自己的**共享状态管理器 ⇒ 反而制造更大的事故。

⇒ 改用**文件独占锁**做单实例：

* 只有**拿到锁**的守护进程才允许跑（拿不到 ⇒ 自己退出）；
* 锁由**操作系统**在进程退出时释放 ⇒ 进程被强杀也不会留下「假锁」；
* 锁文件里写着持有者的 pid ⇒ 下一次启动期能**精确**知道该杀谁（而不是猜命令行）。

跨平台：Windows 用 ``msvcrt.locking``（锁 1 个字节），POSIX 用 ``fcntl.flock``。
两者都是「进程退出即释放」的文件锁。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

#: 锁文件名（放在数据目录 ⇒ 沙箱与生产各有一把锁，互不干扰）
LOCK_FILE_NAME = "autopilot-daemon.lock"

#: 持有者信息文件名（**与锁文件分开**）
#:
#: ⚠ 为什么必须分开：Windows 的字节区间锁（``msvcrt.locking``）会让**别的进程
#:   读不到被锁的那一段**（实测：另一个进程 ``read_bytes()`` 直接
#:   ``PermissionError``）⇒ 元数据若写在锁文件里，**持有者信息就读不出来**
#:   ⇒ 启动期反而找不到该杀谁。
OWNER_FILE_NAME = "autopilot-daemon.owner.json"

#: 锁文件里写的元数据（JSON）
META_KEY_PID = "pid"
META_KEY_OWNER_PID = "owner_pid"
META_KEY_STARTED_AT = "started_at"


def default_lock_path() -> Path:
    """默认锁文件路径（数据目录下）。"""
    from application.paths import resolve_runtime_data_path

    return resolve_runtime_data_path(LOCK_FILE_NAME)


def owner_file_path(lock_path: Optional[Path] = None) -> Path:
    """持有者信息文件路径（与锁文件同目录、不同名）。"""
    target = Path(lock_path) if lock_path is not None else default_lock_path()
    return target.with_name(OWNER_FILE_NAME)


@dataclass
class LockAttempt:
    """一次加锁尝试的结果。"""

    acquired: bool
    path: Path
    holder_pid: Optional[int] = None
    detail: str = ""


class DaemonLock:
    """守护进程的单实例锁（持有者 = 守护进程本身）。

    用法::

        lock = DaemonLock()
        if not lock.acquire().acquired:
            return          # 已经有别的守护进程在跑
        try:
            ...             # 跑主循环
        finally:
            lock.release()
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else default_lock_path()
        self.owner_path = owner_file_path(self.path)
        self._handle: Optional[Any] = None

    # ── 加锁 ────────────────────────────────────────────────────────────

    def acquire(self) -> LockAttempt:
        """尝试独占加锁；成功时把持有者信息写进**持有者信息文件**。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 —— 拿不到锁文件就当没锁住
            return LockAttempt(False, self.path, detail=f"无法创建锁文件目录: {exc}")

        try:
            handle = open(self.path, "a+b")
        except Exception as exc:  # noqa: BLE001
            return LockAttempt(False, self.path, detail=f"无法打开锁文件: {exc}")

        try:
            _lock_first_byte(handle)
        except OSError as exc:
            handle.close()
            return LockAttempt(
                False,
                self.path,
                holder_pid=read_holder_pid(self.path),
                detail=f"锁已被占用: {exc}",
            )

        self._handle = handle
        self._write_metadata()
        return LockAttempt(True, self.path, holder_pid=os.getpid())

    def release(self) -> None:
        """释放锁（并清掉持有者信息，让下一个持有者能写自己的）。"""
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            try:
                self.owner_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            try:
                _unlock_first_byte(handle)
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass

    # ── 元数据 ──────────────────────────────────────────────────────────

    def _write_metadata(self) -> None:
        payload = json.dumps(
            {
                META_KEY_PID: os.getpid(),
                META_KEY_OWNER_PID: os.getppid(),
                META_KEY_STARTED_AT: time.time(),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            self.owner_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.owner_path, "wb") as fh:
                fh.write(payload)
                fh.flush()
        except Exception:  # noqa: BLE001 —— 元数据写不进去不影响锁本身
            pass

    def __enter__(self) -> "DaemonLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.release()


# ── 读侧（不需要持锁） ────────────────────────────────────────────────────


def read_holder_metadata(path: Optional[Path] = None) -> dict:
    """读**持有者信息文件**（不碰锁文件 ⇒ 不会撞上 Windows 的字节锁）。"""
    target = owner_file_path(path)
    try:
        raw = target.read_bytes()
    except Exception:  # noqa: BLE001
        return {}
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def read_holder_pid(path: Optional[Path] = None) -> Optional[int]:
    """锁文件里记录的持有者 pid（没有 ⇒ None）。"""
    meta = read_holder_metadata(path)
    try:
        pid = int(meta.get(META_KEY_PID) or 0)
    except Exception:  # noqa: BLE001
        return None
    return pid if pid > 0 else None


def is_lock_held(path: Optional[Path] = None) -> bool:
    """锁当前是否被占用（**试加锁**判断；不改变持有者）。"""
    target = Path(path) if path is not None else default_lock_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = open(target, "a+b")
    except Exception:  # noqa: BLE001
        # 连文件都开不了 ⇒ 保守认为有人占着
        return True
    try:
        _lock_first_byte(handle)
    except OSError:
        return True
    else:
        try:
            _unlock_first_byte(handle)
        finally:
            handle.close()
        return False
    finally:
        try:
            handle.close()
        except Exception:  # noqa: BLE001
            pass


# ── 平台相关 ──────────────────────────────────────────────────────────────


def _lock_first_byte(handle: Any) -> None:
    """在文件句柄上独占锁住第 1 个字节（失败抛 OSError）。"""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        # 至少要有 1 个字节可锁
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_first_byte(handle: Any) -> None:
    """解除第 1 个字节的锁。"""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
