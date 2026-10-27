"""叙事契约文本：向导与 Bible 中「长期约束」的统一格式化。

供 ContextBudgetAllocator、DAG ctx_blueprint 等复用，避免各处重复拼接。"""
from __future__ import annotations

from typing import Dict, List, Optional

from domain.bible.entities.bible import Bible
from domain.worldbuilding.worldbuilding import Worldbuilding
from application.world.worldbuilding_merge import (
    WORLD_BUILDING_DIMENSION_KEYS,
    WORLD_BUILDING_FIELD_KEYS_BY_DIMENSION,
    worldbuilding_slices_nonempty,
)
from application.world.worldbuilding_schema import WORLDBUILDING_DIMENSION_DEFS


_DIM_DISPLAY: Dict[str, str] = {
    dim_key: str(dim_cfg.get("label") or dim_key)
    for dim_key, dim_cfg in WORLDBUILDING_DIMENSION_DEFS.items()
}

_FIELD_LABELS: Dict[str, str] = {}
for _dim in WORLDBUILDING_DIMENSION_DEFS.values():
    for _key, _label in (_dim.get("fields") or {}).items():
        _FIELD_LABELS.setdefault(_key, str(_label).split("（", 1)[0])


#: 世界观维度的**展示顺序**（第 26 批）—— 按**硬度**排，不按篇幅排。
#:
#: ⚠ 为什么（生产实测，第 26 批）：
#:   创作契约块真实长度 **4379 字**，而正文侧每块上限只有 **2000 字**
#:   ⇒ **一半以上装不下** ⇒ 刀口必然落在块中间。
#:   旧顺序（core_rules → geography → society → culture → daily_life）：
#:   活下来的是「核心法则 + 地理生态大半」⇒ 而**禁忌 / 历史 / 政治 / 等级 / 行话
#:   全丢** ⇒ 模型压根不知道「三更闻唤莫应」「戏票不能捡」这类**硬规则**。
#:   ⇒ 违反即穿帮，且**它连规则是什么都不知道**。
#:
#: ⇒ 改成按硬度排：core_rules（力量体系/物理/技术）→ culture（禁忌/历史/宗教）
#:   → society（政治/等级/经济）→ geography（地形/气候/资源/生态）→ daily_life。
#:   实测：禁忌 / 历史 / 政治 **从"丢"变成"在"**。
#:   代价：地形 / 气候 / 资源掉出去 —— 可接受，因为**地理信息在地点清单里还有一份**
#:   （``bible_locations`` 的描述同样写着地形），而禁忌**只在这里有**。
#:
#: ⚠ 契约（``WORLD_BUILDING_DIMENSION_KEYS``）**不动** ⇒ 前端 / 提示词 schema 不受影响；
#:   这里只是**呈现顺序**。契约里新增的维度/字段会自动追加到**末尾**（不丢）。
WORLDBUILDING_DIMENSION_DISPLAY_ORDER: tuple = (
    "core_rules",
    "culture",
    "society",
    "geography",
    "daily_life",
)

#: 维度**内部**的字段展示顺序（同样按硬度）—— 维度被切一半时，先保住靠前的字段。
WORLDBUILDING_FIELD_DISPLAY_ORDER: Dict[str, tuple] = {
    "core_rules": ("power_system", "physics_rules", "magic_tech"),
    "culture": ("taboos", "history", "religion"),
    "society": ("politics", "class_system", "economy"),
    "geography": ("terrain", "climate", "resources", "ecology"),
    "daily_life": ("language_slang", "food_clothing", "entertainment"),
}

#: 字段的**显示分组**调整（第 26 批）—— 只改**呈现**，不动契约与数据。
#:
#: ⚠ 为什么：``religion``（宗教信仰体系：城隍出巡、梨园供老郎神、走阴问亡）
#:   语义上属于**民俗/生活质感**（沉浸感），而它原先挂在「历史文化」维里
#:   ⇒ 那一维**整体**排在「社会结构」前面 ⇒ 预算刚好差 ~160 字，
#:   「政治体制与权力架构」（探员能不能跨界办案、官府不受理邪祟案……）
#:   就**整行被切掉** ⇒ 而它**比宗教更容易穿帮**（第 21 批 A1 抓到的
#:   「郑铁珊被无解释赋予写票能力」就是**权力边界**问题）。
#: ⇒ 把 ``religion`` 挪到「沉浸感细节」组（语义也说得通）
#:   ⇒ 它跟着最软的一组走 ⇒ 预算紧时**第一个被牺牲** ⇒ 换「政治体制」进来。
#:
#: ⚠ 只影响**正文提示词里的呈现顺序与分组**；契约 / 前端 / 数据映射一律不动。
WORLDBUILDING_FIELD_DIMENSION_OVERRIDE: Dict[str, str] = {
    "religion": "daily_life",
}

#: 创作契约槽位**声明的分节标题**（第 26 批）—— 截断时**点名丢了哪几维**。
#:
#: ⚠ 为什么必须声明：此前这个槽位**没声明** section_headers ⇒ 截断后只有一句
#:   「仅是部分内容」⇒ 模型不知道丢的是**哪一维**
#:   ⇒ 它会以为「这个世界没有禁忌/没有历史」⇒ **静默失效**。
#: ⇒ 声明之后，截断机制会点名「社会结构 / 地理生态 / 沉浸感细节」（第 24 批那套）。
WORLDBUILDING_DIMENSION_SECTION_HEADERS: tuple = tuple(
    f"▸ {_DIM_DISPLAY.get(dim_key, dim_key)}"
    for dim_key in WORLDBUILDING_DIMENSION_DISPLAY_ORDER
    if dim_key in _DIM_DISPLAY
)


def _dimension_display_order() -> tuple:
    """维度展示顺序 = 硬度顺序 + 契约里**没列**的维度（按契约顺序追加在末尾）。"""
    declared = [d for d in WORLDBUILDING_DIMENSION_DISPLAY_ORDER if d in _DIM_DISPLAY]
    rest = [d for d in WORLD_BUILDING_DIMENSION_KEYS if d not in declared]
    return tuple([*declared, *rest])


def _field_display_order(dim_key: str) -> tuple:
    """字段展示顺序 = 硬度顺序 + 契约里**没列**的字段（按契约顺序追加在末尾）。"""
    contract_fields = tuple(WORLD_BUILDING_FIELD_KEYS_BY_DIMENSION.get(dim_key, ()))
    declared = [
        f for f in WORLDBUILDING_FIELD_DISPLAY_ORDER.get(dim_key, ()) if f in contract_fields
    ]
    rest = [f for f in contract_fields if f not in declared]
    return tuple([*declared, *rest])


def _grouped_fields(slices: Dict[str, Dict[str, str]]) -> List[tuple]:
    """按**展示分组**把字段排好 ⇒ ``[(组维度, 字段键, 文本), ...]``。

    ⚠ 跨维调整（:data:`WORLDBUILDING_FIELD_DIMENSION_OVERRIDE`）在这里生效：
    被改组的字段**只在目标组**出现（原组不再出现）⇒ 不会重复。
    """
    grouped: List[tuple] = []
    for dim in _dimension_display_order():
        blk = slices.get(dim) or {}
        for key in _field_display_order(dim):
            value = str(blk.get(key) or "").strip()
            if not value:
                continue
            target = WORLDBUILDING_FIELD_DIMENSION_OVERRIDE.get(key)
            if target is not None and target != dim:
                continue  # 在目标组里渲染
            grouped.append((dim, key, value))
        # 被改到本组的字段（按它们的硬度位置插进来）
        for other_dim in _dimension_display_order():
            for key in _field_display_order(other_dim):
                if WORLDBUILDING_FIELD_DIMENSION_OVERRIDE.get(key) != dim:
                    continue
                value = str((slices.get(other_dim) or {}).get(key) or "").strip()
                if not value:
                    continue
                grouped.append((dim, key, value))
    return grouped


def format_worldbuilding_slices_for_prompt(
    slices: Optional[Dict[str, Dict[str, str]]],
) -> str:
    """合并后的五维 dict → writer 基础字段顺序的紧凑正文。

    ⚠ 顺序按**硬度**（第 26 批）—— 见 :data:`WORLDBUILDING_DIMENSION_DISPLAY_ORDER`。
    """
    if not slices or not worldbuilding_slices_nonempty(slices):
        return ""

    lines: List[str] = ["【世界观五维（作者确认）】"]
    current_dim = None
    for dim, key, value in _grouped_fields(slices):
        if dim != current_dim:
            lines.append(f"▸ {_DIM_DISPLAY.get(dim, dim)}")
            current_dim = dim
        lines.append(f"- {_FIELD_LABELS.get(key, key)}：{value}")

    if len(lines) <= 1:
        return ""
    return "\n".join(lines)


def build_worldbuilding_prompt_fields(
    *,
    bible: Optional[Bible] = None,
    worldbuilding: Optional[Worldbuilding] = None,
    worldbuilding_slices: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, str]:
    """将世界观切片展开为全量块与独立维度字段。"""
    if worldbuilding_slices is None:
        from application.world.services.narrative_contract_loader import load_merged_worldbuilding_slices

        worldbuilding_slices = load_merged_worldbuilding_slices(
            bible=bible,
            worldbuilding=worldbuilding,
        )

    full_text = format_worldbuilding_slices_for_prompt(worldbuilding_slices)
    fields: Dict[str, str] = {
        "worldbuilding_full": full_text,
    }
    for dim in WORLD_BUILDING_DIMENSION_KEYS:
        fields[dim] = format_worldbuilding_slices_for_prompt({dim: (worldbuilding_slices or {}).get(dim) or {}})
    return fields


def format_worldbuilding_for_prompt(wb: Optional[Worldbuilding]) -> str:
    """将 worldbuilding 表实体转为紧凑正文（仅非空字段）。"""
    if wb is None:
        return ""

    lines: List[str] = ["【世界观五维（作者确认）】"]
    empty = True
    for dim in WORLD_BUILDING_DIMENSION_KEYS:
        block: List[str] = []
        for attr in WORLD_BUILDING_FIELD_KEYS_BY_DIMENSION.get(dim, ()):
            val = (getattr(wb, attr, None) or "").strip()
            if val:
                block.append(f"- {_FIELD_LABELS.get(attr, attr)}：{val}")
        if block:
            lines.append(f"▸ {_DIM_DISPLAY.get(dim, dim)}")
            lines.extend(block)
            empty = False

    if empty:
        return ""
    return "\n".join(lines)


def format_style_notes_for_prompt(bible: Optional[Bible]) -> str:
    """Bible 文风/惯例类 style_notes。"""
    if bible is None:
        return ""
    notes = getattr(bible, "style_notes", None) or []
    if not notes:
        return ""

    lines: List[str] = ["【文风与叙述公约】"]
    for sn in notes:
        cat = (getattr(sn, "category", None) or "").strip()
        content = (getattr(sn, "content", None) or "").strip()
        if not content:
            continue
        if cat:
            lines.append(f"- [{cat}] {content}")
        else:
            lines.append(f"- {content}")

    if len(lines) <= 1:
        return ""
    return "\n".join(lines)


def format_world_setting_rules_for_prompt(bible: Optional[Bible]) -> str:
    """Bible 中 setting_type=rule 的条目（补充五维表之外的硬规则）。"""
    if bible is None:
        return ""
    settings = getattr(bible, "world_settings", None) or []
    rules = [s for s in settings if getattr(s, "setting_type", "") == "rule"]
    if not rules:
        return ""

    lines: List[str] = ["【世界规则条目（Bible）】"]
    for s in rules:
        name = (getattr(s, "name", None) or "").strip()
        desc = (getattr(s, "description", None) or "").strip()
        if not name and not desc:
            continue
        if name and desc:
            lines.append(f"- {name}：{desc}")
        elif name:
            lines.append(f"- {name}")
        else:
            lines.append(f"- {desc}")

    if len(lines) <= 1:
        return ""
    return "\n".join(lines)


def build_narrative_contract_block(
    *,
    bible: Optional[Bible],
    worldbuilding: Optional[Worldbuilding] = None,
    worldbuilding_slices: Optional[Dict[str, Dict[str, str]]] = None,
) -> str:
    """合并：文风公约 → 五维世界观 → Bible 规则条目。空段自动省略。"""
    parts: List[str] = []
    style = format_style_notes_for_prompt(bible)
    if style:
        parts.append(style)
    if worldbuilding_slices is not None:
        wb_text = format_worldbuilding_slices_for_prompt(worldbuilding_slices)
    else:
        wb_text = format_worldbuilding_for_prompt(worldbuilding)
    if wb_text:
        parts.append(wb_text)
    rules = format_world_setting_rules_for_prompt(bible)
    if rules:
        parts.append(rules)

    if not parts:
        return ""
    return "\n\n".join(parts)


def build_ctx_blueprint_outputs(
    *,
    bible: Optional[Bible],
    worldbuilding: Optional[Worldbuilding] = None,
    worldbuilding_slices: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, str]:
    """ctx_blueprint 节点三路输出：规则摘要 / 禁忌 / 氛围感。"""
    if worldbuilding_slices is None and (bible is not None or worldbuilding is not None):
        from application.world.services.narrative_contract_loader import load_merged_worldbuilding_slices

        worldbuilding_slices = load_merged_worldbuilding_slices(
            bible=bible, worldbuilding=worldbuilding
        )

    world_rules = ""
    if bible:
        world_rules = format_world_setting_rules_for_prompt(bible)
    wb_for_rules = format_worldbuilding_slices_for_prompt(worldbuilding_slices)
    if not wb_for_rules:
        wb_for_rules = format_worldbuilding_for_prompt(worldbuilding)
    if wb_for_rules:
        world_rules = f"{wb_for_rules}\n\n{world_rules}".strip() if world_rules else wb_for_rules

    taboos = ""
    culture = (worldbuilding_slices or {}).get("culture") or {}
    t = (culture.get("taboos") or "").strip()
    if not t and worldbuilding is not None:
        t = (worldbuilding.taboos or "").strip()
    if t:
        taboos = f"【文化禁忌】\n{t}"

    atmosphere = format_style_notes_for_prompt(bible)

    return {
        "world_rules": world_rules,
        "taboos": taboos,
        "atmosphere": atmosphere,
    }
