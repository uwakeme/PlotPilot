"""J-01：目标章数与终局预算的**后端单一来源**。

改造前的问题（登记表 J-01 + 批次计划核实）：

1. **前端代答**：`AutopilotPanel.vue` 自己算「继续写」要加多少章
   （``done + max(20, round(target * 0.2))``）再 PUT 回后端。
   登记表说这是「不可改的答案」**有偏差** —— 它只是输入框的**初值**，用户可改；
   但「该加多少章」这个判断确实写死在前端。
2. **两套公式**（同一个问题两个答案，差 2 倍）：
   - 前端「继续写」：``max(20, round(target * 0.2))``（加在完成数上）
   - 闸门 `target_reached_gate` 自动续写：``max(10, round(target * 0.1))``
     （加在 ``max(target, 完成数)`` 上）
   两者**意图不同**（用户手动 vs 系统自动），但都叫「再写多少章」，
   散在两个文件里 ⇒ 迟早漂移。现在都从这里取。
3. **上限只在前端**：终局预算 [5,200] 的钳制写在输入框上；
   后端**只在读取偏好时钳**（``GenerationPreferences.from_dict``），
   而 ``target_chapters`` 本身**无上限**（``UpdateNovelRequest`` 只有 ``gt=0``）。

现在：提案、钳制、上限都在**后端一处**；前端只显示后端给的初值，用户仍可改。
AI 提案要接进来时，替换的也是这一处。

两套「加多少章」保留为两个**意图不同**的函数（不是同一逻辑写两份）：
- :func:`propose_continue_block` —— 用户手动点「继续写」：给得大方些（20%）；
- :func:`auto_extend_block` —— 无人值守的自动续写策略：保守些（10%），
  避免一本书在用户不在时无限膨胀。
"""
from __future__ import annotations

from domain.novel.value_objects.generation_preferences import (
    FINALE_BUDGET_DEFAULT,
    FINALE_BUDGET_MAX,
    FINALE_BUDGET_MIN,
    clamp_finale_budget,
)

#: 目标章数硬上限（与守护进程 ``max_auto_chapters`` 默认 9999 同口径）
TARGET_CHAPTERS_MAX = 9999

#: 用户手动「继续写」的加写块：下限 / 比例
CONTINUE_BLOCK_MIN = 20
CONTINUE_BLOCK_RATIO = 0.2

#: 无人值守自动续写的加写块：下限 / 比例（比手动更保守）
AUTO_EXTEND_BLOCK_MIN = 10
AUTO_EXTEND_BLOCK_RATIO = 0.1

__all__ = [
    "AUTO_EXTEND_BLOCK_MIN",
    "AUTO_EXTEND_BLOCK_RATIO",
    "CONTINUE_BLOCK_MIN",
    "CONTINUE_BLOCK_RATIO",
    "FINALE_BUDGET_DEFAULT",
    "FINALE_BUDGET_MAX",
    "FINALE_BUDGET_MIN",
    "DEFAULT_TARGET_CHAPTERS",
    "TARGET_CHAPTERS_MAX",
    "auto_extend_block",
    "clamp_finale_budget",
    "clamp_target_chapters",
    "propose_continue_block",
    "propose_continue_target",
    "resolve_target_chapters",
]


#: 目标章数的**统一默认值**（I-05）。
#: 此前 6 处各写各的兜底（30 / 50 / 100 / 100…）
#: ⇒ **同一本书按不同口径算结构预算**。
DEFAULT_TARGET_CHAPTERS = 100


def resolve_target_chapters(value, *, default: int = DEFAULT_TARGET_CHAPTERS) -> int:
    """目标章数的**唯一解析入口**（I-05）。

    读不到 / 非法 ⇒ ``default``；超界 ⇒ 钳到 ``TARGET_CHAPTERS_MAX``。

    ⚠ 与 ``clamp_target_chapters`` 的差别：那个把「≤0 未设置」当作 0 返回；
    这个给的是**一个能用的数字**（结构预算不能用 0）。
    """
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return min(number, TARGET_CHAPTERS_MAX)


def clamp_target_chapters(value) -> int:
    """目标章数钳到 [1, TARGET_CHAPTERS_MAX]；≤0 视为未设置（返回 0）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    if number <= 0:
        return 0
    return min(number, TARGET_CHAPTERS_MAX)


def propose_continue_block(*, target_chapters: int, completed_chapters: int = 0) -> int:
    """用户手动「继续写」的加写块（章）。

    旧实现：前端 ``max(20, round(target * 0.2))`` —— 搬到这里，前端不再自己算。
    """
    target = max(int(target_chapters or 0), 0)
    if target <= 0:
        return CONTINUE_BLOCK_MIN
    return max(CONTINUE_BLOCK_MIN, int(round(target * CONTINUE_BLOCK_RATIO)))


def auto_extend_block(*, target_chapters: int) -> int:
    """无人值守自动续写的加写块（章）。

    比手动保守：用户不在场时不让目标无限膨胀。
    """
    target = max(int(target_chapters or 0), 0)
    if target <= 0:
        return AUTO_EXTEND_BLOCK_MIN
    return max(AUTO_EXTEND_BLOCK_MIN, int(round(target * AUTO_EXTEND_BLOCK_RATIO)))


def propose_continue_target(*, target_chapters: int, completed_chapters: int) -> int:
    """「继续写」建议的新目标章数（用户可改）。

    口径与旧前端一致：``完成数 + 加写块``。
    """
    completed = max(int(completed_chapters or 0), 0)
    block = propose_continue_block(
        target_chapters=target_chapters, completed_chapters=completed
    )
    return clamp_target_chapters(completed + block)
