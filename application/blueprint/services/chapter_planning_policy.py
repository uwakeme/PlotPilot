"""Shared policy for act and chapter pre-planning."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from application.blueprint.services.foreshadow_pairing import (
    validate_foreshadow_pairing,
)


@dataclass(frozen=True)
class ChapterPlanningPolicy:
    recent_chapter_limit: int = 3
    previous_ending_chars: int = 700
    recent_outline_chars: int = 700
    required_act_plan_fields: tuple[str, ...] = (
        "number",
        "title",
        "main_event",
        "handoff_from_previous",
        "handoff_to_next",
    )


DEFAULT_CHAPTER_PLANNING_POLICY = ChapterPlanningPolicy()


#: 七段执行剧本的**段标题 + 计数单位**（**唯一一处**定义「哪七段、每段怎么报数」）。
#:
#: ⚠ E-01a：此前这段「标题 + 计数单位」在 `chapter_plan_renderer.py` 的段落表里
#: **又写了一份**（与下面的 marker 常量平行）⇒ 改一处就会与另一处对不上，
#: 而「七段」是**代码写死**给模型的骨架（登记表 E-01）。
#: ⇒ 现在渲染器从这张表取标题，marker 常量也由它派生。
CHAPTER_EXECUTION_PLAN_SECTIONS: tuple[tuple[str, str], ...] = (
    ("一、开篇切入点", ""),
    ("二、场景转换列表", ""),
    ("三、关键对话", "组"),
    ("四、剧情事件链", "个事件"),
    ("五、角色关键决策", ""),
    ("六、爽点/反转设计", ""),
    ("七、主角状态变化", ""),
)

#: 七段的段标题（由上面的表派生，**不再手写第二份**）。
CHAPTER_EXECUTION_PLAN_MARKERS = tuple(
    marker for marker, _unit in CHAPTER_EXECUTION_PLAN_SECTIONS
)

CHAPTER_EXECUTION_PLAN_FORBIDDEN_MARKERS = (
    "故事单元检查",
    "情绪变化节点",
)

#: **段落序号前缀**（「一、」/「11、」）—— 检测与重新编号**共用**这一条口径
#: （同一逻辑写两份必然漂移）。
SECTION_NUMBER_PREFIX_RE = re.compile(r"^(?:[一二三四五六七八九十]+|\d+)、")

#: 中文数字 → 阿拉伯数字（只认 1..99，够段落序号用）。
_CN_DIGITS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def parse_section_number(text: str) -> Optional[int]:
    """解析段落序号（中文数字或阿拉伯数字）；不是序号返回 ``None``。"""
    value = (text or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if "十" in value:
        head, _, tail = value.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    total = 0
    for char in value:
        digit = _CN_DIGITS.get(char)
        if digit is None:
            return None
        total = total * 10 + digit
    return total or None


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return len(value) == 0
    return False


def validate_lightweight_act_plan(
    chapters: Any,
    *,
    expected_count: int,
    policy: ChapterPlanningPolicy = DEFAULT_CHAPTER_PLANNING_POLICY,
) -> list[str]:
    """Validate the slim act-level chapter chain before it is persisted."""
    errors: list[str] = []
    if not isinstance(chapters, list):
        return ["chapters must be a list"]
    if expected_count <= 0:
        errors.append("expected chapter count must be positive")
    if len(chapters) != expected_count:
        errors.append(f"expected {expected_count} chapters, got {len(chapters)}")

    for index, raw in enumerate(chapters, start=1):
        if not isinstance(raw, Mapping):
            errors.append(f"chapter {index} must be an object")
            continue
        try:
            number = int(raw.get("number"))
        except (TypeError, ValueError):
            number = None
        if number != index:
            errors.append(f"chapter {index} number must be {index}")
        for field in policy.required_act_plan_fields:
            if _is_blank(raw.get(field)):
                errors.append(f"chapter {index} missing required field: {field}")

    # 伏笔节奏（foreshadow_action 必选 + 配对约束）。
    # 提示词 planning-act/user.md:29,36 早就把这四条写成硬约束，但此前无任何代码
    # 校验 —— 实测生产库 105 幕全部合规，说明模型一直遵守、代码从不校验。
    # 这里接上校验器，给 C-01「允许 AI 报『本幕只需 N 章』」预置兜底安全网。
    errors.extend(validate_foreshadow_pairing(chapters))

    return errors


def has_rendered_chapter_execution_plan(text: str | None) -> bool:
    """这份文本是不是一份**章前执行剧本**？

    ⚠ E-01b：此前判据是「**七段齐且有序**」（七个固定段标题全在且按序出现）。
    现在段落结构由 **AI 选**（七段降为可选契约之一）⇒ 判据改成
    「**有编号段落标题**、编号递增、且不含禁段」。

    对代码自产的产物**判定等价**：细纲要么是齐七段（两种判据都真），
    要么是轻量占位（无编号标题 ⇒ 两种判据都假）。
    唯一改变的是「缺段的七段剧本」：此前判否（会重新生成），现在判真 ——
    那正是「七段齐」这个契约被拆掉的后果。
    """
    value = (text or "").strip()
    if not value:
        return False
    if any(marker in value for marker in CHAPTER_EXECUTION_PLAN_FORBIDDEN_MARKERS):
        return False

    numbers: list[int] = []
    # ⚠ **按行扫**：``^`` 只在行首生效，整段 finditer 只会看到第一行的标题。
    for line in value.splitlines():
        match = SECTION_NUMBER_PREFIX_RE.match(line)
        if match is None:
            continue
        number = parse_section_number(match.group(0).rstrip("、"))
        if number is not None:
            numbers.append(number)
    if not numbers:
        return False
    return all(later > earlier for earlier, later in zip(numbers, numbers[1:]))


def truncate_text(text: str | None, limit: int) -> str:
    value = (text or "").strip()
    if not value or limit <= 0:
        return ""
    return value if len(value) <= limit else value[-limit:]
