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

    # Ensure project root is first in sys.path
    root_str = str(_root)
    if root_str in sys.path:
        sys.path.remove(root_str)
    sys.path.insert(0, root_str)


import pytest  # noqa: E402


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
