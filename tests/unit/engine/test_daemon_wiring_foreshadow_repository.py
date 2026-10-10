"""生产对象图必须真的挂上伏笔仓储（C1 回归门）。

背景：伏笔绑定校验的每一层——域字段、mapper、prompt、_check_foreshadow_binding、
门控解耦——都有测试且都正确，但没有一条测试走**生产接线**：
`scripts.start_daemon.build_daemon()` 造出的 daemon/runner 上
`foreshadowing_repository` 恒为 None（daemon_kwargs 漏传），于是

    daemon → runner → PipelineContext  全是 None
        → base._build_foreshadows 每章短路 return []
            → 伏笔绑定校验在生产上一次都没跑过

四轮评审全部漏掉，根因是所有测试都直接给 PipelineContext 塞桩仓储：
结构上"接上了没有"验得到，语义上"接上的是不是 None"没人验。
所以本文件只做一件事——用真实的 build_daemon() 断言值非 None。

DB 隔离：monkeypatch `application.paths.get_db_path`，把 build_daemon 及其
依赖的 factories 全部导向临时库（本仓已知问题：pytest tests/unit 会写生产库）。
"""
import pytest


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    import os

    # 快照必须早于 import：scripts.start_daemon 在**模块级**执行 load_dotenv()，
    # 会把 .env 灌进进程环境变量（OPENAI_API_KEY / EMBEDDING_API_KEY 等），
    # 污染后续用例（test_embedding_environment 会读到真的 key）。
    env_snapshot = dict(os.environ)

    from infrastructure.persistence.database.connection import DatabaseConnection
    from scripts import start_daemon

    db_path = str(tmp_path_factory.mktemp("daemon-wiring") / "wiring.db")
    DatabaseConnection(db_path)  # 建表/迁移，让仓储构造不炸

    patcher = pytest.MonkeyPatch()
    # get_database() 无参调用时是调用现场 import application.paths.get_db_path，
    # 所以打在这里能覆盖 start_daemon 之外的所有工厂
    patcher.setattr("application.paths.get_db_path", lambda: db_path)
    patcher.setattr(start_daemon, "get_db_path", lambda: db_path)
    try:
        yield start_daemon.build_daemon()
    finally:
        patcher.undo()
        os.environ.clear()
        os.environ.update(env_snapshot)


def test_daemon_carries_foreshadowing_repository(daemon):
    """C1 本体：daemon 上的伏笔仓储不得是 None。

    删掉 start_daemon.py daemon_kwargs 里的 foreshadowing_repository= 立刻转红。
    """
    assert daemon.foreshadowing_repository is not None, (
        "build_daemon() 没有注入伏笔仓储 → 绑定校验每章静默跳过"
    )


def test_runner_carries_foreshadowing_repository(daemon):
    """daemon 挂上了还不够，写作阶段真正用的 runner 上也必须有。"""
    assert daemon.runner.foreshadowing_repository is not None


def test_writing_delegate_runner_carries_foreshadowing_repository(daemon):
    """走 writing_delegate 真正构建的 runner（生产写作路径）。

    writing_delegate 从 daemon 属性逐个转发依赖，任何一段漏转发都会在这里断掉。
    """
    from engine.runtime.writing_delegate import _build_runner

    runner = _build_runner(daemon)

    assert runner.foreshadowing_repository is not None


def test_pipeline_reaches_the_repository_instead_of_short_circuiting(daemon):
    """最末端：runner 组装出的 ctx 上带着真仓储，_build_foreshadows 会真的去取数。

    只断言"仓储非 None"仍可能漏掉最后一段转发（_make_context → ctx.inject），
    所以这里走真实的 _make_context，并在仓储取数入口上装探针。
    """
    runner = daemon.runner
    repo = runner.foreshadowing_repository

    calls = []
    original = repo.get_by_novel_id

    def _probe(_novel_id):
        calls.append(_novel_id)
        return None  # 本书没有伏笔数据，走正常降级路径

    repo.get_by_novel_id = _probe
    try:
        ctx = runner._make_context("novel-wiring", 1)
        assert runner._build_foreshadows(ctx) == []
    finally:
        del repo.get_by_novel_id

    assert ctx.foreshadowing_repository is repo
    assert len(calls) == 1, (
        "仓储没被取用过：ctx.foreshadowing_repository 是 None，_build_foreshadows 直接短路"
    )
    assert original is not None