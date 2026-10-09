"""守护进程主循环：优雅停机信号 + 事件循环保持运转的睡眠。"""
import asyncio

import pytest

from engine.runtime.daemon_loop import _interruptible_sleep


def test_interruptible_sleep_returns_immediately_on_stop():
    loop = asyncio.new_event_loop()
    stop = {"flag": True}
    _interruptible_sleep(loop, 10.0, stop)  # 已请求停止：立即返回不睡
    loop.close()


def test_interruptible_sleep_runs_loop_during_wait():
    """睡眠期间事件循环应运转（后台任务可推进），到时正常返回。"""
    loop = asyncio.new_event_loop()
    ran = {"count": 0}

    async def _bg():
        while True:
            ran["count"] += 1
            await asyncio.sleep(0.01)

    bg = loop.create_task(_bg())
    stop = {"flag": False}
    _interruptible_sleep(loop, 0.2, stop)
    bg.cancel()
    try:
        loop.run_until_complete(bg)
    except (asyncio.CancelledError, Exception):
        pass
    loop.close()
    assert ran["count"] >= 5  # 睡眠期事件循环确实在跑


def test_stop_flag_breaks_sleep_early(monkeypatch):
    """睡眠中收到停止信号：1 秒内醒来返回。"""
    import time as _time

    loop = asyncio.new_event_loop()
    stop = {"flag": False}
    started = _time.time()

    # 后台协程在 _interruptible_sleep 内部的 run_until_complete 中推进并置位
    async def _set_flag():
        await asyncio.sleep(0.05)
        stop["flag"] = True

    # 先起置位协程（run_until_complete 在 _interruptible_sleep 内部推进它）
    task = loop.create_task(_set_flag())
    _interruptible_sleep(loop, 30.0, stop)
    elapsed = _time.time() - started
    try:
        loop.run_until_complete(task)
    except Exception:
        pass
    loop.close()
    assert elapsed < 5.0  # 没有 30s 睡满


@pytest.mark.asyncio
async def test_dead_stream_is_terminated_by_idle_timeout():
    """连接挂死（永不产出 chunk）时，空闲超时必须真正终止流消费。

    旧实现的 `async for` 只在收到下一个 chunk 时检查停止标志——
    无 chunk 时 watcher 置位也没人消费，空闲/总超时形同虚设。
    """
    import asyncio
    from engine.runtime.daemon_host import DaemonHostMixin
    from domain.ai.value_objects.prompt import Prompt
    from domain.ai.services.llm_service import GenerationConfig

    class _DeadLLM:
        async def stream_generate(self, prompt, config):
            yield "第一段"
            await asyncio.sleep(999)  # 模拟连接彻底挂死
            yield "永远到不了"

    host = object.__new__(DaemonHostMixin)
    host.llm_service = _DeadLLM()
    prompt = Prompt(system="s", user="u")

    started = asyncio.get_event_loop().time()
    content = await host._stream_llm_with_stop_watch(
        prompt, GenerationConfig(), novel=None, idle_timeout=0.3, total_timeout=30.0
    )
    elapsed = asyncio.get_event_loop().time() - started

    assert content == "第一段"
    assert elapsed < 10.0  # 没有跟着流一起挂死（watcher 5s 粒度 + 消费侧 1s 响应）


@pytest.mark.asyncio
async def test_healthy_stream_consumed_fully():
    from engine.runtime.daemon_host import DaemonHostMixin
    from domain.ai.value_objects.prompt import Prompt
    from domain.ai.services.llm_service import GenerationConfig

    class _OkLLM:
        async def stream_generate(self, prompt, config):
            for part in ("春", "风", "又", "绿"):
                yield part

    host = object.__new__(DaemonHostMixin)
    host.llm_service = _OkLLM()

    content = await host._stream_llm_with_stop_watch(
        Prompt(system="s", user="u"), GenerationConfig(),
        novel=None, idle_timeout=5.0, total_timeout=30.0,
    )
    assert content == "春风又绿"
