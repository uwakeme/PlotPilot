"""测试进程的**日志隔离**（第 16 批 · E'）。

问题
----
第 16 批在生产库跑章时踩到：跑门禁期间，测试的**预期**错误输出（迁移失败之类）
混进了生产的 ``logs/plotpilot.log`` ⇒ 排查生产问题时**把测试的日志当成了生产故障**
（本轮误判过一次，以为生产在跑坏迁移）。

⚠ 真正的机制有**两条**，只堵一条等于没堵：

1. **环境变量被覆盖**
   ``interfaces/main.py`` 在**模块级**调 ``load_env()``，而 ``load_env.py`` 是
   **覆盖语义**（``os.environ[k] = v``）⇒ ``.env`` 里的
   ``LOG_FILE=logs/plotpilot.log`` 会把进程环境变量**改回去**。
   （pytest 会导入 ``tests/unit/interfaces/test_runtime.py`` ⇒ 该模块
   ``import interfaces.main`` ⇒ 收集期就触发了。）

2. **根 logger 上多了一个指向生产日志的 handler**
   同一个模块在**模块级**调 ``setup_logging(log_file=settings.log_file)``
   ⇒ 根 logger 上多了一个 ``RotatingFileHandler``，目标就是生产日志
   ⇒ 之后**所有**日志都写进生产文件（哪怕环境变量已经改对）。

⇒ 所以隔离必须**同时**做：① 强制环境变量指向临时文件；② 摘掉指向生产 ``logs/`` 的 handler。
⇒ 并且要能扛住「收集期被覆盖」⇒ 在 ``pytest_configure`` / 收集结束 / 每个用例前各修一次。

⚠ 这个模块被守卫**直接调用**（不是被复制一遍）⇒ 改它，守卫就会红。
"""
from __future__ import annotations

import logging
import os
import pathlib
import tempfile
from typing import List

#: 仓库根目录
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: **生产**日志目录（测试进程绝不许往这儿写）
PRODUCTION_LOGS_DIR = (REPO_ROOT / "logs").resolve()


def isolated_log_file() -> pathlib.Path:
    """测试进程自己的日志文件（系统临时目录）。"""
    return pathlib.Path(tempfile.gettempdir()) / f"plotpilot-tests-{os.getpid()}.log"


def is_production_log_path(path: os.PathLike | str) -> bool:
    """这个路径是不是**生产**日志（日志目录之内，或正是那个目录）。

    ⚠ 相对路径按**仓库根**解析（不是 CWD）—— 测试里 CWD 恰好是仓库根，
    但「相对路径的含义」不该随 CWD 漂。
    """
    candidate = pathlib.Path(path)
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return False
    return resolved == PRODUCTION_LOGS_DIR or PRODUCTION_LOGS_DIR in resolved.parents


def production_log_handlers() -> List[str]:
    """根 logger 上**正指向生产日志**的 handler（文件名列表）。"""
    offenders: List[str] = []
    for handler in logging.getLogger().handlers:
        base = getattr(handler, "baseFilename", None)
        if base and is_production_log_path(base):
            offenders.append(str(base))
    return offenders


def apply_test_log_isolation() -> None:
    """把测试进程的日志**拉回**临时目录。

    做两件事：
    ① ``LOG_FILE`` 指向临时文件（**强制**，不是 setdefault —— 因为会被 ``.env`` 覆盖）；
    ② 摘掉根 logger 上指向生产 ``logs/`` 的 handler（模块级 ``setup_logging`` 挂上去的）。
    """
    os.environ["LOG_FILE"] = str(isolated_log_file())

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        base = getattr(handler, "baseFilename", None)
        if base and is_production_log_path(base):
            root_logger.removeHandler(handler)


def install_setup_logging_guard() -> None:
    """把 ``setup_logging`` 包一层：**指向生产日志**的落盘路径一律换成测试日志。

    ⚠ 为什么必须包这一层：``interfaces/main.py`` 与 ``scripts/start_daemon.py``
    都在**模块级**调 ``setup_logging(log_file=...)`` ⇒ 任何一次 ``import``
    就会给根 logger 挂上一个指向生产日志的 ``RotatingFileHandler``。
    ⇒ 只在「用例开始前」清理是不够的（挂上去的那一刻日志就已经写进去了）。

    ⚠ 只重定向**生产路径**：测试自己用临时目录的 ``setup_logging``（例如
    ``test_logging_config.py``）必须原样保留，否则那条用例就测不到东西了。
    """
    try:
        import interfaces.api.middleware.logging_config as logging_config
    except Exception:  # pragma: no cover - 依赖还没就绪时忽略
        return

    if getattr(logging_config, "_plotpilot_test_isolated", False):
        return

    original_setup_logging = logging_config.setup_logging

    def _setup_logging_isolated(*args, **kwargs):
        log_file = kwargs.get("log_file")
        if log_file is None and len(args) >= 3:
            log_file = args[2]
        if log_file and is_production_log_path(log_file):
            replacement = str(isolated_log_file())
            if len(args) >= 3:
                args = list(args)
                args[2] = replacement
            else:
                kwargs["log_file"] = replacement
        return original_setup_logging(*args, **kwargs)

    logging_config.setup_logging = _setup_logging_isolated
    logging_config._plotpilot_test_isolated = True
