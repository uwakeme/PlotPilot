"""结构预算 (Structure Budget) — 目标章数 → 部/卷/幕 结构的唯一真相源。

本模块刻意**零重依赖**（只用标准库），因为两个层次都要读它：

- ``application.blueprint.services.continuous_planning_service``（规划侧）
- ``engine.runtime.*`` / ``engine.pipeline.*``（守护进程与写作管线侧）

把预算规则放在一个轻模块里，写作管线才能在"该不该再写一章 / 该不该再开一幕"
的判定点上直接引用，而不必拖进 json_repair、仓储、CPMS 等一整条依赖链。

预算纪律（2026-10）
--------------------
"设定 5 章却写成 50 多章"由三层成因叠加，本模块负责第一层：

1. 结构引擎放大目标（此处修复）
2. 宏观规划的每幕章数预算在落库时被丢弃（continuous_planning_service 修复）
3. 幕阶梯没有终点（act_planning_delegate 修复）

``calculate_structure_params`` **不得放大**用户设定的目标：总章数只允许略高于
目标（向上取整的余数），不允许数倍膨胀。
"""

from __future__ import annotations

from typing import Dict, Optional


def calculate_structure_params(target_chapters: int) -> Dict:
    """根据目标总章节数，计算最优的结构参数。

    任何需要知道"该有几幕"、"每幕该几章"的地方都应调用此函数。

    设计原则（基于叙事工程学 + 网文商业实践）：
    - 一幕 = 一个完整的叙事弧线（激励事件 → 发展 → 高潮 → 降级）
    - 一卷 = 一个大的故事单元
    - 一部 = 一个大阶段（起源 / 发展 / 决战）
    - 每幕章数随总篇幅增长：短篇每幕薄，长篇每幕厚

    Args:
        target_chapters: 目标总章节数

    Returns:
        {
            "parts": int,                 # 部数
            "volumes_per_part": int,      # 每部卷数
            "acts_per_volume": int,       # 每卷幕数
            "chapters_per_act": int,      # 每幕建议章数
            "total_acts": int,            # 总幕数
            "total_chapter_budget": int,  # 结构总章数（= total_acts × chapters_per_act）
            "reasoning": str,             # 计算理由（用于日志）
        }
    """
    t = max(int(target_chapters or 0), 1)

    if t <= 30:
        # 短篇：幕数随篇幅收缩，保证每幕至少 5 章。历史实现固定 3 幕且每幕下限
        # 5 章，会把 5~15 章的小目标静默顶成 15 章（5 章目标 → 3 倍膨胀）。
        parts, vpp = 1, 1
        apv = max(1, min(3, t // 5))
        floor_cpa = 5
        cpa = max(2, -(-t // apv))  # 向上取整：宁可略超，不可欠章
        total_acts = apv
        reason = f"短篇({t}章)：1部×1卷×{apv}幕，每幕{cpa}章（总预算{total_acts * cpa}章）"
    elif t <= 80:
        # 中短篇：1部2卷，每卷3-4幕
        parts, vpp = 1, 2
        apv = 4 if t > 50 else 3
        floor_cpa = 5
        reason = f"中短篇({t}章)：{parts}部×{vpp}卷×{apv}幕"
    elif t <= 200:
        # 中篇：2-3部，每部3卷，每卷4-5幕
        parts = 2 if t <= 120 else 3
        vpp = 3
        apv = 5 if t > 100 else 4
        floor_cpa = 6
        reason = f"中篇({t}章)：{parts}部×{vpp}卷×{apv}幕"
    elif t <= 500:
        # 长篇：3-4部，每部3-4卷，每卷5-7幕
        parts = 3 if t <= 300 else 4
        vpp = 3 if t <= 350 else 4
        apv = 6 if t > 300 else 5
        floor_cpa = 8
        reason = f"长篇({t}章)：{parts}部×{vpp}卷×{apv}幕"
    else:
        # 超长篇(500+)
        if t <= 800:
            parts, vpp, apv = 4, 4, 7
        elif t <= 1500:
            parts, vpp, apv = 5, 5, 8
        else:
            parts, vpp, apv = 6, 6, 10
        floor_cpa = 10
        reason = f"超长篇({t}章)：{parts}部×{vpp}卷×{apv}幕（动态扩展）"

    if t > 30:
        # 网格容量只是**上限**，不是配额。每幕章数存在叙事地板（floor_cpa），
        # 当 网格幕数 × 地板章数 > 目标章数 时，地板会把目标顶起来
        # （历史实现 t=100 → 24幕×6章 = 144 章，是目标的 1.44 倍；
        #   t=600 → 112幕×10章 = 1120 章，接近 2 倍）。
        # 此时收缩幕数而不是抬高章数：总章数回到目标附近。
        grid_acts = parts * vpp * apv
        total_acts = min(grid_acts, max(1, -(-t // floor_cpa)))
        cpa = max(floor_cpa, -(-t // total_acts))  # 向上取整覆盖目标
        reason = f"{reason}，实排{total_acts}幕×每幕{cpa}章（总预算{total_acts * cpa}章）"

    return {
        "parts": parts,
        "volumes_per_part": vpp,
        "acts_per_volume": apv,
        "chapters_per_act": cpa,
        "total_acts": total_acts,
        "grid_total_acts": parts * vpp * apv,
        "total_chapter_budget": total_acts * cpa,
        "reasoning": reason,
    }


def resolve_chapter_ceiling(target_chapters: Optional[int]) -> Optional[int]:
    """把目标章数收敛成一条**硬上限**：第 N 章之后不再产出新章节。

    这是"不能无限写"的最后一道闸，守护进程与写作管线共用同一个口径。
    返回 None 表示不设上限（目标缺失且不给出默认值时）。

    语义说明：目标章数是**软目标**——达成后 ``target_reached_gate`` 会按书目策略
    分流（暂停待人 / 加终局预算 / 上调续写），而每一次合法的上调都会写回
    ``novel.target_chapters``。因此这里直接读当前值即可：用户主动加写预算后，
    上限自然跟着抬高，无需另设状态。
    """
    try:
        value = int(target_chapters or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def resolve_act_ceiling(
    target_chapters: Optional[int],
    planned_act_count: int = 0,
) -> int:
    """幕阶梯的硬上限：LLM 决定结构形状，引擎只保证"这一幕还有章节可写"。

    这里**不再**用 ``calculate_structure_params`` 的网格预测幕数。理由：部/卷/幕的
    层数与每层章数改由 LLM 依据剧情容量自行决定（见 planning-quick-macro 契约），
    公式预测值与实际落库结构对不上，用它封顶会误杀正常长篇、或放过超长结构。

    新的口径是一条与公式无关的结构性不变量：

        **每一幕至少要消费 1 章，因此幕数不可能超过章节上限。**

    再叠加"LLM 已规划了多少幕"作为下界——已规划的幕永远算数，不因为预算重算
    而被引擎判定为越界（否则重规划会让人工确认过的结构凭空消失）。

    Args:
        target_chapters: 本书目标章数（None / 非法值表示不设章节上限）
        planned_act_count: 已落库的幕节点数（宏观规划实际产出，而非公式预测）

    Returns:
        幕数上限，恒 >= 1。
    """
    try:
        planned = max(int(planned_act_count or 0), 0)
    except (TypeError, ValueError):
        planned = 0

    ceiling = resolve_chapter_ceiling(target_chapters)
    if ceiling is None:
        # 目标章数不可用时退回复用网格预测值：此时没有任何真实信号可用，
        # 总比"无上限"安全（无上限会让幕阶梯重新变成无限增长）。
        return max(int(calculate_structure_params(100)["total_acts"]), planned, 1)
    return max(ceiling, planned, 1)
