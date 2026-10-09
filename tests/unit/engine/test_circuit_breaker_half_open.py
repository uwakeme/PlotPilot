"""A5: 熔断器 HALF_OPEN 单探测 + daemon 并发调度。"""
import asyncio
import time

from application.engine.services.circuit_breaker import BreakerState, CircuitBreaker


def _tripped(reset_timeout=0.05):
    cb = CircuitBreaker(failure_threshold=2, reset_timeout=reset_timeout, half_open_max_calls=1)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"
    return cb


def test_half_open_admits_only_one_inflight_probe():
    cb = _tripped()
    time.sleep(0.06)

    assert cb.is_open() is False      # 第一个探测放行（占名额）
    assert cb.is_open() is True       # 名额占满：其余调用方等待（旧实现这里是 False）
    assert cb.is_open() is True


def test_probe_success_closes_and_releases():
    cb = _tripped()
    time.sleep(0.06)
    assert cb.is_open() is False
    cb.record_success()               # 探测成功：闭合 + 释放名额
    assert cb.state == "closed"
    assert cb.is_open() is False      # 闭合后放行一切


def test_probe_failure_reopens():
    cb = _tripped()
    time.sleep(0.06)
    assert cb.is_open() is False
    cb.record_failure()
    assert cb.state == "open"


def test_stuck_probe_releases_after_timeout():
    cb = _tripped()
    time.sleep(0.06)
    assert cb.is_open() is False      # 占名额但调用方既不 success 也不 failure
    time.sleep(0.06)
    assert cb.is_open() is False      # 滞留超时：自动释放名额重试，不卡死 HALF_OPEN


def test_novel_concurrency_env(monkeypatch):
    from engine.runtime.daemon_loop import _novel_concurrency

    monkeypatch.delenv("PLOTPILOT_DAEMON_NOVEL_CONCURRENCY", raising=False)
    assert _novel_concurrency() == 1  # 默认串行（保持既有行为）
    monkeypatch.setenv("PLOTPILOT_DAEMON_NOVEL_CONCURRENCY", "3")
    assert _novel_concurrency() == 3
    monkeypatch.setenv("PLOTPILOT_DAEMON_NOVEL_CONCURRENCY", "99")
    assert _novel_concurrency() == 8  # 上限
    monkeypatch.setenv("PLOTPILOT_DAEMON_NOVEL_CONCURRENCY", "abc")
    assert _novel_concurrency() == 1


def test_process_novels_concurrently_limits_parallelism(monkeypatch):
    from engine.runtime.daemon_loop import _process_novels_concurrently

    monkeypatch.setenv("PLOTPILOT_DAEMON_NOVEL_CONCURRENCY", "2")
    loop = asyncio.new_event_loop()
    host = type("H", (), {"_process_novel": None})()
    active = {"now": 0, "peak": 0, "done": 0}

    async def _fake(novel):
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        await asyncio.sleep(0.05)
        active["now"] -= 1
        active["done"] += 1

    host._process_novel = _fake
    novels = [object() for _ in range(5)]
    _process_novels_concurrently(loop, host, novels, {"flag": False})
    loop.close()
    assert active["done"] == 5
    assert active["peak"] <= 2
