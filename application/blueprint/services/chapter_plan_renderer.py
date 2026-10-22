"""Rendering helpers for chapter execution plans."""
from __future__ import annotations

import re
from typing import Any, Callable, List

from application.blueprint.services.chapter_planning_policy import (
    CHAPTER_EXECUTION_PLAN_SECTIONS,
    SECTION_NUMBER_PREFIX_RE,
)

#: 段落序号用的中文数字（自选结构用；标准七段的序号写在段标题里）。
_SECTION_NUMERALS = (
    "一",
    "二",
    "三",
    "四",
    "五",
    "六",
    "七",
    "八",
    "九",
    "十",
)


def stringify_plan_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "、".join(
            text for item in value if (text := stringify_plan_value(item))
        )
    if isinstance(value, dict):
        for key in ("content", "summary", "description", "text", "purpose", "decision", "change"):
            text = stringify_plan_value(value.get(key))
            if text:
                return text
        return "；".join(
            f"{key}：{text}"
            for key, raw in value.items()
            if (text := stringify_plan_value(raw))
        )
    return str(value).strip()


def format_scene_transition(item: Any, index: int) -> str:
    if not isinstance(item, dict):
        return stringify_plan_value(item)
    scene = stringify_plan_value(item.get("scene") or item.get("name")) or f"场景{index}"
    location = stringify_plan_value(item.get("location") or item.get("place")) or "未定地点"
    cast = stringify_plan_value(item.get("cast") or item.get("characters") or item.get("roles")) or "未定人物"
    purpose = stringify_plan_value(
        item.get("purpose") or item.get("event") or item.get("summary") or item.get("description")
    )
    return f"{scene} → {location} | {cast} | {purpose}".strip()


def format_dialogue(item: Any, index: int) -> str:
    if not isinstance(item, dict):
        text = stringify_plan_value(item)
        return f"对话{index}：{text}" if text and not text.startswith("对话") else text
    speaker = stringify_plan_value(item.get("speaker") or item.get("from") or item.get("role"))
    line = stringify_plan_value(item.get("line") or item.get("says") or item.get("content"))
    reply = stringify_plan_value(item.get("reply") or item.get("response") or item.get("to"))
    purpose = stringify_plan_value(item.get("purpose") or item.get("effect") or item.get("result"))
    parts = []
    if speaker or line:
        parts.append(f"{speaker}→{line}".strip("→"))
    if reply:
        parts.append(reply)
    if purpose:
        parts.append(purpose)
    return f"对话{index}：" + " | ".join(parts)


def format_event(item: Any, index: int) -> str:
    if not isinstance(item, dict):
        text = stringify_plan_value(item)
        return f"事件{index}：{text}" if text and not text.startswith("事件") else text
    phase = stringify_plan_value(item.get("phase") or item.get("type") or item.get("stage"))
    content = stringify_plan_value(
        item.get("content") or item.get("event") or item.get("summary") or item.get("description")
    )
    label = f"事件{index}"
    if phase:
        label += f"（{phase}）"
    return f"{label}：{content}"


def format_decision(item: Any) -> str:
    if not isinstance(item, dict):
        return stringify_plan_value(item)
    actor = stringify_plan_value(item.get("actor") or item.get("character") or item.get("role"))
    decision = stringify_plan_value(item.get("decision") or item.get("action") or item.get("content"))
    purpose = stringify_plan_value(item.get("purpose") or item.get("result") or item.get("effect"))
    text = f"{actor}→{decision}".strip("→")
    if purpose:
        text += f"→{purpose}"
    return text


def render_list_section(items: Any, formatter: Callable[[Any, int], str]) -> List[str]:
    if not isinstance(items, list):
        text = stringify_plan_value(items)
        return [text] if text else []
    out: List[str] = []
    for index, item in enumerate(items, start=1):
        text = formatter(item, index)
        if text:
            out.append(text)
    return out


def normalize_plan_sections(raw: Any) -> List[tuple[str, List[str]]]:
    """把模型自选的段落声明规范化成 ``[(标题, 条目), ...]``。

    ⚠ E-01b：七段降为**可选契约之一** —— 模型可以在 ``chapter_plan.plan_sections``
    里声明本章自己的段落结构。规范化只做三件事：

    - 标题必须是非空字符串（没给标题 ⇒ 这段作废，不许用字段名顶上）；
    - 条目统一拍平成文本行（字符串 / 列表 / 字典都收）；
    - **整段没有内容 ⇒ 作废**（不渲染空段 —— 空段就是「（待补充）」噪声的来源）。
    """
    if not isinstance(raw, list):
        return []
    sections: List[tuple[str, List[str]]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "").strip()
        if not title:
            continue
        items = _render_plan_section_items(item.get("items"))
        if not items:
            continue
        sections.append((title, items))
    return sections


def _render_plan_section_items(raw: Any) -> List[str]:
    """自选段落的条目：字符串 / 列表 / 字典都拍平成文本行。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if isinstance(raw, list):
        out: List[str] = []
        for item in raw:
            text = stringify_plan_value(item).strip()
            if text:
                out.append(text)
        return out
    text = stringify_plan_value(raw).strip()
    return [text] if text else []


def _section_numeral(index: int) -> str:
    """第 N 段（1-based）的中文序号；超过十段退回阿拉伯数字。"""
    if 1 <= index <= len(_SECTION_NUMERALS):
        return _SECTION_NUMERALS[index - 1]
    return str(index)


def _renumber_sections(sections: List[tuple[str, List[str]]]) -> List[tuple[str, List[str]]]:
    """丢掉空段后重新编号（避免出现「一、二、四、六」这种断号）。"""
    out: List[tuple[str, List[str]]] = []
    for index, (title, lines) in enumerate(sections, start=1):
        numeral = _section_numeral(index)
        out.append((SECTION_NUMBER_PREFIX_RE.sub(numeral + "、", title, count=1), lines))
    return out


def render_chapter_execution_plan(
    chapter_plan: Any,
    *,
    allow_custom_sections: bool = False,
    drop_empty_sections: bool = False,
) -> str:
    """渲染章前执行剧本。

    ⚠ E-01b：结构**可以由 AI 选**。

    - ``allow_custom_sections=True`` 且模型给了 ``chapter_plan.plan_sections``
      ⇒ 按它自选的段落渲染（段落序号由代码重新编号）；
    - 否则走标准七段（**逐位一致**）；
    - ``drop_empty_sections=True`` ⇒ **空段不渲染**（此前空段会渲染成
      「（待补充）」—— 那是代码替模型写的占位符，模型其实没写这一段）。

    两个开关都由 ``chapter_plan_structure.enabled`` 控制（**默认关闭**）。
    """
    if isinstance(chapter_plan, str):
        return chapter_plan.strip()
    if not isinstance(chapter_plan, dict):
        return ""

    if allow_custom_sections:
        custom = normalize_plan_sections(chapter_plan.get("plan_sections"))
        if custom:
            rendered: List[str] = []
            for index, (title, items) in enumerate(custom, start=1):
                rendered.append(f"{_section_numeral(index)}、{title}：")
                rendered.extend(items)
            return "\n".join(rendered).strip()

    opening = stringify_plan_value(
        chapter_plan.get("opening_entry")
        or chapter_plan.get("opening")
        or chapter_plan.get("entry_point")
        or chapter_plan.get("cut_in")
    )
    scenes = render_list_section(
        chapter_plan.get("scene_transitions") or chapter_plan.get("scenes"),
        format_scene_transition,
    )
    dialogues = render_list_section(
        chapter_plan.get("key_dialogues") or chapter_plan.get("dialogues"),
        format_dialogue,
    )
    events = render_list_section(
        chapter_plan.get("event_chain") or chapter_plan.get("events"),
        format_event,
    )
    decisions = render_list_section(
        chapter_plan.get("character_decisions") or chapter_plan.get("decisions"),
        lambda item, _index: format_decision(item),
    )
    payoffs = render_list_section(
        chapter_plan.get("payoff_reversals") or chapter_plan.get("payoffs") or chapter_plan.get("reversals"),
        lambda item, _index: stringify_plan_value(item),
    )
    state = chapter_plan.get("protagonist_state_change") or chapter_plan.get("state_change")
    if isinstance(state, dict):
        state_lines = [
            f"{key}：{text}"
            for key, raw in state.items()
            if (text := stringify_plan_value(raw))
        ]
    else:
        state_text = stringify_plan_value(state)
        state_lines = [state_text] if state_text else []

    # ⚠ E-01a：段标题**不再**在这里写第二份 ——
    # 「哪七段、每段怎么报数」由 `chapter_planning_policy.CHAPTER_EXECUTION_PLAN_SECTIONS`
    # 唯一定义（此前 marker 常量与这里的段落表是两份平行清单）；本模块顶部已导入。

    section_items = (
        [opening] if opening else [],
        scenes,
        dialogues,
        events,
        decisions,
        payoffs,
        state_lines,
    )
    sections = []
    for (marker, unit), lines in zip(CHAPTER_EXECUTION_PLAN_SECTIONS, section_items):
        if drop_empty_sections and not lines:
            continue
        title = f"{marker}（{len(lines)}{unit}）：" if unit else f"{marker}："
        sections.append((title, lines))
    if drop_empty_sections:
        sections = _renumber_sections(sections)
    rendered: list[str] = []
    for title, lines in sections:
        rendered.append(title)
        rendered.extend(lines or ["（待补充）"])
    return "\n".join(rendered).strip()


def has_rendered_plan_content(text: str | None) -> bool:
    """这份剧本里**至少有一段有真内容**（不是只有标题行，也不是占位符）。

    ⚠ E-01b：登记表 E-01 的不变量是「**大纲必须落地**」。
    「落地」不能靠「七段固定」这个形状保证（形状一放开就没了），
    只能靠「渲染结果里确实有内容」+「正文按细纲列出的条目锚定」。
    ⇒ 这里给「生成路径」一道闸：渲染不出内容 ⇒ 不落空细纲。

    ⚠ 「（待补充）」**不算内容** —— 那是代码替模型写的占位符，
    模型其实没写这一段（开启结构开关时空段会被直接丢掉）。
    """
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return False
    for index, line in enumerate(lines):
        if not SECTION_NUMBER_PREFIX_RE.match(line):
            continue
        if index + 1 >= len(lines):
            continue
        nxt = lines[index + 1]
        if SECTION_NUMBER_PREFIX_RE.match(nxt):
            continue
        if nxt.strip() in ("（待补充）", "(待补充)"):
            continue
        return True
    return False


def render_lightweight_act_chapter_outline(row: dict[str, Any]) -> str:
    """Render the slim act-chain row as a readable placeholder before preplanning."""
    try:
        number = int(row.get("number") or 0)
    except (TypeError, ValueError):
        number = 0
    handoff_label = "开篇入口" if number == 1 else "承接上一章"
    lines = [
        f"主事件：{stringify_plan_value(row.get('main_event'))}",
        f"{handoff_label}：{stringify_plan_value(row.get('handoff_from_previous'))}",
        f"交给下一章：{stringify_plan_value(row.get('handoff_to_next'))}",
    ]
    threads = stringify_plan_value(row.get("required_threads"))
    if threads:
        lines.append(f"需推进线索：{threads}")
    location = stringify_plan_value(row.get("location_hint"))
    if location:
        lines.append(f"地点提示：{location}")
    cast = stringify_plan_value(row.get("cast_hint") or row.get("characters"))
    if cast:
        lines.append(f"出场提示：{cast}")
    thrill = stringify_plan_value(row.get("thrill_type") or row.get("thrill_description"))
    if thrill:
        lines.append(f"回报类型：{thrill}")
    foreshadow = stringify_plan_value(row.get("foreshadow_detail") or row.get("foreshadow_action"))
    if foreshadow:
        lines.append(f"伏笔操作：{foreshadow}")
    return "\n".join(line for line in lines if not line.endswith("：")).strip()
