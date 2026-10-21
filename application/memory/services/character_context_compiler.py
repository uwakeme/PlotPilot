"""角色 projection → T0/T1/T2 提示锁。

D-04：改造前这段是**代码按固定条数切事实**（``scars[:2]`` / ``motivations[:2]`` /
voice ``[:3]`` / catchphrases ``[:3]`` / 知识边界 ``[:3]`` / 关系 ``[:2]``），
且 T2 措辞对所有角色一字不差。「这一章该给模型看这个角色的哪几条记忆」是
**创作判断**，被写成了常量 —— 登记表 D-04 的判定是 C。

现在两条路径**共用同一段渲染代码**，只有「选哪些」不同：

- 兜底（``selection=None``）：按固定条数切 —— 改造前的行为，**逐位不变**；
- AI（``selection={...}``）：按 AI 挑中的候选 id 渲染。

候选由代码产出（``build_candidates``），AI **只挑不新增** —— 候选 id 不在清单里
的一律丢弃，AI 不得凭空写出角色没有的记忆。

**本模块保持章号无关**：编译结果会被烤进 projection 并缓存，按章裁剪只能落在
持有 ``chapter_number`` 的渲染方（``context_budget_allocator._projection_locks_for_plan``）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

#: 事实类别的固定规格：id 前缀 / 展示标签 / 渲染模式 / 所在层 / 兜底条数 / 组内连接符
#:
#: **单一事实源**：类别、前缀、每类条数上限都只此一份。
#: 取材侧的 schema（``character_context_selection_schema``）原先又抄了一份
#: 类别与前缀（复审 §3.2 的第 5 处「同一逻辑写两份」）⇒ 现在它 import 本表。
CATEGORY_SPEC: Dict[str, Dict[str, Any]] = {
    "scars": {"prefix": "s", "label": "创伤", "mode": "item", "tier": "t0", "limit": 2, "sep": ""},
    "motivations": {"prefix": "m", "label": "驱动力", "mode": "item", "tier": "t0", "limit": 2, "sep": ""},
    "voice": {"prefix": "v", "label": "声线", "mode": "group", "tier": "t0", "limit": 3, "sep": " / "},
    "catchphrases": {"prefix": "c", "label": "口癖", "mode": "group", "tier": "t0", "limit": 3, "sep": "、"},
    "knowledge": {"prefix": "k", "label": "知识边界", "mode": "group", "tier": "t0", "limit": 3, "sep": "；"},
    "relationships": {"prefix": "r", "label": "关系张力", "mode": "group", "tier": "t1", "limit": 2, "sep": "；"},
}


@dataclass(frozen=True)
class FactCandidate:
    """一条可被 AI 挑选的事实候选。"""

    id: str
    category: str
    label: str
    tier: str
    mode: str
    value: str
    text: str
    sep: str
    limit: int
    visible: bool = True

    @property
    def display(self) -> str:
        """给 AI 看的描述。"""
        return self.text if self.mode == "item" else f"{self.label}:{self.value}"


def default_t2_instruction(name: str) -> str:
    """角色 T2 措辞的**唯一**默认实现。

    改造前有两份并行实现（``character_context_compiler`` 与
    ``character_narrative_kernel``）各写一份、措辞逐字相同 —— 改一处漏一处。
    现在只有这一份。
    """
    return f"- {name}：允许过场/提及，禁止抢走主线焦点"


class CharacterContextCompiler:
    """Compile character projection data into T0/T1/T2 prompt locks."""

    # ── 候选清单 ──────────────────────────────────────────────────

    def build_candidates(self, projection: Dict[str, Any]) -> List[FactCandidate]:
        """列出该角色可被挑选的事实候选（稳定 id、确定顺序）。

        创伤 / 驱动力 的候选**包含内容为空的那几条** —— 改造前的切片是
        「先切前 N 条、再跳过空内容」，空条目会占掉名额。候选清单要能复现
        同样的兜底结果，就得保留这些空条目；但它们对 AI 没有意义，
        因此 ``visible=False``（不出现在给 AI 的清单里）。
        """
        constitution = projection.get("constitution") or {}
        voice = projection.get("voice_fingerprint") or {}
        knowledge = projection.get("knowledge_boundary") or {}
        scars = projection.get("active_scars") or []
        motivations = projection.get("active_motivations") or []
        rels = projection.get("relationships") or []

        out: List[FactCandidate] = []

        def _add(category: str, value: str, text: str, visible: bool) -> None:
            spec = CATEGORY_SPEC[category]
            index = sum(1 for c in out if c.category == category)
            out.append(
                FactCandidate(
                    id=f"{spec['prefix']}{index}",
                    category=category,
                    label=spec["label"],
                    tier=spec["tier"],
                    mode=spec["mode"],
                    value=value,
                    text=text,
                    sep=spec["sep"],
                    limit=spec["limit"],
                    visible=visible,
                )
            )

        for scar in scars:
            impact = scar.get("impact") or scar.get("description") or ""
            source = scar.get("source_event") or ""
            _add(
                "scars",
                f"{source}->{impact}",
                f"创伤:{source}->{impact}".strip("->"),
                bool(impact or source),
            )

        for motivation in motivations:
            desc = motivation.get("description") or ""
            _add("motivations", desc, f"驱动力:{desc}", bool(desc))

        for key in ("style", "sentence_pattern", "speech_tempo"):
            if voice.get(key):
                _add("voice", str(voice.get(key)), str(voice.get(key)), True)

        for phrase in (voice.get("catchphrases") or []):
            _add("catchphrases", str(phrase), str(phrase), True)

        for item in (knowledge.get("unknown") or []):
            _add("knowledge", str(item), str(item), True)

        for rel in rels:
            value = str(rel.get("description") or rel.get("text") or "")
            _add("relationships", value, value, bool(value))

        return out

    # ── 渲染 ──────────────────────────────────────────────────────

    def compile(
        self,
        projection: Dict[str, Any],
        selection: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """渲染 T0/T1/T2 提示锁。

        ``selection=None`` ⇒ 兜底路径（固定切片，与改造前**逐位相同**）。
        ``selection={"fact_ids": [...], "t2": "..."}`` ⇒ AI 路径。
        """
        name = str(projection.get("name") or projection.get("entity_id") or "")
        constitution = projection.get("constitution") or {}
        current_state = projection.get("current_state") or {}
        hidden = str(constitution.get("hidden_profile") or "")

        candidates = self.build_candidates(projection)
        if selection is None:
            chosen = set(self._fallback_ids(candidates))
            t2 = default_t2_instruction(name)
        else:
            chosen = set(str(x) for x in (selection.get("fact_ids") or []))
            ai_t2 = str(selection.get("t2") or "").strip()
            t2 = f"- {name}：{ai_t2}" if ai_t2 else default_t2_instruction(name)

        t0_parts: List[str] = [f"- {name}"]
        if constitution.get("public_profile"):
            t0_parts.append(f"公开面:{constitution['public_profile']}")
        if constitution.get("core_belief"):
            t0_parts.append(f"核心信念:{constitution['core_belief']}")
        for taboo in constitution.get("moral_taboos") or []:
            t0_parts.append(f"禁忌:{str(taboo)[:120]}")
        if current_state.get("summary"):
            t0_parts.append(f"当前状态:{current_state['summary']}")

        for category, spec in CATEGORY_SPEC.items():
            if spec["tier"] != "t0":
                continue
            picked = [c for c in candidates if c.category == category and c.id in chosen]
            if not picked:
                continue
            if spec["mode"] == "item":
                t0_parts.extend(c.text for c in picked if c.visible)
            else:
                t0_parts.append(f"{spec['label']}:" + spec["sep"].join(c.value for c in picked))

        if constitution.get("core_belief"):
            t0_parts.append(f"禁止漂移:不得无因违背核心信念：{constitution['core_belief'][:80]}")

        t1 = f"- {name}"
        if current_state.get("summary"):
            t1 += f" | 状态:{current_state['summary']}"
        rel_picked = [c for c in candidates if c.category == "relationships" and c.id in chosen]
        if rel_picked:
            t1 += " | 关系张力:" + "；".join(c.value for c in rel_picked)

        return {
            "t0": " | ".join(x for x in t0_parts if x),
            "t1": t1,
            "t2": t2,
            # 隐藏面（POV 防火墙）作为独立锁字段输出，**不在此处做章号判断**。
            # compile() 的结果会被烤进 projection 并缓存（见 CharacterProjectionService.rebuild_projection），
            # 一旦在这里按章号裁剪，某个章节的判断就会被后续所有章节复用（提前泄密或永不揭露）。
            # 门控由持有 chapter_number 的渲染方（_projection_locks_for_plan）执行，
            # reveal_chapter 为 None 表示「总是可见」——与 CharacterDTO 字段说明及
            # context_builder / context_budget_allocator / engine.core.entities.character 一致。
            # 注意：application/engine/services/memory_engine.py:274-277 用的是相反口径
            # （要求 reveal_chapter is not None，即 None = 永不显示），与字段文档矛盾，
            # 属独立的既有缺陷，尚未修。
            "t0_hidden": f"隐藏面:{hidden}" if hidden else "",
            "t0_hidden_reveal_chapter": constitution.get("reveal_chapter"),
        }

    @staticmethod
    def _fallback_ids(candidates: Sequence[FactCandidate]) -> List[str]:
        """兜底：每个类别取前 N 条（与改造前的切片口径一致）。"""
        chosen: List[str] = []
        for category, spec in CATEGORY_SPEC.items():
            picked = [c for c in candidates if c.category == category][: spec["limit"]]
            chosen.extend(c.id for c in picked)
        return chosen
