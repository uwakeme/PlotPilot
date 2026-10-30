# -*- coding: utf-8 -*-
"""第 64 批：守护进程被杀掉又自动拉起 ⇒ **用户看得见**。

链路（前端**零新增轮询**）：
  监督者（interfaces 层，API 进程内）
    ⇒ 共享内存（`shared_state_repository`）
    ⇒ ``/autopilot/status`` 的 ``daemon_supervisor`` 字段
    ⇒ 前端**本来就在轮询**的 AutopilotPanel
    ⇒ 面板上的一枚小徽标：「已自动重启 N 次」/「自动拉起失败」

⚠ 为什么必须走共享内存而不是新开 HTTP 端点：
``application`` 层**不能**反向依赖 ``interfaces``（分层铁律）⇒ 监督者状态住在
interfaces 层 ⇒ 要让 application 层的 ``/status`` 带出来，只能走既有的跨进程通道。
"""
import pathlib
from types import SimpleNamespace

import pytest

from application.engine.services.query_service import QueryService
from application.engine.services.shared_state_repository import SharedStateRepository
from interfaces.daemon_manager import (
    DaemonSupervisor,
    DaemonSupervisorSettings,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]


# ─────────────────────── 1. 共享内存：存取面 ───────────────────────


def test_shared_state_round_trips_the_supervisor_state():
    """监督者状态要能经共享内存往返（写进去能原样读出来）。"""
    repo = SharedStateRepository(shared_dict={})
    payload = {
        "enabled": True,
        "running": True,
        "restart_attempts": 2,
        "last_restart_at": 123.0,
        "last_error": "",
    }

    repo.update_daemon_supervisor_state(payload)

    assert repo.get_daemon_supervisor_state() == payload


def test_shared_state_returns_empty_dict_when_nothing_was_published():
    """没发布过 ⇒ 空字典（**不是** None ⇒ 消费方不必判空）。"""
    repo = SharedStateRepository(shared_dict={})

    assert repo.get_daemon_supervisor_state() == {}


# ─────────────────────── 2. 监督者：真的发布 ───────────────────────


def test_the_supervisor_publishes_its_state():
    """监督者每检查一次 ⇒ 把状态发布出去（前端才看得见）。"""
    published: list[dict] = []

    class _FakeManager:
        def status(self):
            return SimpleNamespace(running=True, pid=1234)

        def start(self):  # pragma: no cover —— 守护进程活着 ⇒ 不该被调用
            raise AssertionError("守护进程活着时监督者不许拉起")

    supervisor = DaemonSupervisor(
        _FakeManager(),
        DaemonSupervisorSettings(),
        state_publisher=published.append,
    )

    assert supervisor._check_once() == "alive"

    assert published, "监督者没有发布状态 ⇒ 前端永远看不到"
    state = published[-1]
    assert state["enabled"] is True
    assert state["restart_attempts"] == 0
    assert "last_error" in state
    assert "running" in state


def test_publishing_failure_does_not_break_the_supervisor():
    """发布失败（共享内存挂了）不许拖垮监督者本身。"""

    def _boom(_state):
        raise RuntimeError("共享内存不可用")

    class _FakeManager:
        def status(self):
            return SimpleNamespace(running=False, pid=1234)

        def start(self):
            return None

    supervisor = DaemonSupervisor(
        _FakeManager(),
        DaemonSupervisorSettings(),
        state_publisher=_boom,
        # ⚠ 必须注入锁探针（不许读**真锁**）—— 默认实现读的是生产锁
        #   ⇒ 真锁被占着时这条会走成「locked_elsewhere」（假失败）。
        lock_probe=lambda: False,
    )

    assert supervisor._check_once() == "restarted"


# ─────────────────────── 3. /status 载荷带出它 ───────────────────────


@pytest.mark.parametrize("shared_state_path", ["novel_state_object", "raw_dict"])
def test_the_status_payload_carries_the_supervisor_state(shared_state_path):
    """``/autopilot/status`` 的载荷必须带出 ``daemon_supervisor``（真行为）。

    ⚠ **两条路径都要覆盖**（变异验证抓到过）：
    ``get_novel_status`` 有两个 builder ——
    ① 共享内存里有 ``NovelState`` 对象 ⇒ ``_build_status_from_shared``；
    ② 只有原始 dict ⇒ ``_build_status_from_raw``（兼容旧格式）。
    只覆盖一条 ⇒ 另一条被改坏也照样绿。
    """
    supervisor_state = {
        "enabled": True,
        "running": True,
        "restart_attempts": 3,
        "last_restart_at": 1700000000.0,
        "last_error": "",
    }

    novel_state = SimpleNamespace(
        novel_id="n-1",
        title="测试书",
        autopilot_status="running",
        current_stage="writing",
        current_act=1,
        current_chapter_in_act=2,
        current_beat_index=3,
        current_auto_chapters=4,
        target_chapters=10,
        target_words_per_chapter=2000,
        last_chapter_tension=None,
        consecutive_error_count=0,
        auto_approve_mode=False,
    )

    class _FakeShared:
        def get_novel_state(self, novel_id):
            return novel_state if shared_state_path == "novel_state_object" else None

        def get_raw_state(self, novel_id):
            return {"title": "测试书", "target_chapters": 10}

        def get_chapters(self, novel_id):
            return []

        def is_daemon_alive(self):
            return True

        def get_daemon_heartbeat(self):
            return 1700000000.0

        def get_daemon_supervisor_state(self):
            return dict(supervisor_state)

    service = QueryService(shared_state=_FakeShared())
    payload = service.get_novel_status_dict("n-1")

    assert payload is not None
    assert payload["daemon_supervisor"] == supervisor_state


# ─────────────────────── 4. 前端把它显示出来 ───────────────────────


def test_the_frontend_type_declares_the_supervisor_state():
    """前端类型必须声明 ``daemon_supervisor``（否则消费方只能靠兜底索引）。"""
    source = (REPO_ROOT / "frontend" / "src" / "api" / "autopilot.ts").read_text(
        encoding="utf-8"
    )

    assert "export interface DaemonSupervisorState" in source
    assert "daemon_supervisor?: DaemonSupervisorState" in source


def test_the_frontend_surfaces_the_supervisor_state():
    """面板必须**读**它并**显示**出来（不读 ⇒ 前端等于没有这条链路）。"""
    source = (
        REPO_ROOT
        / "frontend"
        / "src"
        / "components"
        / "autopilot"
        / "AutopilotPanel.vue"
    ).read_text(encoding="utf-8")

    assert "const supervisor = status.value?.daemon_supervisor" in source
    assert "supervisorNotice" in source
    # 文案必须说清「重启过几次」，而不是笼统一句「异常」
    assert "已自动重启" in source
    assert "restart_attempts" in source
