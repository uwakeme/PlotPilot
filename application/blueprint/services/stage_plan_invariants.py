# -*- coding: utf-8 -*-
"""A-02：阶段数与断点交回 AI，代码只留三条不变量。

改造前
------
1. ``len(raw_items) != 5`` 直接 raise —— 阶段数由代码写死；
2. ``_chapter_ranges`` 用硬编码 ``ratios = [0.15, 0.40, 0.70, 0.90, 1.0]`` 切分章号区间；
3. ``phase`` / ``label`` 按**下标**从 ``_PHASE_SCHEMA`` 强制回贴 ——
   AI 就算给了别的阶段名，也会被改写成固定的 5 个名字。

改造后
------
AI 自定阶段数与断点。代码只保留三条不变量（登记表 A-02「代码保留的不变量」列）：

  I1 阶段数 ∈ [3, 8]
  I2 断点单调递增且首尾相接（第 i 段起点 = 第 i-1 段终点 + 1，无空洞无重叠）
  I3 末段 ``chapter_end`` == 目标章数

``_PHASE_SCHEMA`` 降级为「AI 什么都没说时的兜底」：
- 仍是 5 段且 AI 未给区间 ⇒ 沿用原比例表（保持既有行为与既有单测不变）
- 其他段数 ⇒ 均分，且每段至少 1 章

AI 给出的 ``chapter_start`` / ``chapter_end`` **优先**于兜底。
"""
from __future__ import annotations

from typing import Any

#: 阶段数下限 / 上限（不变量 I1）
MIN_STAGES = 3
MAX_STAGES = 8

#: 5 段时的兜底比例（保持改造前的既有行为）
_FIVE_STAGE_RATIOS = (0.15, 0.40, 0.70, 0.90, 1.0)

#: 非 5 段时的兜底阶段名
_FALLBACK_LABELS = ("起始", "展开", "推进", "深化", "高潮", "收束", "过渡", "尾声")


def _default_phase_name(index: int, stage_count: int) -> str:
    """AI 没给阶段名时的兜底（按位置取，不映射到固定的 5 个名字）。"""
    label = _FALLBACK_LABELS[index] if index < len(_FALLBACK_LABELS) else f"阶段{index + 1}"
    return f"{label}阶段"


def chapter_ranges(target_chapters: int, stage_count: int) -> list[tuple[int, int]]:
    """按 ``stage_count`` 切分 1..target_chapters，返回互不重叠且首尾相接的区间。

    5 段时沿用改造前的比例表（向后兼容）；其他段数均分。
    每段至少 1 章。若 ``target_chapters < stage_count``，返回尽可能少的段数。
    """
    total = max(int(target_chapters), 1)
    n = max(1, min(int(stage_count), total))

    if n == 5:
        ratios = _FIVE_STAGE_RATIOS
    else:
        ratios = [(i + 1) / n for i in range(n)]

    ends: list[int] = []
    previous = 0
    for index, ratio in enumerate(ratios):
        if index == len(ratios) - 1:
            end = total
        else:
            end = max(previous + 1, round(total * ratio))
            remaining_min = len(ratios) - index - 1
            end = min(end, total - remaining_min)
        ends.append(max(previous + 1, end))
        previous = ends[-1]

    starts = [1, *(end + 1 for end in ends[:-1])]
    return list(zip(starts, ends))


def validate_stage_plan_shape(
    stage_plan: list[dict[str, Any]],
    *,
    target_chapters: int | None = None,
) -> list[str]:
    """校验三条不变量，返回错误描述列表（空 = 通过）。

    I1 阶段数 ∈ [3, 8]
    I2 断点单调递增且首尾相接（第 i 段起点 = 第 i-1 段终点 + 1，无空洞无重叠）
    I3 首段起点 = 1；且末段终点 ≥ 目标章数（计划必须覆盖到目标）

    注意 I3 **不是**「末段终点必须等于目标章数」—— 既有行为允许计划覆盖到
    比目标更大的章数（见 ``test_normalize_plot_outline_uses_explicit_chapter_ranges``：
    目标 100 章，阶段区间显式给到 500）。等值约束会与该行为冲突且不必要 ——
    真正要保证的是「计划不许比目标短」。
    """
    errors: list[str] = []
    if not isinstance(stage_plan, list) or not stage_plan:
        return ["stage_plan 为空"]

    count = len(stage_plan)
    if not (MIN_STAGES <= count <= MAX_STAGES):
        errors.append(f"阶段数 {count} 不在 [{MIN_STAGES}, {MAX_STAGES}] 区间内")

    for index, stage in enumerate(stage_plan):
        start = int(stage.get("chapter_start") or 0)
        end = int(stage.get("chapter_end") or 0)
        if start <= 0 or end <= 0:
            errors.append(f"stage_plan[{index}] 章节范围缺失")
            continue
        if start > end:
            errors.append(f"stage_plan[{index}] 章节范围不合法（{start} > {end}）")
            continue
        if index > 0:
            prev_end = int(stage_plan[index - 1].get("chapter_end") or 0)
            if start != prev_end + 1:
                errors.append(
                    f"stage_plan[{index}] 起点 {start} 与上一段终点 {prev_end} 不相接"
                    f"（不变量：首尾相接、无空洞无重叠）"
                )

    first_start = int(stage_plan[0].get("chapter_start") or 0)
    if first_start != 1:
        errors.append(f"stage_plan 首段起点应为第 1 章，实际 {first_start}")

    last_end = int(stage_plan[-1].get("chapter_end") or 0)
    if target_chapters and last_end < int(target_chapters):
        errors.append(
            f"stage_plan 末段终点 {last_end} 小于目标章数 {target_chapters}"
            f"（计划必须覆盖到目标）"
        )

    return errors
