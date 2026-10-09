"""守护进程主循环 — Phase 5 从 AutopilotDaemon 收拢到 engine/runtime"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class DaemonLoopHost(Protocol):
    """守护进程主循环所需的最小 host 接口"""

    poll_interval: int
    circuit_breaker: Any

    def _write_daemon_heartbeat(self) -> None: ...
    def _get_active_novels(self) -> list: ...
    def _cleanup_stale_stop_signals(self, active_novels: list) -> None: ...
    async def _process_novel(self, novel: Any) -> None: ...


def _register_stop_signals(stop: dict) -> list:
    """注册 SIGINT/SIGTERM/SIGBREAK 优雅停机；返回成功注册的信号名。

    旧行为是 while True 无信号处理——进程只能被硬杀，在途 LLM 生成与
    后台 asyncio 任务全部悬空。现在收到信号只置标志：当前小说处理
    完成后退出循环（不打断在途生成）。
    """
    registered: list[str] = []

    def _request_stop(signum, _frame):
        if not stop["flag"]:
            logger.info("收到停止信号 %s，将在当前小说处理完成后退出", signum)
        stop["flag"] = True

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _request_stop)
            registered.append(name)
        except (ValueError, OSError):
            pass  # 非主线程或平台不支持（如 Windows 无 SIGTERM）
    return registered


def _interruptible_sleep(loop: asyncio.AbstractEventLoop, seconds: float, stop: dict) -> None:
    """事件循环保持运转的睡眠。

    与 time.sleep 的区别：
    1. 睡眠期间事件循环在跑——后台 asyncio 任务（宏观诊断等）得以推进，
       不再依赖「恰好有别的小说在被处理」才被调度；
    2. 每秒醒来检查停止信号，收到信号立即返回。
    """
    deadline = time.time() + max(0.0, seconds)
    while not stop["flag"]:
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        try:
            loop.run_until_complete(asyncio.sleep(min(1.0, remaining)))
        except Exception:
            return


def _novel_concurrency() -> int:
    """每轮并发处理的上限（默认 1 保持既有串行行为）。

    多本小说的 delegate 各自持有独立 novel 对象、写路径走单写者队列，
    并发安全性成立；默认关闭是因为 LLM 并发成本与速率限制由用户权衡——
    设 PLOTPILOT_DAEMON_NOVEL_CONCURRENCY=2..4 开启。
    """
    import os as _os

    raw = (_os.environ.get("PLOTPILOT_DAEMON_NOVEL_CONCURRENCY") or "").strip()
    if not raw:
        return 1
    try:
        return max(1, min(8, int(raw)))
    except ValueError:
        return 1


def _process_novels_concurrently(loop: asyncio.AbstractEventLoop, host, novels: list, stop: dict) -> None:
    """按并发上限处理本轮活跃小说；收到停止信号尽快返回。"""
    limit = _novel_concurrency()
    if limit <= 1 or len(novels) <= 1:
        for novel in novels:
            if stop["flag"]:
                return
            loop.run_until_complete(host._process_novel(novel))
        return

    semaphore = asyncio.Semaphore(limit)

    async def _one(novel):
        async with semaphore:
            if stop["flag"]:
                return
            await host._process_novel(novel)

    tasks = [asyncio.ensure_future(_one(n), loop=loop) for n in novels]
    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))


def run_daemon_loop(host: DaemonLoopHost, *, banner: str | None = None) -> None:
    """守护进程主循环（事务最小化原则）

    AutopilotDaemon、StoryPipelineRunner、EngineDaemon 共用此循环。
    """
    if banner:
        logger.info("=" * 80)
        logger.info(banner)
        logger.info("=" * 80)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    stop: dict = {"flag": False}
    registered = _register_stop_signals(stop)
    if registered:
        logger.info("守护进程已注册停止信号：%s（优雅停机：完成当前小说后退出）", ", ".join(registered))

    loop_count = 0
    try:
        while not stop["flag"]:
            loop_count += 1
            loop_start = time.time()

            host._write_daemon_heartbeat()

            if host.circuit_breaker and host.circuit_breaker.is_open():
                wait = host.circuit_breaker.wait_seconds()
                logger.warning("熔断器打开，暂停 %.0fs", wait)
                _interruptible_sleep(loop, min(wait, host.poll_interval), stop)
                continue

            try:
                try:
                    from application.engine.services.streaming_bus import streaming_bus

                    streaming_bus.consume_stop_signals()
                except Exception:
                    pass

                active_novels = host._get_active_novels()

                if active_novels:
                    host._cleanup_stale_stop_signals(active_novels)

                if loop_count % 10 == 1:
                    logger.info("Loop #%s: 发现 %s 本活跃小说", loop_count, len(active_novels))

                if active_novels:
                    # HALF_OPEN 试探轮：只处理一本，闭合后下一轮恢复全量——
                    # 旧实现对所有小说放行，API 刚恢复就被打爆再次熔断震荡
                    if host.circuit_breaker and host.circuit_breaker.state == "half_open":
                        active_novels = active_novels[:1]
                        logger.info("熔断器 HALF_OPEN 试探轮：仅处理第一本活跃小说")
                    _process_novels_concurrently(loop, host, active_novels, stop)
                    host._write_daemon_heartbeat()

            except Exception as e:
                logger.error("Daemon 顶层异常: %s", e, exc_info=True)

            loop_elapsed = time.time() - loop_start
            if loop_elapsed > host.poll_interval * 2:
                logger.warning("Loop #%s 耗时过长: %.2fs", loop_count, loop_elapsed)

            _interruptible_sleep(loop, host.poll_interval, stop)
    finally:
        logger.info("守护进程主循环已退出（共 %s 轮）", loop_count)
        try:
            loop.close()
        except Exception:
            pass
