"""EmotionLedger值对象 — 情绪账本（替代传统摘要）

核心改进：从"流水账摘要"升级为"小说家视角的情绪账本"

传统摘要（流水账）：
- 林羽和赵虎打了一架，林羽赢了。然后去了客栈。

EmotionLedger（小说家视角）：
- 【核心损失】林羽失去信任的导师（心境变化：多疑、谨慎）
- 【势能转化】从"被动挨打" → "暗中筹谋的猎手"
- 【未解悬念】赵虎的眼神暗示：另有幕后黑手

四大维度：
1. EmotionalWound：核心损失（失去导师 → 多疑）
2. EmotionalBoon：核心获得（获得剑谱 → 实力提升）
3. PowerShift：势能转化（被动挨打 → 暗中筹谋）
4. OpenLoop：未解悬念（赵虎眼神另有隐情）

⚠ G-04：模型给的值**原样保留**，代码不替它说话
------------------------------------------------
- ``OpenLoop.urgency``：**不再**被 clamp 到 [0,1]，读不出数字 ⇒ ``None``
  （**未评估**），而不是回落 0.5 ——
  此前 clamp + 回落 0.5 等于「代码替模型说话」：模型说 0.9 被改成 1，
  说不出被说成 0.5。
- 账本规模上限：每类最多留 ``MAX_ITEMS_PER_CATEGORY`` 条（留最新），
  丢弃的条数记在 ``trimmed_items``（**不许静默**）。
  ⚠ 未解悬念（open_loops）**不裁剪** —— 还没回收的线不能因为上限而消失。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass(frozen=True)
class EmotionalWound:
    """情绪创伤（核心损失）

    不是事件记录，而是事件对角色心态的影响
    示例：失去导师 → 心境变化：多疑、谨慎
    """

    description: str     # 创伤描述："失去信任的导师"
    impact: str          # 对角色心态的影响："多疑、谨慎"
    chapter_number: int = 0

    def to_summary_line(self) -> str:
        return f"【核心损失】{self.description}（心境变化：{self.impact}）"


@dataclass(frozen=True)
class EmotionalBoon:
    """情绪收获（核心获得）

    示例：获得师父的剑谱 → 实力提升，自信增加
    """

    description: str     # 收获描述："获得师父的剑谱"
    value: str           # 带来的价值："实力提升，自信增加"
    chapter_number: int = 0

    def to_summary_line(self) -> str:
        return f"【核心获得】{self.description}（价值：{self.value}）"


@dataclass(frozen=True)
class PowerShift:
    """势能转化

    记录角色/局势的权力关系变化
    示例：从"被动挨打" → "暗中筹谋的猎手"
    """

    from_state: str      # 从"被动挨打"
    to_state: str        # 到"暗中筹谋的猎手"
    trigger: str = ""    # 触发原因

    def to_summary_line(self) -> str:
        return f"【势能转化】从「{self.from_state}」→「{self.to_state}」"


@dataclass(frozen=True)
class OpenLoop:
    """未解悬念

    不是所有伏笔都需要回收，但需要追踪
    示例：赵虎的眼神暗示——另有幕后黑手
    """

    description: str     # 悬念描述："赵虎死前不可置信的眼神"
    hint: str            # 暗示线索："另有幕后黑手"
    planted_chapter: int = 0
    # ⚠ G-04：紧迫度**原样保留模型给的值**；``None`` = **未评估**
    #   （模型没给，或给的不是数字）。此前这里是 ``float = 0.5`` 且解析时
    #   被 clamp 到 [0,1] ⇒ 等于代码替模型说话。
    urgency: Optional[float] = None

    def to_summary_line(self) -> str:
        return f"【未解悬念】{self.description}（暗示：{self.hint}）"


#: 账本规模上限（登记表 G-04 的不变量）：每类最多留这么多条。
#: 留**最新**、丢最旧；被丢的条数记在 ``trimmed_items``（**不许静默**）。
MAX_ITEMS_PER_CATEGORY = 20

#: G-04-a：**注入侧**的口径 —— 每类最多注入多少条。
#:
#: ⚠ 此前「注入长度」完全由外部预算决定：
#:   - ``to_t0_section()`` 里 wounds/boons/power_shifts 各取最近 3/3/2 条，
#:     但 **open_loops 全部打印**（``add_open_loop`` 的 docstring 明说
#:     「未回收的悬念不能因为上限而消失」）⇒ 注入长度随未回收悬念**线性增长**；
#:   - 唯一的兜底是槽位的 ``max_tokens=400`` ⇒ 到点就被砍一刀，
#:     而砍掉的是「已埋但还没回收的线」⇒ **静默丢失**。
#:
#: ⇒ 现在：**账本自己定口径**（这里是独立预算口径的落点），
#:   每类注入条数有界；``open_loops`` 超量时**保留最新**并**明说**裁了多少
#:   （模型与作者都看得见「还有几条没列出来」）。
#:
#: ⚠ 注意与 ``MAX_ITEMS_PER_CATEGORY`` 的区别：那是**写入侧**的规模上限
#:   （账本里最多存多少条），这个是**注入侧**的展示口径（一次给模型看多少条）。
#: ⇒ 两者独立：写得进 ≠ 一次全给。
INJECT_ITEMS_PER_CATEGORY = 5


def coerce_urgency(value: Any) -> Optional[float]:
    """模型给的紧迫度**原样保留**；读不出数字 ⇒ **未评估**（None）。

    ⚠ G-04：此前这里是 ``max(0.0, min(1.0, float(v)))``，读不出时回落 0.5
    ⇒ 模型说 0.9 被改成 1、说不出被说成 0.5 —— **代码替模型说话**。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _trim_to_cap(items: list) -> tuple[list, int]:
    """按规模上限裁剪（留最新），返回 (裁剪后, 被丢条数)。"""
    if len(items) <= MAX_ITEMS_PER_CATEGORY:
        return items, 0
    return (
        items[-MAX_ITEMS_PER_CATEGORY:],
        len(items) - MAX_ITEMS_PER_CATEGORY,
    )


def _trim_for_injection(items: list) -> tuple[list, int]:
    """按**注入口径**裁剪（留最新），返回 (裁剪后, 被丢条数)。

    G-04-a：与 :func:`_trim_to_cap`（**写入侧**规模上限）不同 ——
    这个管**一次给模型看多少条**。
    """
    if len(items) <= INJECT_ITEMS_PER_CATEGORY:
        return items, 0
    return (
        items[-INJECT_ITEMS_PER_CATEGORY:],
        len(items) - INJECT_ITEMS_PER_CATEGORY,
    )


@dataclass(frozen=True)
class EmotionLedger:
    """情绪账本值对象（替代传统摘要）

    小说家视角的章节状态记录：
    - Wounds/Boons: 核心损失与获得
    - PowerShift: 势能转化
    - OpenLoops: 未解悬念

    这不是流水账，而是记录"林羽心里憋着什么火"
    """

    wounds: List[EmotionalWound] = field(default_factory=list)
    boons: List[EmotionalBoon] = field(default_factory=list)
    power_shifts: List[PowerShift] = field(default_factory=list)
    open_loops: List[OpenLoop] = field(default_factory=list)
    # 因规模上限被丢弃的历史条数（**不许静默**：要能查出来丢了多少）
    trimmed_items: int = 0

    @classmethod
    def create_empty(cls) -> EmotionLedger:
        """创建空账本"""
        return cls(wounds=[], boons=[], power_shifts=[], open_loops=[])

    def add_wound(self, wound: EmotionalWound) -> EmotionLedger:
        """追加创伤（返回新账本，不可变）。

        ⚠ 账本规模上限：每类最多留 ``MAX_ITEMS_PER_CATEGORY`` 条（留最新），
        丢弃的条数记在 ``trimmed_items``（**不许静默**）。
        """
        wounds, dropped = _trim_to_cap(self.wounds + [wound])
        return EmotionLedger(
            wounds=wounds,
            boons=self.boons,
            power_shifts=self.power_shifts,
            open_loops=self.open_loops,
            trimmed_items=self.trimmed_items + dropped,
        )

    def add_boon(self, boon: EmotionalBoon) -> EmotionLedger:
        """追加收获（返回新账本，不可变）。"""
        boons, dropped = _trim_to_cap(self.boons + [boon])
        return EmotionLedger(
            wounds=self.wounds,
            boons=boons,
            power_shifts=self.power_shifts,
            open_loops=self.open_loops,
            trimmed_items=self.trimmed_items + dropped,
        )

    def add_power_shift(self, shift: PowerShift) -> EmotionLedger:
        """追加势能转化（返回新账本，不可变）。"""
        power_shifts, dropped = _trim_to_cap(self.power_shifts + [shift])
        return EmotionLedger(
            wounds=self.wounds,
            boons=self.boons,
            power_shifts=power_shifts,
            open_loops=self.open_loops,
            trimmed_items=self.trimmed_items + dropped,
        )

    def add_open_loop(self, loop: OpenLoop) -> EmotionLedger:
        """追加未解悬念。

        ⚠ **不裁剪**：未回收的悬念不能因为上限而消失
        （那会让「还没埋的线」静默消失）。上限只作用于注入侧的 token 预算。
        """
        return EmotionLedger(
            wounds=self.wounds,
            boons=self.boons,
            power_shifts=self.power_shifts,
            open_loops=self.open_loops + [loop],
            trimmed_items=self.trimmed_items,
        )

    def close_loop(self, loop_description: str) -> EmotionLedger:
        """关闭已回收的悬念"""
        remaining = [l for l in self.open_loops if l.description != loop_description]
        return EmotionLedger(
            wounds=self.wounds,
            boons=self.boons,
            power_shifts=self.power_shifts,
            open_loops=remaining,
            trimmed_items=self.trimmed_items,
        )

    def to_t0_section(self) -> str:
        """生成T0层情绪账本注入内容。

        ⚠ G-04-a：此前**注入长度不受账本自己控制** ——
        wounds/boons/power_shifts 各取最近 3/3/2 条，但 **open_loops 全部打印**
        ⇒ 注入长度随未回收悬念**线性增长**，
        唯一的兜底是槽位 ``max_tokens=400`` ⇒ 到点被砍一刀，
        而砍掉的是「已埋但还没回收的线」⇒ **静默丢失**。

        ⇒ 现在按**注入口径**（``INJECT_ITEMS_PER_CATEGORY``）逐类裁剪，
        并且**明说**裁了多少 ⇒ 模型与作者都看得见「还有几条没列出来」。

        ⚠ 口径只管**展示**，不管**存储**：
        ``add_open_loop`` 仍然不裁剪（未回收的线不能因为上限而消失），
        写侧上限是另一个常量（``MAX_ITEMS_PER_CATEGORY``）。
        """
        lines = ["[情绪账本]"]
        for label, items, render in (
            ("创伤", self.wounds, lambda x: x.to_summary_line()),
            ("获得", self.boons, lambda x: x.to_summary_line()),
            ("势能变化", self.power_shifts, lambda x: x.to_summary_line()),
            ("未解悬念", self.open_loops, lambda x: x.to_summary_line()),
        ):
            kept, dropped = _trim_for_injection(items)
            for item in kept:
                lines.append(render(item))
            if dropped:
                # 不许静默：明说还剩几条没列出来
                lines.append(
                    f"（{label}共 {len(items)} 条，此处按注入口径只列最近 {len(kept)} 条，"
                    f"另有 {dropped} 条未列）"
                )
        if self.trimmed_items:
            # 复审 S6：写入侧因**账本规模上限**丢弃的条数此前只落库、从不展示
            # ⇒ 作者/模型都看不到「这个账本本身已经不完整」。
            # ⚠ 措辞是「未保留」而不是「已删」—— 它们被丢弃，但账本本身仍有边界。
            lines.append(
                f"（账本规模上限：另有 {self.trimmed_items} 条记录未保留在本账本中）"
            )
        return "\n".join(lines)

    def to_chapter_summary(self) -> str:
        """生成章节摘要（替代传统流水账）"""
        parts = []
        if self.wounds:
            parts.append("核心损失：" + "；".join(w.to_summary_line() for w in self.wounds))
        if self.boons:
            parts.append("核心获得：" + "；".join(b.to_summary_line() for b in self.boons))
        if self.power_shifts:
            parts.append("势能变化：" + "；".join(ps.to_summary_line() for ps in self.power_shifts))
        if self.open_loops:
            parts.append("未解悬念：" + "；".join(ol.to_summary_line() for ol in self.open_loops))
        return "\n".join(parts)

    def to_dict(self) -> dict:
        """序列化"""
        return {
            "wounds": [{"description": w.description, "impact": w.impact, "chapter": w.chapter_number} for w in self.wounds],
            "boons": [{"description": b.description, "value": b.value, "chapter": b.chapter_number} for b in self.boons],
            "power_shifts": [{"from": ps.from_state, "to": ps.to_state, "trigger": ps.trigger} for ps in self.power_shifts],
            "open_loops": [
                {
                    "description": ol.description,
                    "hint": ol.hint,
                    "planted_chapter": ol.planted_chapter,
                    "urgency": ol.urgency,
                }
                for ol in self.open_loops
            ],
            # 因规模上限被丢弃的历史条数（**不许静默**）
            "trimmed_items": self.trimmed_items,
        }

    @classmethod
    def from_dict(cls, data: dict) -> EmotionLedger:
        """反序列化"""
        wounds = [cls._parse_wound(w) for w in data.get("wounds", [])]
        boons = [cls._parse_boon(b) for b in data.get("boons", [])]
        power_shifts = [cls._parse_power_shift(ps) for ps in data.get("power_shifts", [])]
        open_loops = [cls._parse_open_loop(ol) for ol in data.get("open_loops", [])]
        trimmed_items = int(data.get("trimmed_items", 0) or 0)
        return cls(
            wounds=wounds,
            boons=boons,
            power_shifts=power_shifts,
            open_loops=open_loops,
            trimmed_items=trimmed_items,
        )

    @staticmethod
    def _parse_wound(data: dict) -> EmotionalWound:
        return EmotionalWound(
            description=data.get("description", ""),
            impact=data.get("impact", ""),
            chapter_number=int(data.get("chapter_number", data.get("chapter", 0)) or 0),
        )

    @staticmethod
    def _parse_boon(data: dict) -> EmotionalBoon:
        return EmotionalBoon(
            description=data.get("description", ""),
            value=data.get("value", ""),
            chapter_number=int(data.get("chapter_number", data.get("chapter", 0)) or 0),
        )

    @staticmethod
    def _parse_power_shift(data: dict) -> PowerShift:
        return PowerShift(
            from_state=data.get("from_state", data.get("from", "")),
            to_state=data.get("to_state", data.get("to", "")),
            trigger=data.get("trigger", ""),
        )

    @staticmethod
    def _parse_open_loop(data: dict) -> OpenLoop:
        return OpenLoop(
            description=data.get("description", ""),
            hint=data.get("hint", ""),
            planted_chapter=int(data.get("planted_chapter", data.get("chapter", 0)) or 0),
            # ⚠ G-04：不再 clamp、不再回落 0.5 ⇒ 读不出就是「未评估」
            urgency=coerce_urgency(data.get("urgency")),
        )
