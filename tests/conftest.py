"""保证从仓库内任意 cwd 运行 pytest 时能找到项目包（pythonpath 含仓库根）。"""
import sys
from pathlib import Path

# Add paths immediately at module import time
_root = Path(__file__).resolve().parent.parent
_parent = _root.parent

# Add project root FIRST (for infrastructure, domain, etc.) - this is most important
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

# Add parent for project root - but AFTER project root
if str(_parent) not in sys.path:
    sys.path.append(str(_parent))  # Use append instead of insert to keep it lower priority


def pytest_configure(config):
    """Configure pytest - ensure paths are set before test collection"""
    import os

    os.environ.setdefault("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "1")

    # 测试日志**不许**写进生产的 ``logs/plotpilot.log``。
    #
    # ⚠ 第 16 批实测踩过：跑门禁时，测试的**预期**错误日志（迁移失败之类）
    #   混进了同一个日志文件 ⇒ 排查生产问题时被它误导过一次
    #   （把测试的输出当成了生产在跑坏迁移）。
    #
    # ⚠ 这件事有**三条**通道，只堵一条等于没堵（详见 tests/log_isolation.py）：
    #   ① ``interfaces/main.py`` 模块级调 ``load_env()``（**覆盖**语义）
    #      ⇒ ``.env`` 里的 ``LOG_FILE=logs/plotpilot.log`` 会把环境变量改回去；
    #   ② 同一个模块模块级调 ``setup_logging()`` ⇒ 根 logger 上多了一个
    #      指向生产日志的 handler；
    #   ③ 「用例开始前清理」来不及 —— ``interfaces.main`` 是在**用例体内**
    #      被 import 的，挂上去那一刻日志就已经写进生产文件了。
    _install_log_isolation()

    # Ensure project root is first in sys.path
    root_str = str(_root)
    if root_str in sys.path:
        sys.path.remove(root_str)
    sys.path.insert(0, root_str)


def _install_log_isolation() -> None:
    """把测试进程的日志隔离装上（环境变量 + handler + 封堵 ``.env`` 覆盖 + ``setup_logging`` 守卫）。"""
    import os

    from tests.log_isolation import apply_test_log_isolation, install_setup_logging_guard

    apply_test_log_isolation()
    install_setup_logging_guard()

    # 封堵 ①：``load_env`` 是**覆盖**语义 ⇒ 它会把 ``LOG_FILE`` 改回 ``.env`` 里的值。
    # ⚠ 必须在 ``interfaces.main`` 被 import **之前**改（``from load_env import load_env``
    #   是在 import 时绑定函数对象的 ⇒ 晚于它改就来不及了）。
    try:
        import load_env as load_env_module
    except Exception:  # pragma: no cover - 仓库根不在 sys.path 时忽略
        return

    if getattr(load_env_module, "_plotpilot_test_patched", False):
        return

    original = load_env_module.load_env

    def _load_env_without_clobbering_log_file(path=None):
        previous = os.environ.get("LOG_FILE")
        result = original(path)
        if previous is not None:
            os.environ["LOG_FILE"] = previous
        return result

    load_env_module.load_env = _load_env_without_clobbering_log_file
    load_env_module._plotpilot_test_patched = True


def pytest_collection_finish(session):
    """收集结束后再修一次日志隔离。

    ⚠ 收集期会 import 各种模块（``interfaces.main`` 就是在这一阶段挂上生产日志 handler 的）
    ⇒ 收集结束必须再清理一次，否则收集期产生的日志已经写进生产文件了。
    """
    from tests.log_isolation import apply_test_log_isolation

    apply_test_log_isolation()


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _keep_test_logs_isolated():
    """每个用例**前**再修一次日志隔离。

    ⚠ 为什么每个用例都要修：隔离是在 ``pytest_configure`` 装的，但运行期还可能
    再被掀翻（``.env`` 覆盖、模块级 ``setup_logging``、某个用例自己改了 env）。
    ⇒ 用例开始前把「环境变量 + 生产 handler」拉回临时目录，
    保证**用例执行期间**的日志不会落进生产文件。
    """
    from tests.log_isolation import apply_test_log_isolation

    apply_test_log_isolation()
    yield


@pytest.fixture(autouse=True)
def _reset_process_level_caches():
    """每个测试前后清进程级缓存，保证 env patch 与依赖替换的隔离性。

    get_backend_settings 现为 lru_cache 进程级缓存（D9），
    不清会让前一个测试的 env 泄漏到后续测试。

    D-01/D-02 的两个 planner 工厂也带 @lru_cache（它们必须长生命周期，
    否则实例上的权重缓存与失败退避永远失效）。副作用是：某个测试替换了
    ``get_llm_service`` 之后，后续测试仍会拿到**首次调用时捕获**的实例。
    这里一并清掉。
    """
    _clear_all()
    yield
    _clear_all()


@pytest.fixture(autouse=True)
def _restore_prompt_singletons():
    """还原 CPMS 全局单例（``_manager_instance`` / ``_registry_instance``）。

    多个用例会**临时**把这两个模块级全局指向自己的临时库，用完把库关了
    却没还原 ⇒ 全局单例就此指向一个**已关闭的库** ⇒ 后面任何用到
    ``get_prompt_registry()`` / ``get_prompt_manager()`` 的用例都会炸
    「sqlite3.ProgrammingError: Cannot operate on a closed database」。

    这不是某个用例的 bug，是**全局状态泄漏**：谁改了全局谁负责还原，
    但漏一处就要炸后面一串。与其逐个文件记得还原，不如在这里统一兜底。
    """
    try:
        import infrastructure.ai.prompt_manager as prompt_manager_module
        import infrastructure.ai.prompt_registry as prompt_registry_module
    except Exception:  # pragma: no cover - 导入失败时不该挡住整个套件
        yield
        return

    prev_manager = prompt_manager_module._manager_instance
    prev_registry = prompt_registry_module._registry_instance
    try:
        yield
    finally:
        prompt_manager_module._manager_instance = prev_manager
        prompt_registry_module._registry_instance = prev_registry


def _clear_all():
    try:
        from interfaces.api.settings import reset_backend_settings_cache

        reset_backend_settings_cache()
    except Exception:
        pass
    for factory in ("get_context_weight_planner", "get_chapter_mode_planner"):
        try:
            import interfaces.api.dependencies as deps

            fn = getattr(deps, factory, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()
        except Exception:
            pass
