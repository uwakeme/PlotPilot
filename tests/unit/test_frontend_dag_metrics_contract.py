# -*- coding: utf-8 -*-
"""第 76 批 U-1' / U-2' 守卫：前端 DAG 画布的指标契约。

⚠ 前端**没有测试设施**（只有 ``npm run build``）⇒ 这里只能做**文件内容级**断言
  （与 ``tests/unit/test_frontend_tristate_consumers.py`` 同一纪律）。

守的是什么
----------
U-2'：``NodeRunState.metrics`` 声明成 ``Record<string, number>``，实际后端会塞**布尔**
      （反 AI 的 ``severe``）⇒ 类型骗人；
      而 ``Number(value)`` 会把 ``true`` 变成 ``1`` ⇒ 布尔被悄悄变成数字。
U-1'：画布上 ``val_style`` / ``val_tension`` / ``val_anti_ai`` 三个指标分支
      **永远不会被填充**（数据源是 DAG 运行时的 metrics，而运行入口是 501
      ⇒ 运行时根本不跑；唯一的推送出口 ``publish_sse_event`` 没有调用者）⇒ 死代码。
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "src"
DAG_TYPES = FRONTEND / "types" / "dag.ts"
DAG_STORE = FRONTEND / "stores" / "dagStore.ts"
CUSTOM_NODE = FRONTEND / "components" / "autopilot" / "CustomNode.vue"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _code_only(path: pathlib.Path) -> str:
    """只留**代码行**（去掉注释）⇒ 注释里提到某个写法不算违规。"""
    out = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
            continue
        out.append(line)
    return "\n".join(out)


def test_the_metrics_type_admits_booleans():
    """⚠ metrics 里可能有布尔（``severe``）⇒ 类型不许骗人。"""
    text = _read(DAG_TYPES)
    assert "metrics: Record<string, number | boolean>" in text, (
        "DAG 的 metrics 类型又退回 Record<string, number> 了"
        "（后端会塞布尔 severe ⇒ 类型骗人）"
    )


def test_the_store_does_not_coerce_booleans_into_numbers():
    """⚠ ``Number(value)`` 会把 ``true`` 变成 ``1`` ⇒ 布尔必须**原样保留**。"""
    text = _code_only(DAG_STORE)
    # ⚠ 关键不是「不许出现 Number(...)」，而是「**布尔先判**、布尔不许被转成数字」
    bool_at = text.find("typeof value === 'boolean'")
    number_at = text.find("Number(value)")
    assert bool_at != -1, "布尔必须原样保留（不许转数字）"
    assert number_at != -1, "数字分支还得能转换（否则非数字的字符串会被塞进去）"
    assert bool_at < number_at, (
        "布尔判断必须在数字转换**之前**"
        "（否则 true 会被 Number() 变成 1 ⇒ 语义丢了）"
    )


def test_the_progress_field_only_reads_numbers():
    """⚠ ``progress`` 是数值字段 ⇒ 布尔不是进度。"""
    text = _code_only(DAG_STORE)
    assert "function numericProgress(" in text, "progress 没有走「只认数字」的取值"
    assert "nextMetrics.progress ??" not in text, "progress 又直接取 metrics.progress 了"


def test_the_dead_metric_branches_are_gone():
    """⚠ 那三个指标分支永远不会被填充 ⇒ 不许留在界面上假装有阈值。"""
    text = _read(CUSTOM_NODE)
    for dead in ("metrics.drift_score", "metrics.composite", "metrics.severity_score"):
        assert dead not in text, (
            f"{dead} 又回来了 —— 它的数据源是 DAG 运行时的 metrics，"
            "而运行入口是 501 ⇒ 永远不会有数据"
        )


def test_the_frontend_has_no_node_metric_thresholds():
    """⚠ 画布上不许再有「那半个阈值」（0.5 / 30）—— 后端没数据，前端判了也白判。

    ⚠ 真正的阈值在**后端**（反 AI 那条见
    ``tests/unit/application/audit/test_anti_ai_threshold_single_source.py``），
    真实的指标展示在**仪表盘**里。
    """
    text = _read(CUSTOM_NODE)
    start = text.find("const displayMetrics")
    assert start != -1, "找不到 displayMetrics"
    body = text[start : text.find("return items.slice", start)]
    assert "drift_score" not in body
    assert "composite" not in body
    assert "> 0.5" not in body, "前端又自己判了一个偏离度阈值"
    assert "< 30" not in body, "前端又自己判了一个综合张力阈值"
