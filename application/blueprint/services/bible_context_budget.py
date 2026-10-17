"""Bible 注入 prompt 的预算与截断策略（B-09）。

## 改之前是什么样

`_format_bible_context` 的默认参数是 `character_limit=5, location_limit=5`，
而且五个调用点并不一致：宏观 prompt 传 5，卷 prompt 传 8。**没有单一事实源。**

更要紧的是 `timeline_notes` 复用了 `location_limit`。实测生产库
`data/plotpilot.db`（2026-10-17）：

| 表 | 实测 |
|---|---|
| `unified_characters` | 5 / 7 / 1 条（按书） |
| `bible_locations` | 10 / 10 条 |
| `bible_timeline_notes` | **2074 / 38 条** |

2074 条时间线事件只注入 5 条 —— **99.76% 被静默丢弃**。而且 `sort_order`
全部为 0，`[:5]` 取到的是查询返回顺序的前 5 条，是**任意切片**，不是「最重要的 5 条」。

（另注：`_get_bible_context` 里的角色来自 `unified_characters`
—— `sqlite_bible_repository` 明确声明那是角色的事实源；`bible_characters`
是一张 0 行的遗留表。所以角色上限的实际影响比登记表估的小，
但时间线那一项比登记表估的大得多。）

## 错在哪

**上限本身是对的** —— prompt 要有 token 上限，「让 AI 决定注入多少」不是选项。
错的是砍完之后**装作那就是全部**：模型拿着 5 条时间线事件去规划整本书结构，
却没有任何提示说还有 2069 条没给它。它会以为世界线就长这样。

所以本模块做两件事：

1. **按字符预算收口，而不是按条数盲切。** 条数上限只是兜底，字符预算才是
   真正的约束 —— 一条 5 字的地点和一条 500 字的世界观占的 prompt 完全不同。
2. **砍掉就要说。** 每有内容因预算被丢弃，就在对应区块末尾追加一行
   「另有 N 条未注入（受注入预算限制）」。模型于是能区分「只有 5 条」
   和「有 2074 条但我只拿到 5 条」，这两者的正确反应完全不同。

这也是本批一贯的三态口径：降级路径必须留痕，不能把截断伪装成事实。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class BibleContextBudget:
    """一次 Bible 注入的预算。

    字段语义：
      - `max_chars`：注入文本的字符预算，**这是唯一的硬约束**；其余是各区块
        的条数上限（防止某一类条目独占预算）。任一为 ``None`` 表示不限条数，
        只受 `max_chars` 约束。
      - `timeline_limit` 单列，因为时间线是唯一实测会突破千条量级的区块，
        值得一个独立旋钮。
    """

    max_chars: int = 24_000
    character_limit: Optional[int] = None
    relationship_limit: Optional[int] = None
    location_limit: Optional[int] = None
    world_setting_limit: Optional[int] = None
    timeline_limit: int = 120


#: 宏观规划：一次规划整本书，预算给得最足。
MACRO_PLANNING = BibleContextBudget()

#: 卷级规划：上下文刻意收窄以提高字段完整度（见 `_build_precise_volume_prompt`
#: 的 docstring），但不再靠「砍到 5 条」这种看不出意图的数字。
VOLUME_PLANNING = BibleContextBudget(max_chars=16_000, timeline_limit=60)

#: 场景 / 章节级：更窄。
SCENE_PLANNING = BibleContextBudget(max_chars=8_000, timeline_limit=30)


def _take(
    items: List[Dict[str, Any]],
    *,
    limit: Optional[int],
    budget: "_CharBudget",
) -> Tuple[List[Dict[str, Any]], int]:
    """在条数上限与字符预算内取条目，返回 (取到的, 丢掉的条数)。

    顺序**保持调用方传入的顺序**。这里不做「按重要性排序」——
    `bible_timeline_notes` 实测 `sort_order` 全为 0，没有可信的优先级信号，
    按某个臆造的权重重排只会把任意性藏得更深。把「丢了多少」如实报出去，
    比假装排过序更诚实。
    """
    pool = items[:limit] if limit is not None else list(items)
    kept: List[Dict[str, Any]] = []
    for item in pool:
        cost = _render_cost(item)
        if kept and not budget.can_afford(cost):
            break
        kept.append(item)
        budget.spend(cost)
    return kept, len(items) - len(kept)


class _CharBudget:
    """字符预算计数器。"""

    __slots__ = ("_left",)

    def __init__(self, max_chars: int) -> None:
        self._left = max(int(max_chars), 0)

    def can_afford(self, cost: int) -> bool:
        return cost <= self._left

    def spend(self, cost: int) -> None:
        self._left -= cost


def _render_cost(item: Dict[str, Any]) -> int:
    """条目注入后的估算字符成本（字段值长度 + 固定开销）。

    刻意保守：宁可少注入几条，也不要让实际 prompt 撑爆预算。
    """
    total = 16  # "- 名称: " 之类的固定开销
    for key in ("name", "description", "event", "time_point", "impact",
                "role", "character_arc", "significance", "relationship_type",
                "character1", "character2"):
        value = item.get(key)
        if value:
            total += len(str(value))
    return total


def _dropped_note(block_label: str, dropped: int) -> str:
    return f"（另有 {dropped} 条{block_label}未注入：受注入预算限制，非本作品没有）"


def format_bible_context(
    bible_context: Dict[str, Any],
    *,
    budget: BibleContextBudget = MACRO_PLANNING,
    include_ids: bool = False,
    empty_hint: str = (
        "暂无详细设定，请优先依据作者梗概与题材赛道生成结构；"
        "只补全必要因果，不套入无关热门题材。"
    ),
) -> str:
    """把 Bible 结构化数据转为可注入 CPMS 的上下文块（预算感知 + 截断留痕）。

    ⚠️ **区块顺序就是优先级顺序。** 共享同一个字符预算，先处理的区块先花钱，
    后面的区块会被前面的饿死。实测遇到过：300 条长描述角色会把 24000 字符
    预算吃光，导致 2000 条时间线事件只挤进 1 条。

    所以这里的顺序是刻意排的 —— 世界观（体量固定、必读）→ 地点（体量小、
    规划整本书必需）→ 时间线（体量大但决定因果链）→ 角色 → 关系。
    改顺序等于改优先级，`test_block_order_is_the_documented_priority`
    会拦住没配套改测试的改动。
    """
    blocks: List[str] = []
    chars = _CharBudget(budget.max_chars)

    worldview = bible_context.get("worldview") or ""
    if worldview:
        chars.spend(len(str(worldview)))
        blocks.append(f"【世界观】\n{worldview}")

    locations = bible_context.get("locations") or []
    if locations:
        kept, dropped = _take(locations, limit=budget.location_limit, budget=chars)
        lines = ["【关键地点】"]
        for loc in kept:
            name = loc.get("name", "Unknown")
            desc = loc.get("description", "")
            significance = loc.get("significance", "")
            loc_id = f" (ID: {loc.get('id', 'N/A')})" if include_ids else ""
            lines.append(f"- {name}{loc_id}: {desc}")
            if significance:
                lines.append(f"  叙事意义：{significance}")
        if dropped:
            lines.append(_dropped_note("关键地点", dropped))
        blocks.append("\n".join(lines))

    timeline_notes = bible_context.get("timeline_notes") or []
    if timeline_notes:
        kept, dropped = _take(
            timeline_notes, limit=budget.timeline_limit, budget=chars
        )
        lines = ["【时间线事件】"]
        for note in kept:
            event = note.get("event", "")
            desc = note.get("description", "")
            time_point = note.get("time_point", "")
            impact = note.get("impact", "")
            head = f"{event}"
            if time_point:
                head = f"{head}（{time_point}）"
            lines.append(f"- {head}: {desc}")
            if impact:
                lines.append(f"  情节影响：{impact}")
        if dropped:
            lines.append(_dropped_note("时间线事件", dropped))
        blocks.append("\n".join(lines))

    world_settings = bible_context.get("world_settings") or []
    if world_settings:
        kept, dropped = _take(
            world_settings, limit=budget.world_setting_limit, budget=chars
        )
        lines = ["【世界设定】"]
        for item in kept:
            name = item.get("name", "未命名设定")
            desc = item.get("description", "")
            item_id = f" (ID: {item.get('id', 'N/A')})" if include_ids else ""
            lines.append(f"- {name}{item_id}: {desc}")
        if dropped:
            lines.append(_dropped_note("世界设定", dropped))
        blocks.append("\n".join(lines))

    characters = bible_context.get("characters") or []
    if characters:
        kept, dropped = _take(
            characters, limit=budget.character_limit, budget=chars
        )
        lines = ["【角色设定】"]
        for char in kept:
            name = char.get("name", "Unknown")
            desc = char.get("description", "")
            role = char.get("role", "")
            arc = char.get("character_arc", "")
            char_id = f" (ID: {char.get('id', 'N/A')})" if include_ids else ""
            role_text = f" [{role}]" if role else ""
            lines.append(f"- {name}{char_id}{role_text}: {desc}")
            if arc:
                lines.append(f"  人物弧光：{arc}")
        if dropped:
            lines.append(_dropped_note("角色设定", dropped))
        blocks.append("\n".join(lines))

    relationships = bible_context.get("relationships") or []
    if relationships:
        kept, dropped = _take(
            relationships, limit=budget.relationship_limit, budget=chars
        )
        lines = ["【角色关系】"]
        for rel in kept:
            char1 = rel.get("character1", "")
            char2 = rel.get("character2", "")
            rel_type = rel.get("relationship_type", "")
            rel_desc = rel.get("description", "")
            lines.append(f"- {char1} ↔ {char2} ({rel_type}): {rel_desc}")
        if dropped:
            lines.append(_dropped_note("角色关系", dropped))
        blocks.append("\n".join(lines))

    if blocks:
        return "\n\n".join(blocks)
    return f"【世界观与人物】\n{empty_hint}"


def format_locations_context(
    bible_context: Dict[str, Any], *, budget: BibleContextBudget = MACRO_PLANNING
) -> str:
    """地点清单（供 quick 规划的 `locations.list` 变量）。"""
    locations = bible_context.get("locations") or []
    if not locations:
        return ""
    chars = _CharBudget(budget.max_chars)
    kept, dropped = _take(locations, limit=budget.location_limit, budget=chars)
    lines: List[str] = []
    for loc in kept:
        name = loc.get("name", "Unknown")
        desc = loc.get("description", "")
        significance = loc.get("significance", "")
        line = f"- {name}: {desc}".strip()
        if significance:
            line = f"{line}\n  叙事意义：{significance}"
        lines.append(line)
    if dropped:
        lines.append(_dropped_note("关键地点", dropped))
    return "\n".join(lines)


def format_worldbuilding_context(
    bible_context: Dict[str, Any], *, budget: BibleContextBudget = MACRO_PLANNING
) -> str:
    """世界观设定 + 时间线（供 quick 规划的 `worldbuilding.content` 变量）。"""
    blocks: List[str] = []
    chars = _CharBudget(budget.max_chars)

    worldview = bible_context.get("worldview") or ""
    if worldview:
        chars.spend(len(str(worldview)))
        blocks.append(str(worldview))

    world_settings = bible_context.get("world_settings") or []
    if world_settings:
        kept, dropped = _take(
            world_settings, limit=budget.world_setting_limit, budget=chars
        )
        lines = []
        for item in kept:
            name = item.get("name", "未命名设定")
            desc = item.get("description", "")
            lines.append(f"- {name}: {desc}")
        if dropped:
            lines.append(_dropped_note("世界设定", dropped))
        blocks.append("\n".join(lines))

    timeline_notes = bible_context.get("timeline_notes") or []
    if timeline_notes:
        kept, dropped = _take(
            timeline_notes, limit=budget.timeline_limit, budget=chars
        )
        lines = []
        for note in kept:
            event = note.get("event", "")
            desc = note.get("description", "")
            lines.append(f"- {event}: {desc}")
        if dropped:
            lines.append(_dropped_note("时间线事件", dropped))
        blocks.append("\n".join(lines))

    return "\n\n".join(block for block in blocks if block.strip())
