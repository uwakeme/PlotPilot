# -*- coding: utf-8 -*-
"""第 79 批守卫：守护进程监督者徽标（**全局**版，读 ``GET /health``）。

⚠ 前端**没有测试设施**（只有 ``npm run build``）⇒ 这里只能做**文件内容级**断言
  （与 ``tests/unit/test_frontend_dag_metrics_contract.py`` 同一纪律）。

守的是什么
----------
1. 徽标**真的挂在两处**（工作台顶栏 + 书库页）⇒ 「任何页面都看得见」；
2. 数据源是**全局**的 ``/health``（不碰 DB ⇒ 轮询很轻；且书库页没有 novel 上下文）；
3. **不许**退回按书订阅（那会为了一枚徽标硬拉起 per-novel 的状态轮询）；
4. 轮询间隔**不许太密**（≥ 15 秒 ⇒ 不是热循环）；
5. 拉不到数据时**保留上一次的值**（≠「一切正常」⇒ 徽标不许假装没事）。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "src"
BADGE = FRONTEND / "components" / "global" / "DaemonSupervisorBadge.vue"
TOP_BAR = FRONTEND / "components" / "stats" / "StatsTopBar.vue"
HOME = FRONTEND / "views" / "Home.vue"
HEALTH_STORE = FRONTEND / "stores" / "daemonHealthStore.ts"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _code_only(path: pathlib.Path) -> str:
    out = []
    for line in _read(path).splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
            continue
        out.append(line)
    return "\n".join(out)


def test_the_badge_is_mounted_in_both_the_top_bar_and_the_library_page():
    """⚠ 「任何页面都看得见」⇒ 两处都要挂（书库页没有 novel 上下文，最容易漏）。"""
    assert BADGE.is_file(), f"徽标组件不存在：{BADGE}"
    assert "DaemonSupervisorBadge" in _read(TOP_BAR), "工作台顶栏没挂监督者徽标"
    assert "DaemonSupervisorBadge" in _read(HOME), "书库页（Home）没挂监督者徽标"


def test_the_badge_reads_the_global_health_store():
    """⚠ 数据源是**全局**的 `/health` 链（不碰 DB、不按书订阅）。"""
    badge = _read(BADGE)
    assert "useDaemonHealthStore" in badge, "徽标没有用全局健康 store"
    # ⚠ 不许退回按书订阅：那会为了一枚徽标硬拉起 per-novel 的状态轮询
    for forbidden in ("autopilotStatusStore", "autopilotApi", "/autopilot/status"):
        assert forbidden not in badge, (
            f"徽标里出现了 {forbidden} ⇒ 它改成了按书订阅 ⇒ "
            "会为了一枚徽标硬拉起一条 per-novel 轮询链"
        )
    assert "novelId" not in badge, "徽标不该再依赖 novel id（它是进程级的）"


def test_the_health_store_polls_the_health_endpoint():
    """⚠ store 必须打 `/health`（后端已把监督者状态放进这个载荷）。"""
    store = _read(HEALTH_STORE)
    assert "fetchJson" in store, "健康 store 没用统一的 fetchJson"
    assert "'/health'" in store or '"/health"' in store, "健康 store 没打 /health"
    assert "daemon_supervisor" in store, "健康 store 没读 daemon_supervisor 字段"


def test_the_poll_interval_is_not_a_hot_loop():
    """⚠ 轮询间隔 ≥ 15 秒（`/health` 虽轻，也不该变成热循环）。"""
    store = _read(HEALTH_STORE)
    # ⚠ 数字里可能有下划线分隔（``30_000``）⇒ 匹配要带上 `_`
    m = re.search(r"POLL_INTERVAL_MS\s*=\s*([\d_]+)", store)
    assert m, "找不到轮询间隔常量"
    interval_ms = int(m.group(1).replace("_", ""))
    assert interval_ms >= 15_000, f"轮询间隔只有 {interval_ms}ms ⇒ 太密"


def test_a_failed_read_keeps_the_last_known_value():
    """⚠ 拉不到数据 ≠「一切正常」⇒ 徽标不许把上一次的状态清掉。"""
    store = _code_only(HEALTH_STORE)
    # catch 分支里只更新 lastError，**不**把 supervisor 置空
    catch_block = store.split("catch (e)", 1)[-1].split("}", 1)[0] if "catch (e)" in store else ""
    assert catch_block, "健康 store 没有 catch 分支"
    assert "supervisor.value = null" not in catch_block, (
        "读取失败时把 supervisor 清空了 ⇒ 徽标会假装「一切正常」"
    )


def test_the_badge_only_shows_when_there_is_something_to_say():
    """⚠ 平时不占位：只有「拉起失败」或「重启过」才显示。"""
    badge = _read(BADGE)
    assert "last_error" in badge, "徽标没有考虑拉起失败"
    assert "restart_attempts" in badge, "徽标没有考虑重启次数"
    assert 'v-if="notice"' in badge, "徽标没有在无内容时隐藏"
