"""停止信号链路故障可见性（登记表 I-01）。

缺陷背景：`BaseStoryPipeline._novel_stream_should_stop` 此前有两处静默吞异常
（第一处 `pass`、第二处 `return False`）。停止信号存储一旦故障，返回值与
「用户没点停止」完全无法区分 —— 故障被伪装成了正常。运维只能看到
「点停止没反应」，看不到链路已经坏了。

本组用例钉死三条红线：

1. 故障必须留痕（第一段 warning / 第二段 error）；
2. 故障必须有**可断言的非日志**标记（计数 + 错误摘要），不靠断言日志字符串；
3. 故障**不得**改变返回值方向（第二段异常仍返回 False）—— 反向守卫。

全部为行为测试：不读源码，不断言「某段源码字符串不存在」。
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from engine.pipeline.base import BaseStoryPipeline


class _Pipeline(BaseStoryPipeline):
    """最小可实例化管线（与 test_pipeline_streaming.py 同形）。"""


def _records(caplog, name: str, levelno: int):
    """只取指定 logger、 指定级别的记录，避免被别的 logger 噪声污染。"""
    return [r for r in caplog.records if r.name == name and r.levelno == levelno]


def _install_stop_signal(monkeypatch, script):
    """按脚本替换 ``is_novel_stopped``。

    script 每项是返回值或要抛出的异常；逐次生效，耗尽后重复最后一项 ——
    正好表达「第一次抛错、第二次正常」这类两段式场景。
    """
    state = {"i": 0}

    def _fake(_novel_id):
        idx = min(state["i"], len(script) - 1)
        state["i"] += 1
        item = script[idx]
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(
        "application.engine.services.novel_stop_signal.is_novel_stopped", _fake
    )


def _stub_bus(monkeypatch):
    """控制队列消费一律 no-op，避免依赖真实流式总线运行时。"""
    monkeypatch.setattr(
        "application.engine.services.streaming_bus.streaming_bus", MagicMock()
    )


def test_first_segment_failure_warns_and_still_honours_second_check(monkeypatch, caplog):
    """第一段抛错 → warning 留痕，且仍落到第二段并真正尊重停止信号。

    第一段会重试，所以它**不算**链路故障（否则瞬时抖动会刷满故障计数）。
    """
    _install_stop_signal(monkeypatch, [RuntimeError("signal store down"), True])
    _stub_bus(monkeypatch)
    pipeline = _Pipeline()

    with caplog.at_level(logging.WARNING, logger="engine.pipeline.base"):
        result = pipeline._novel_stream_should_stop("n1")

    assert result is True, "第二段仍能读到停止信号时必须真的停下"
    warns = _records(caplog, "engine.pipeline.base", logging.WARNING)
    assert warns, "第一段异常必须有 warning 留痕，不能静默 pass"
    assert "signal store down" in warns[0].getMessage()
    assert pipeline._stop_signal_failure_count == 0, (
        "第一段异常会落到第二段重试，不应记为链路故障"
    )


def test_second_segment_failure_logs_error_and_keeps_return_false(monkeypatch, caplog):
    """第二段抛错 → error 级留痕（真故障），返回值仍是 False。"""
    _install_stop_signal(monkeypatch, [False, RuntimeError("signal store down")])
    _stub_bus(monkeypatch)
    pipeline = _Pipeline()

    with caplog.at_level(logging.ERROR, logger="engine.pipeline.base"):
        result = pipeline._novel_stream_should_stop("n1")

    assert result is False
    errors = _records(caplog, "engine.pipeline.base", logging.ERROR)
    assert errors, "第二段异常是链路真故障，必须有 error 级留痕"
    assert "signal store down" in errors[0].getMessage()


def test_failure_marker_is_assertable_without_reading_logs(monkeypatch):
    """可观测标记：计数累加 + 错误摘要，全程不依赖日志文本。"""
    _install_stop_signal(monkeypatch, [False, RuntimeError("boom-1")])
    _stub_bus(monkeypatch)
    pipeline = _Pipeline()

    assert pipeline._stop_signal_failure_count == 0
    assert pipeline._stop_signal_last_error is None

    pipeline._novel_stream_should_stop("n1")
    assert pipeline._stop_signal_failure_count == 1
    assert "RuntimeError" in pipeline._stop_signal_last_error
    assert "boom-1" in pipeline._stop_signal_last_error

    pipeline._novel_stream_should_stop("n1")
    assert pipeline._stop_signal_failure_count == 2, "重复故障必须能累加，否则看不出持续故障"


def test_healthy_path_leaves_marker_untouched(monkeypatch, caplog):
    """健康路径不留痕、不计数 —— 否则标记没有区分度。"""
    _install_stop_signal(monkeypatch, [False, False])
    _stub_bus(monkeypatch)
    pipeline = _Pipeline()

    with caplog.at_level(logging.WARNING, logger="engine.pipeline.base"):
        assert pipeline._novel_stream_should_stop("n1") is False

    assert pipeline._stop_signal_failure_count == 0
    assert pipeline._stop_signal_last_error is None
    assert not _records(caplog, "engine.pipeline.base", logging.WARNING), (
        "链路正常时不得留下故障告警"
    )
    assert not _records(caplog, "engine.pipeline.base", logging.ERROR)


def test_second_segment_failure_must_not_be_turned_into_fail_safe(monkeypatch):
    """反向守卫 —— 本 Task 最重要的红线。

    停止信号链路故障时返回值必须**仍是 False**。若有人把 I-01 改成
    fail-safe ``return True``，停止信号存储一旦持续故障，每一章的流式
    生成立刻中止，整本书一个字都写不出来：把「用户偶尔点不到停止」
    升级成「产品完全不可用」。故障该被看见，不该被改写成新的行为。
    """
    _install_stop_signal(monkeypatch, [False, RuntimeError("boom")])
    _stub_bus(monkeypatch)
    pipeline = _Pipeline()

    result = pipeline._novel_stream_should_stop("n1")

    assert result is False, (
        "停止信号故障不得改成 fail-safe True —— 那是硬造行为分支，"
        "会让链路故障直接变成整本书写不出来"
    )
