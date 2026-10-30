# -*- coding: utf-8 -*-
"""第 63 批：测试期间**不许**扫真进程（跑门禁不许杀掉开发者正在跑的后端）。

事故链（2026-10-30 实测定位）
-----------------------------
1. 单测里 ``TestClient(app).get("/health")`` **会跑 app 的 lifespan**
   ⇒ ``BackendLifecycle.startup()`` ⇒ ``cleanup_orphans()``
   ⇒ **真的** ``cleanup_orphan_python_processes()`` ⇒ 扫**真进程表**
   ⇒ 按命令行匹配把**正在运行的** API（``interfaces.main``）与守护子进程
   （PID 记录）**一起 taskkill**；
2. ⇒ **跑一次门禁，正在跑的后端就没了** —— 而且**没有任何日志**
   （taskkill /F 不留输出，Windows 事件日志也没有崩溃记录）。

⇒ 根治：加 ``PLOTPILOT_SKIP_ORPHAN_CLEANUP`` 开关
（``interfaces/daemon_manager.py``），测试套件全局打开它
（``tests/conftest.py``）⇒ **整个测试套件永远不会杀真进程**。

本文件的守卫：
- 开关**真的**在测试进程里开着（不是装饰品）；
- 开着时两个清扫入口都**不碰真进程**（行为级：枚举函数一旦被调用就炸）；
- 关掉时清扫**照常工作**（反向守卫：证明开关不是把功能焊死了）。
"""
from __future__ import annotations

import os

import pytest

from interfaces import daemon_manager
from interfaces.daemon_manager import (
    SKIP_ORPHAN_CLEANUP_ENV,
    cleanup_orphan_daemon,
    cleanup_orphan_python_processes,
)

ENV = SKIP_ORPHAN_CLEANUP_ENV


def test_the_test_suite_disables_the_orphan_cleanup():
    """测试套件必须开着这个开关（conftest 负责）。

    ⚠ 这是「跑门禁不许杀真进程」的**前提** —— 前提没了，整条保证就没了。
    """
    assert os.environ.get(ENV) == "1", (
        f"{ENV} 没被打开 ⇒ 测试期间的清扫会杀掉正在运行的后端"
    )


def test_orphan_cleanup_does_not_touch_real_processes_while_the_switch_is_on(monkeypatch):
    """开关打开 ⇒ 清扫**一个进程都不许碰**（枚举函数一次都不能被调用）。

    ⚠ 探测方式必须是「**记录调用**」，不能是「让枚举函数抛异常」——
    生产代码把枚举异常**吞掉**了（枚举失败 ⇒ 跳过清理 ⇒ 记一条 warning）
    ⇒ 抛异常式的探针会被吞掉 ⇒ **守卫假绿**（变异验证第 9/10 条抓到了这个）。
    """
    monkeypatch.setenv(ENV, "1")

    calls: list[object] = []

    def _record(settings, log):
        calls.append(settings)
        return []

    monkeypatch.setattr(daemon_manager, "_list_python_processes", _record)

    cleanup_orphan_python_processes()

    assert calls == [], "开关打开时清扫仍然枚举了真进程！"


def test_orphan_daemon_cleanup_does_not_touch_real_processes_while_the_switch_is_on(monkeypatch):
    """开关打开 ⇒ 孤儿守护进程清理也不许碰真进程（同上：记录调用，不靠抛异常）。"""
    monkeypatch.setenv(ENV, "1")

    calls: list[object] = []

    def _record(settings, log):
        calls.append(settings)
        return []

    monkeypatch.setattr(daemon_manager, "_list_python_processes", _record)

    assert cleanup_orphan_daemon() == 0
    assert calls == [], "开关打开时孤儿清理仍然枚举了真进程！"


def test_the_switch_is_not_welding_the_feature_shut(monkeypatch):
    """反向守卫：开关**关掉**时，清扫照常工作（不是把功能焊死了）。"""
    monkeypatch.delenv(ENV, raising=False)

    seen: list[object] = []

    def _fake_rows(settings, log):
        seen.append(settings)
        return []

    monkeypatch.setattr(daemon_manager, "_list_python_processes", _fake_rows)

    cleanup_orphan_python_processes()

    assert seen, "开关关掉时清扫必须真的去枚举进程"
