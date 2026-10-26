"""E'：测试进程的日志**不许**写进生产的 ``logs/plotpilot.log``。

背景 —— 第 16 批生产跑章时踩到：
跑门禁期间，测试的**预期**错误输出（迁移失败之类）会混进同一个日志文件，
于是排查生产问题时，**把测试的日志当成了生产故障**（本轮就误判过一次，
以为生产在跑坏迁移文件）。

⚠ 第一版修法**只设了环境变量** ⇒ 守卫查的也是环境变量 ⇒ **空守卫**：
真实机制有**两条**（详见 ``tests/log_isolation.py``）：

1. ``interfaces/main.py`` **模块级**调 ``load_env()``（覆盖语义）
   ⇒ ``.env`` 里的 ``LOG_FILE=logs/plotpilot.log`` 把环境变量**改回去**；
2. 同一个模块**模块级**调 ``setup_logging()``
   ⇒ 根 logger 上多了一个指向生产日志的 ``RotatingFileHandler``
   ⇒ 之后所有日志都写进生产文件（哪怕环境变量已经改对）。

⇒ 本文件查**有效目标**：环境变量 **+** 根 logger 上所有 handler 的落盘位置，
并且**直接调用**隔离实现做自检（这样「隔离逻辑本身坏了」也会红）。
"""
from __future__ import annotations

import logging
import os
import pathlib

from tests.log_isolation import (
    PRODUCTION_LOGS_DIR,
    apply_test_log_isolation,
    is_production_log_path,
    production_log_handlers,
    isolated_log_file,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PRODUCTION_LOG = REPO_ROOT / "logs" / "plotpilot.log"


def test_log_file_env_points_outside_the_production_logs_dir():
    """``LOG_FILE`` 必须指向生产日志之外。"""
    log_file = os.environ.get("LOG_FILE", "")
    assert log_file, "测试进程没有设 LOG_FILE ⇒ 会落到生产默认路径"

    resolved = pathlib.Path(log_file).resolve()
    assert resolved != PRODUCTION_LOG.resolve(), (
        f"测试进程把日志指向了生产日志：{resolved}"
    )
    assert not is_production_log_path(resolved), (
        f"测试进程的日志落在生产 logs/ 目录里：{resolved}"
    )


def test_no_root_handler_writes_into_the_production_logs_dir():
    """根 logger 上**不许**有指向生产日志的 handler。

    ⚠ 这条才是「日志真的不会写进生产文件」的判据 ——
    只查环境变量会漏掉「模块级 ``setup_logging`` 挂上去的 handler」这条路。
    """
    offenders = production_log_handlers()
    assert not offenders, f"根 logger 上有 handler 正写向生产日志：{offenders}"


def test_isolation_removes_a_planted_production_handler():
    """**自检**：给根 logger 挂一个指向生产日志的 handler ⇒ 隔离必须把它摘掉。

    ⚠ 为什么要自检：否则「隔离逻辑本身失效」是**不可观测变异**（守卫照样绿）。
    ⇒ 这里用一个**假 handler**（只带 ``baseFilename``，不真的建文件）
    触发一次真实调用 ⇒ 摘不掉就红。
    """
    class _FakeProductionHandler(logging.Handler):
        baseFilename = str(PRODUCTION_LOG)

    handler = _FakeProductionHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        apply_test_log_isolation()
        remaining = [h for h in root.handlers if h is handler]
        assert not remaining, "隔离没摘掉指向生产日志的 handler ⇒ 生产日志会被写"
        assert not production_log_handlers(), "隔离之后仍有 handler 指向生产日志"
    finally:
        if handler in root.handlers:
            root.removeHandler(handler)


def test_load_env_cannot_clobber_the_log_file_env():
    """``load_env()`` 是**覆盖**语义 ⇒ 它不许把 ``LOG_FILE`` 改回 ``.env`` 里的值。

    ⚠ ``.env`` 里确实写着 ``LOG_FILE=logs/plotpilot.log``（生产默认路径）。
    ⇒ 隔离层把 ``load_env`` 包了一层：别的键照旧覆盖，``LOG_FILE`` 保留。
    """
    import load_env as load_env_module

    before = os.environ.get("LOG_FILE")
    snapshot = dict(os.environ)
    try:
        load_env_module.load_env()
        assert os.environ.get("LOG_FILE") == before, (
            "load_env() 把 LOG_FILE 改回了 .env 里的生产路径 ⇒ "
            "测试日志会落进生产文件"
        )
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


def test_setup_logging_cannot_attach_a_production_log_handler():
    """``setup_logging(log_file=<生产路径>)`` 不许在根 logger 上挂生产日志 handler。

    ⚠ 这是**最关键**的一条：``interfaces/main.py`` / ``scripts/start_daemon.py``
    都在**模块级**调 ``setup_logging`` ⇒ 任何一次 ``import`` 就会挂上生产日志 handler
    ⇒ 「用例开始前清理」来不及（挂上去那一刻日志就写进去了）。

    ⚠ 用一个**探针路径**（生产 logs/ 目录下的临时文件名）而不是真的生产日志文件
    ⇒ 即使守卫失效，也只是多一个探针文件（finally 里删掉），**不碰**真的生产日志。
    """
    import interfaces.api.middleware.logging_config as logging_config

    probe = PRODUCTION_LOGS_DIR / "_guard_probe.log"
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        logging_config.setup_logging(level=logging.INFO, log_file=str(probe))
        offenders = production_log_handlers()
        assert not offenders, f"setup_logging 仍然挂上了生产日志 handler：{offenders}"
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in before:
            root.addHandler(handler)
        if probe.exists():
            probe.unlink()


def test_isolation_forces_the_log_file_env_even_after_a_clobber():
    """**自检**：环境变量被掀翻（= 生产路径）后，隔离必须把它拉回临时目录。

    ⚠ 为什么要有这条：``.env`` 里就写着 ``LOG_FILE=logs/plotpilot.log``，
    而 ``load_env`` 是覆盖语义 ⇒ 这条路径**真的会被走到**。
    """
    before = os.environ.get("LOG_FILE")
    try:
        os.environ["LOG_FILE"] = str(PRODUCTION_LOG)
        apply_test_log_isolation()
        after = os.environ.get("LOG_FILE", "")
        assert after, "隔离把 LOG_FILE 弄没了"
        assert not is_production_log_path(after), (
            f"隔离没有把 LOG_FILE 拉回临时目录：{after}"
        )
    finally:
        if before is not None:
            os.environ["LOG_FILE"] = before


def test_isolated_log_file_is_in_the_temp_dir_not_the_repo():
    """测试日志文件本身在系统临时目录（不是仓库里）。"""
    resolved = isolated_log_file().resolve()
    assert not is_production_log_path(resolved)
    assert REPO_ROOT.resolve() not in resolved.parents, (
        f"测试日志文件落在仓库里：{resolved}"
    )
    assert PRODUCTION_LOGS_DIR != resolved.parent, (
        f"测试日志文件落在生产日志目录里：{resolved}"
    )
