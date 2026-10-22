"""B-08：建幕时「这一幕属于哪一卷」的结果载体 + **代码保留的不变量**。

改造前，新一幕挂到哪一卷**完全由代码按幕数计数决定**
（``daemon_host._find_parent_volume_for_new_act``：锚点卷幕数达到
``rec_acts_per_volume`` 才换卷，只向后、不回填）—— 「换卷时机」是创作判断，
却被写成了一条计数规则（登记表 B-08：「该谁定 = AI 声明所属卷」）。

现在这个判断交回 AI（``planning-act-volume-placement`` 节点），
但**两条不变量仍由代码保证**（登记表 B-08「代码保留的不变量」列）：

1. **卷号单调** —— 新一幕所在卷号不得小于上一幕所在卷号；
2. **不回填** —— 不许选锚点卷**之前**的卷。partial 宏观规划会留下大量空壳卷，
   回填会让后期章节倒挂到早期剧情定位的卷下（本仓真实书目曾因此堆积 76 幕）。

不变量校验与选卷策略**共用同一个锚点实现**（``resolve_volume_anchor``）——
同一逻辑写两份必然各自漂移。

三态与来源
----------
- ``ai``       —— AI 声明了卷号，且该卷号落在允许集合内 ⇒ 用它；
- ``none``     —— **未评估**：AI 没判出来 / 调用失败 / 输出不合契约
  ⇒ 调用方回落**代码策略**（改造前的行为，逐位一致）；
- ``fallback`` —— 已回落代码策略（落库时如实标注，不许标成「AI 声明的」）。

「AI 说没有」与「没评估」必须可分辨：前者是 ``none`` + ``declared_volume_number
is None``，后者只能通过日志与 ``fallback`` 标记区分 —— 所以代码**不编造**任何
「AI 认为该留在当前卷」之类的结论。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

#: AI 声明可用，且落在允许集合内
SOURCE_AI = "ai"
#: **未评估**：AI 没判出来 / 调用失败 / 输出不合契约
SOURCE_NONE = "none"
#: 已回落代码策略（逐位一致于改造前）
SOURCE_FALLBACK = "fallback"

#: 锚点原因：还没有任何幕
ANCHOR_NO_ACTS = "no_acts"
#: 锚点原因：最新一幕所在的卷
ANCHOR_LATEST_ACT = "latest_act"
#: 锚点原因：最新一幕的父卷不在卷列表里（脏数据）
ANCHOR_DANGLING = "dangling"

#: 拒绝原因：AI 没判出来
REJECT_NOT_ASSESSED = "not_assessed"
#: 拒绝原因：声明的卷号不在允许集合（卷不存在，或违反卷号单调/不回填）
REJECT_NOT_ALLOWED = "not_allowed"

#: 依据文本长度上限（防止模型写一整页塞进提示词）
MAX_REASON_CHARS = 400


@dataclass(frozen=True)
class VolumePlacement:
    """一次「这一幕属于哪一卷」的判定结果。"""

    #: AI 声明的卷号。``None`` = 未评估 / 判不出来。
    declared_volume_number: Optional[int]
    #: AI 给的依据（空串 = 模型没写）
    reason: str = ""
    #: ``ai`` / ``none`` / ``fallback``
    source: str = SOURCE_NONE
    #: AI 声明被拒的原因（空串 = 没被拒）。见 ``REJECT_*``。
    reject_reason: str = ""

    @property
    def is_assessed(self) -> bool:
        """AI 是否给出了一个可用的声明。"""
        return self.source == SOURCE_AI and self.declared_volume_number is not None


@dataclass(frozen=True)
class VolumeAnchor:
    """锚点卷的解析结果。"""

    #: 按 number 排好序的卷列表
    ordered: list = field(default_factory=list)
    #: 锚点卷节点；``None`` = 没有可用锚点（还没有幕 / 脏数据）
    anchor: Any = None
    #: ``ANCHOR_NO_ACTS`` / ``ANCHOR_LATEST_ACT`` / ``ANCHOR_DANGLING``
    reason: str = ANCHOR_NO_ACTS
    #: 脏数据时的现场信息（仅用于日志）
    last_act_id: str = ""
    last_act_parent_id: str = ""


def normalize_volume_placement(raw: Any) -> VolumePlacement:
    """规范化模型的卷号声明。**不编造**：给不出就是给不出。

    - 卷号不是正整数 ⇒ 判为**未评估**（``none``），不回落成某个默认卷；
    - 布尔值不是卷号（``True`` 不是第 1 卷）；
    - 非字符串的 ``reason`` 丢弃（**不** ``str()`` 转成假文本 ——
      ``str(None)`` 会得到字面量 ``"None"``，那是把「模型没给」伪装成「模型说了 None」）。
    """
    if not isinstance(raw, dict):
        return VolumePlacement(None, "", SOURCE_NONE)

    raw_number = raw.get("volume_number")
    number: Optional[int] = None
    if isinstance(raw_number, bool):
        number = None
    elif isinstance(raw_number, int):
        number = raw_number
    elif isinstance(raw_number, str):
        text = raw_number.strip()
        if text.isdigit():
            number = int(text)
    if number is not None and number <= 0:
        # 卷号从 1 起
        number = None

    raw_reason = raw.get("reason")
    reason = (
        raw_reason.strip()[:MAX_REASON_CHARS] if isinstance(raw_reason, str) else ""
    )

    if number is None:
        return VolumePlacement(None, reason, SOURCE_NONE)
    return VolumePlacement(number, reason, SOURCE_AI)


def resolve_volume_anchor(
    volume_nodes: Sequence[Any],
    act_nodes: Sequence[Any],
) -> VolumeAnchor:
    """锚点卷 = **最新幕所在的卷**；还没有幕时 = 第一卷。

    返回 ``VolumeAnchor``。选卷策略与不变量校验**共用**这一个实现 ——
    两边看到不同的锚点就会各自漂移（早前的 ``_observed_acts_per_volume``
    就因为和选卷策略口径不一致，把公式当成了地板）。

    ⚠ ``act_nodes`` 必须已按 number 排序（幕号即时间序）。
    """
    ordered = sorted(
        list(volume_nodes or []),
        key=lambda v: (
            getattr(v, "number", None)
            if getattr(v, "number", None) is not None
            else 0
        ),
    )
    acts = list(act_nodes or [])
    if not acts:
        # 没有幕 ⇒ 锚点 = 第一卷（与选卷策略的「第一个未满卷」同口径）
        return VolumeAnchor(
            ordered=ordered,
            anchor=ordered[0] if ordered else None,
            reason=ANCHOR_NO_ACTS,
        )

    last_act = acts[-1]
    parent_id = getattr(last_act, "parent_id", None)
    anchor = next(
        (v for v in ordered if getattr(v, "id", None) == parent_id),
        None,
    )
    if anchor is None:
        return VolumeAnchor(
            ordered=ordered,
            anchor=None,
            reason=ANCHOR_DANGLING,
            last_act_id=str(getattr(last_act, "id", "") or ""),
            last_act_parent_id=str(parent_id or ""),
        )
    return VolumeAnchor(ordered=ordered, anchor=anchor, reason=ANCHOR_LATEST_ACT)


def allowed_volume_numbers(
    volume_nodes: Sequence[Any],
    act_nodes: Sequence[Any],
) -> list[int]:
    """AI 可声明的卷号集合 —— **代码保留的不变量**。

    - 锚点是最新幕所在卷 ⇒ 允许的卷号 **≥ 锚点卷号**（卷号单调 + 不回填）；
    - 没有锚点（还没有幕 / 锚点脏数据）⇒ 允许全部卷号
      （此时「单调」无从谈起，与选卷策略的脏数据回退口径保持一致）。
    """
    anchor = resolve_volume_anchor(volume_nodes, act_nodes)
    anchor_number = getattr(anchor.anchor, "number", None)
    numbers: list[int] = []
    for volume in anchor.ordered:
        number = getattr(volume, "number", None)
        if number is None:
            continue
        number = int(number)
        if anchor.anchor is not None and anchor_number is not None:
            if number < int(anchor_number):
                # 更早的卷：不许回填
                continue
        numbers.append(number)
    return numbers


def resolve_placed_volume_number(
    declared: Optional[int],
    allowed_numbers: Sequence[int],
) -> tuple[Optional[int], str]:
    """把 AI 的声明落到一个**允许**的卷号上。

    返回 ``(有效卷号, 拒绝原因)``：

    - 有效卷号为 ``None`` ⇒ 这次声明不可用，调用方**必须**回落代码选卷策略；
    - 拒绝原因非空 ⇒ 为什么要拒（落日志、落库），**不许静默**。

    这条函数就是登记表 B-08「代码保留的不变量：卷号单调、不回填」的落点 ——
    AI 说什么都不能越过它。
    """
    if declared is None:
        return None, REJECT_NOT_ASSESSED
    allowed = {int(n) for n in (allowed_numbers or [])}
    if int(declared) not in allowed:
        return None, REJECT_NOT_ALLOWED
    return int(declared), ""
