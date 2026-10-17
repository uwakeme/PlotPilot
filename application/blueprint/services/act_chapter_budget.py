"""B-03：幕级章数「校验告警，不改写」。

背景
----
``_rebalance_act_chapters`` 把 LLM 给的 ``estimated_chapters`` 当成**权重**，
重新按 ``target_chapters`` 等比缩放后**直接覆盖**原值，且**没有任何告警分支**。
于是 AI 对每幕章数的判断在落库前就被静默改写。

登记表 B-03 的要求：保留 LLM 原值；总数不符时**告警而非改写**。

本模块提供纯函数式的那一半：不变量校验 + 偏差报告。
改写那一半（``rebalance``）保留但默认关闭，见
``continuous_planning_service._rebalance_act_chapters``。

不变量
------
唯一硬约束是「各级 ``estimated_chapters`` 之和等于 ``target_chapters``」
（prompt 侧由 ``user.md`` 强制）。代码这边**只报告，不纠正** ——
纠正等于替 AI 做决定，正是本批要拆掉的东西。
"""
from __future__ import annotations

from typing import Any, Dict, List


def read_act_chapters(act: Dict[str, Any]) -> int:
    """读取单幕的 ``estimated_chapters``；无法解析时按 0 计（= 没给）。"""
    try:
        return max(int(act.get("estimated_chapters", 0) or 0), 0)
    except (TypeError, ValueError):
        return 0


def check_act_chapter_budget(
    acts: List[Dict[str, Any]],
    target_chapters: int,
) -> Dict[str, Any]:
    """核对各幕章数之和是否等于目标章数。**只报告，不改写。**

    Returns:
        {
            "total": int,          # LLM 给的章数之和
            "target": int,         # 目标章数
            "delta": int,          # total - target
            "ok": bool,            # 是否相等
            "act_count": int,
            "missing": int,        # 完全没给章数的幕数
            "warning": str,        # 人类可读的告警文本；ok 时为 ""
        }
    """
    per_act = [read_act_chapters(act) for act in acts]
    total = sum(per_act)
    target = max(int(target_chapters), 0)
    delta = total - target
    missing = sum(1 for value in per_act if value <= 0)

    if total == target:
        warning = ""
    else:
        warning = (
            f"幕级章数之和 {total} 与目标 {target} 不符（差 {delta:+d}），"
            f"共 {len(acts)} 幕、其中 {missing} 幕未给出章数。"
            f"按设计**保留 LLM 原值不改写**，如需强制归一请显式开启归一开关。"
        )

    return {
        "total": total,
        "target": target,
        "delta": delta,
        "ok": delta == 0,
        "act_count": len(acts),
        "missing": missing,
        "warning": warning,
    }
