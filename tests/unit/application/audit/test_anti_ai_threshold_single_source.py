"""第 70 批守卫：R-3 —— 反 AI 的「严重线」必须**只有一份**（后端）。

背景（第二轮审查报告 ``review-briefs/2026-10-31-review-report-round2-99a4f4e1.md`` R-3）
--------------------------------------------------------------------------
反 AI 阈值此前有**三份**，互不一致：

| 位置 | 阈值 | 生产实测 |
|---|---|---|
| ``application/audit/services/anti_ai_thresholds.py``（后端 SSOT） | 4.0 / 2.0 / 0.8 | 193 行判「严重」 |
| ``application/engine/dag/nodes/anti_ai_nodes.py`` | **``> 50``** | **0 行**能触发（生产最高分 15.3） |
| ``application/engine/dag/nodes/validation_nodes.py`` | **``> 50``** | 同上 |
| ``frontend/src/components/autopilot/CustomNode.vue`` | **``> 5``** | 118 行会变红 |

⇒ DAG 的线高 12.5 倍 ⇒ **该节点永远显示 SUCCESS**（报警器从未响过）；
⇒ 前端 ``> 5`` 与后端 4.0 不一致 ⇒ 4.5 分：后端判「严重」、前端判绿。

⇒ 修：**阈值只在后端**（从 ``ASSESSMENT_THRESHOLDS`` 派生），
  DAG 输出一个**布尔** ``severe`` ⇒ 前端按布尔着色（前端不再有任何数字阈值）。

本文件钉住
----------
1. 「严重线」常量 == ``ASSESSMENT_THRESHOLDS[0][0]``（不是另抄一份）；
2. 两个 DAG 节点的 WARNING 判定**必须引用那个常量**（不许再写死数字）；
3. 两个 DAG 节点必须输出 ``severe`` 布尔（前端靠它着色）；
4. 前端**不许**再出现反 AI 的数字阈值（``severity_score > 5`` 这种）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
DAG_NODES = (
    ROOT / "application" / "engine" / "dag" / "nodes" / "anti_ai_nodes.py",
    ROOT / "application" / "engine" / "dag" / "nodes" / "validation_nodes.py",
)
FRONTEND_SRC = ROOT / "frontend" / "src"


def test_the_severe_threshold_is_the_single_source():
    """⚠ 「严重线」必须就是评级表的第一档（不许另抄一个数）。"""
    from application.audit.services.anti_ai_thresholds import (
        ASSESSMENT_THRESHOLDS,
        SEVERE_ASSESSMENT_THRESHOLD,
    )

    assert SEVERE_ASSESSMENT_THRESHOLD == ASSESSMENT_THRESHOLDS[0][0] == 4.0, (
        f"严重线是 {SEVERE_ASSESSMENT_THRESHOLD}，评级表第一档是 {ASSESSMENT_THRESHOLDS[0][0]}"
    )


def test_the_dag_nodes_derive_their_threshold_from_the_single_source():
    """⚠ 两个 DAG 节点的严重判定必须**引用那个常量**（不许写死数字）。

    ⚠ 写死数字的后果（实测）：``> 50`` ⇒ 生产最高分 15.3 ⇒ 节点**永不报警**。

    ⚠ 判据：对 ``severity_score`` 的**任何数字比较**都不许存在
      （用 AST 找 Compare ⇒ 注释里的数字不算）。
    """
    for path in DAG_NODES:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)

        assert "SEVERE_ASSESSMENT_THRESHOLD" in src, (
            f"{path.name} 没有引用单一事实源的严重线"
        )

        offenders = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
            and "severity_score" in ast.unparse(node)
            and re.search(r"[<>=]=?\s*\d", ast.unparse(node))
        ]
        assert not offenders, (
            f"{path.name} 里对 severity_score 写了数字阈值：{offenders}"
            f"⇒ 它比后端「严重」线高 ⇒ 节点永远 SUCCESS（报警器从未响过）"
        )


def test_the_dag_nodes_output_the_boolean_flag():
    """⚠ 两个 DAG 节点必须输出 ``severe`` 布尔（前端靠它着色，不靠自己的阈值）。"""
    for path in DAG_NODES:
        src = path.read_text(encoding="utf-8")
        assert '"severe": severe' in src, f"{path.name} 的 outputs 里没有 severe 布尔"
        assert '"severe": severe' in src, f"{path.name} 的 metrics 里没有 severe 布尔"


def test_the_frontend_has_no_hardcoded_anti_ai_score_threshold():
    """⚠ 前端**不许**再自己判「严重」（此前 ``> 5`` 与后端 4.0 不一致）。

    ⚠ 4.5 分这个真实样本：后端判「严重」、前端判绿 ⇒ 同一件事两个结论。
    """
    offenders: list[str] = []
    for path in FRONTEND_SRC.rglob("*"):
        if path.suffix not in {".ts", ".vue"} or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if "severity_score" not in line:
                continue
            if re.search(r"severity_score\s*[<>]=?\s*\d", line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")

    assert not offenders, (
        "前端又出现了反 AI 的数字阈值（阈值只该在后端判）：\n  " + "\n  ".join(offenders)
    )
