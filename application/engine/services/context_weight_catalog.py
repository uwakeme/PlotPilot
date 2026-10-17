"""D-02：槽位目录 —— 告诉 LLM「有哪些槽位、各管什么」，同时供代码校验键名。

**单一事实源**：prompt 里的槽位清单由本目录生成，代码侧也用它过滤 LLM 返回的键。
这样「AI 报了个不存在的槽位名」不会静默生效。

⚠️ 键必须是 `ContextBudgetAllocator` 里的 **dict key**（``vector_recall``），
**不是** ``ContextSlot.name``（``向量召回``）。两者在真实槽位里从不相同，
且 name 带括号后缀（``绝对事实边界(FACT_LOCK)``），不适合当 JSON 键。
`allocate_tier` 的排序与封顶都按 dict key 查权重，这里必须与之一致。

27 个槽位全部来自对 `context_budget_allocator.py` 的 ast 提取，不是手写猜测。
"""
from __future__ import annotations

from typing import Dict, NamedTuple


class SlotSpec(NamedTuple):
    key: str          # 权重表的键（= slots[...] 的 dict key）
    label: str        # ContextSlot.name，中文展示名
    purpose: str      # 这个槽位装什么
    tier: str         # 所属预算层，仅用于 prompt 说明


#: 与 `ContextBudgetAllocator` 里注册的槽位一一对应（27 个）。
#: 新增/删除槽位时**必须**同步这里，否则 prompt 会教 AI 报不存在的键。
SLOT_CATALOG: Dict[str, SlotSpec] = {
    "lifecycle_directive": SlotSpec(
        "lifecycle_directive", "生命周期行为准则", "沙漏阶段给出的收束/铺开指令", "T0"),
    "story_anchor": SlotSpec(
        "story_anchor", "全书主线锚点", "不可动摇的主线承诺", "T0"),
    "narrative_promise": SlotSpec(
        "narrative_promise", "叙事承诺锁", "作者对读者的题材承诺", "T0"),
    "narrative_contract": SlotSpec(
        "narrative_contract", "创作契约", "题材约定与文风公约", "T0"),
    "fact_lock": SlotSpec(
        "fact_lock", "绝对事实边界", "已确立且不可改写的事实", "T0"),
    "character_anchors": SlotSpec(
        "character_anchors", "角色锚点", "本章出场角色的性格/状态硬约束", "T0"),
    "context_brief": SlotSpec(
        "context_brief", "编辑手记", "用自然语言写的降级内容索引", "T0"),
    "evolution_presenter": SlotSpec(
        "evolution_presenter", "叙事状态锁", "进化阶段锁定的叙事状态", "T0"),
    "current_act_summary": SlotSpec(
        "current_act_summary", "当前幕摘要", "正在写的这一幕的摘要", "T0"),
    "scars_and_motivations": SlotSpec(
        "scars_and_motivations", "角色伤疤与执念", "角色的创伤与动机", "T1"),
    "completed_beats": SlotSpec(
        "completed_beats", "已完成节拍", "本章已经写完的节拍", "T1"),
    "debt_due": SlotSpec(
        "debt_due", "叙事备忘", "到期的伏笔/承诺提醒", "T1"),
    "revealed_clues": SlotSpec(
        "revealed_clues", "已揭露线索", "读者已知的信息（避免重复揭示）", "T1"),
    "pending_foreshadowings": SlotSpec(
        "pending_foreshadowings", "待回收伏笔", "已种下待回收的伏笔", "T1"),
    "anti_ai_protocol": SlotSpec(
        "anti_ai_protocol", "Anti-AI 行为协议", "反套话/反模板的写作约束", "T1"),
    "graph_subnetwork": SlotSpec(
        "graph_subnetwork", "图谱子网", "与本章相关的知识图谱切片", "T1"),
    "causal_chains": SlotSpec(
        "causal_chains", "未闭环因果链", "已铺开但未回收的因果", "T1"),
    "diagnosis_breakpoints": SlotSpec(
        "diagnosis_breakpoints", "人设冲突提醒", "宏观诊断指出的人设风险", "T1"),
    "recent_act_summaries": SlotSpec(
        "recent_act_summaries", "近期幕摘要", "前几幕的摘要", "T1"),
    "character_state_lock": SlotSpec(
        "character_state_lock", "角色状态锁", "角色跨章的状态变化", "T1"),
    "deferred_foreshadowings": SlotSpec(
        "deferred_foreshadowings", "冗余伏笔参考", "降级用的伏笔参考", "T1"),
    "worldbuilding_core": SlotSpec(
        "worldbuilding_core", "世界核心规则", "世界观的核心设定", "T1"),
    "immersion_details": SlotSpec(
        "immersion_details", "世界沉浸感细节", "五维沉浸感描述", "T1"),
    "key_props": SlotSpec(
        "key_props", "本章关键道具", "标记为关键的道具状态", "T1"),
    "storyline_context": SlotSpec(
        "storyline_context", "故事线上下文", "各条故事线的当前进度", "T1"),
    "recent_chapters": SlotSpec(
        "recent_chapters", "最近章节", "上章全文/半章/更早章首预览", "T2"),
    "vector_recall": SlotSpec(
        "vector_recall", "向量召回", "语义检索召回的历史片段", "T3"),
}


def catalog_lines() -> str:
    """给 prompt 用的槽位清单。"""
    return "\n".join(
        f"- {spec.key}（{spec.tier}，{spec.label}）：{spec.purpose}"
        for spec in SLOT_CATALOG.values()
    )


def sanitize_weights(raw: object) -> Dict[str, float]:
    """只保留目录内存在、且数值合法的键。

    AI 报未知槽位名 → 丢弃（而不是静默生效）。
    AI 报非数值 / 越界 → 钳到 [0,1]。
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, float] = {}
    for key, value in raw.items():
        if key not in SLOT_CATALOG:
            continue
        try:
            weight = float(value)
        except (TypeError, ValueError):
            continue
        out[key] = max(0.0, min(1.0, weight))
    return out
