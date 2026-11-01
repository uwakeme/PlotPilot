# -*- coding: utf-8 -*-
"""第 77 批（C）守卫：守护进程监督者徽标（顶栏）。

⚠ 前端**没有测试设施**（只有 ``npm run build``）⇒ 这里只能做**文件内容级**断言
  （与 ``tests/unit/test_frontend_dag_metrics_contract.py`` 同一纪律）。

守的是什么
----------
1. 徽标**真的挂在顶栏**上（否则「写书时看不到」这件事没解决）；
2. 徽标**不自己发请求** —— 它只是既有 ``autopilotStatusStore`` 的**又一个订阅者**
   ⇒ ``/autopilot/status`` 仍然只有**一条**轮询链（前端零新增轮询通道）；
3. 平时**不占位**（只有「重启过 / 拉起失败」才出现）⇒ 不给用户添噪音。
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "src"
BADGE = FRONTEND / "components" / "global" / "DaemonSupervisorBadge.vue"
TOP_BAR = FRONTEND / "components" / "stats" / "StatsTopBar.vue"
STORE = FRONTEND / "stores" / "autopilotStatusStore.ts"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_the_badge_exists_and_is_mounted_in_the_top_bar():
    """⚠ 徽标必须**真的**挂在顶栏（不是建了个组件没人用）。"""
    assert BADGE.is_file(), f"徽标组件不存在：{BADGE}"
    top_bar = _read(TOP_BAR)
    assert "DaemonSupervisorBadge" in top_bar, "顶栏没有挂守护进程监督者徽标"
    assert ":novel-id=" in top_bar, "徽标没有拿到 novel id（顶栏的 slug 就是 novel id）"


def test_the_badge_does_not_poll_on_its_own():
    """⚠ 徽标只当**订阅者** ⇒ 不新增轮询通道（否则同一本书会有两条轮询链）。"""
    badge = _read(BADGE)
    for forbidden in ("autopilotApi", "fetch(", "axios", "setInterval"):
        assert forbidden not in badge, (
            f"徽标里出现了 {forbidden} ⇒ 它在自己发请求 ⇒ "
            "会出现第二条轮询链（前端零新增通道的纪律被破坏）"
        )
    assert "useAutopilotStatusStore" in badge, "徽标没有复用既有的状态 store"
    assert "store.subscribe(" in badge, "徽标没有订阅 store（那它拿不到数据）"


def test_the_badge_only_shows_when_there_is_something_to_say():
    """⚠ 平时不占位：只有「拉起失败」或「重启过」才显示。"""
    badge = _read(BADGE)
    assert "last_error" in badge, "徽标没有考虑拉起失败"
    assert "restart_attempts" in badge, "徽标没有考虑重启次数"
    assert 'v-if="notice"' in badge, "徽标没有在无内容时隐藏"


def test_the_store_remains_the_single_polling_owner():
    """⚠ store 仍是**单一**轮询链的所有者（徽标只是订阅者）。"""
    store = _read(STORE)
    assert "function subscribe(" in store, "store 没有订阅入口（徽标就没法只当订阅者）"
    assert "activeNovelId" in store, "store 的单一轮询链语义没了"
