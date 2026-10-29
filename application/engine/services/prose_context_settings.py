"""X1：生产正文提示词要带哪几块「硬事实」（槽位级），以及每块的上限。

## 背景

`engine/pipeline/prose_composer.py` 的 `_build_variables` 曾经这样写：

```python
continuity_context = (str(metadata.get("continuity_context") or "").strip()
                      or str(request.context_text or "").strip())
```

两个**不同的东西**挤在同一个变量槽里：

- `metadata["continuity_context"]` = 「近3章承接摘要」（来自 continuity ledger，
  空时返回「暂无近3章承接摘要。」⇒ **恒非空**）；
- `request.context_text` = T0–T3 上下文包。

⇒ `or` 的后半支**永不触发** ⇒ 生产正文提示词里**没有上下文包**。

## 但「整包倾倒」是错的

正文提示词的**三变量设计是刻意的**（2026-06-08 `444f5e0d`「优化生文流程 & 提示词」：
同一次提交里改了 composer、加了 `test_..._stay_minimal_for_prose_prompt` 与
`test_..._builds_only_core_prompt_variables` 两个守卫、改了 prompt 模板的
「以【正文细纲】中的七段结构为唯一剧情骨架」硬约束）。
⇒ 上下文包是那套设计里的**兜底**（没有承接摘要时顶着），不是主料。

而且整包**很贵**（生产库实测）：
- 上下文包本身 6.9k~19.4k token/章；
- 其中 `fact_lock`（绝对事实边界）在 529 章那本书里就有 **20365 字 ≈ 12.7k token**
  —— 它自己就在无上限地长（独立缺陷，见遗留）。

## 本模块的取舍

只送**「硬事实」小块**（默认：死亡锁/白名单 + 角色当前状态 + 创作契约），
每块带上限 ⇒ ~3-4k token/章，且不与「七段结构是唯一剧情骨架」打架：
**硬事实是「不得违反的既成事实」，不是剧情指令。**

- `enabled`：要不要送（默认开 —— 这是断链的修复）；
- `slots`：送哪几个槽位（键名见 `context_budget_allocator` 的 slots 字典）；
- `max_chars_per_slot`：每块上限（超出要**说明**，不静默截断）。

配置位置：`config/performance.yaml` 的 `prose_context:` 段。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple

from application.core.config.config_loader import get_config
from application.core.config.runtime_settings_utils import (
    non_negative_int,
    section_value,
)


#: 默认送进正文提示词的「硬事实」槽位
#: - fact_lock          绝对事实边界（死亡锁 / 白名单 / 关系 / 身份 / 时间线）= 不可违反
#: - character_anchors  角色锚点（当前状态 / 伤势 / 位置 / 情绪 + T2 措辞）= D-04 的载体
#: - narrative_contract 创作契约（向导五维 / 文风公约 / Bible 规则）= 风格与价值观
DEFAULT_SLOTS: Tuple[str, ...] = (
    "fact_lock",
    "character_anchors",
    "narrative_contract",
)

#: 每块默认上限（字符）—— fact_lock 在长篇上会涨到两万字，必须切
#:
#: ⚠ 复审 S6：这是**内容**的上限，**不含**「点名块」与「截断标记」——
#:   截断后会在内容**前面**加一段「【被截掉的小节：…】」⇒ 实际块长会略微超出这个数
#:   （实测 fact_lock 块长 2052 > 2000）。
#: ⇒ **按这个数推理余量会算错**；**总量**才是硬约束
#:   （``PROSE_CONTEXT_SLOT_OVERHEAD_CHARS`` 与 composer 的兜底都按**实际长度**算）。
DEFAULT_MAX_CHARS_PER_SLOT = 12000

#: 每块的**额外**开销（块标题 ``=== XXX ===`` + 截断标记）的预留字符数。
#:
#: 第 3 批复审 S5：此前 builder 只守「逐块上限」（2000），
#: 而 composer 的**总长**兜底是 ``max_chars_per_slot × 槽位数``（2000×3=6000）。
#: 但 builder 的实际产出 = Σ(块标题 + 内容 + 截断标记) ≈ 6109
#: ⇒ **超过** composer 的兜底 ⇒ composer 被迫在第三块**中间**腰斩，
#: 把那块自己的「（本块已按上限截断）」标记换成了总标记
#: ⇒ 模型看到的最后一块不再标着「2000 字上限」。
#:
#: ⇒ 现在**总长上限**由这一个公式给出（builder 与 composer 共用）：
#:     总上限 = (每块上限 + 每块开销) × 块数
#: ⇒ builder 自己守住它 ⇒ composer 的兜底截断**不会发生**。
PROSE_CONTEXT_SLOT_OVERHEAD_CHARS = 200


@dataclass(frozen=True)
class ProseContextRuntimeSettings:
    #: 是否把「硬事实」送进正文提示词
    enabled: bool = True
    #: 送哪几个槽位
    slots: Tuple[str, ...] = DEFAULT_SLOTS
    #: 每块上限（0 = 不切）
    max_chars_per_slot: int = DEFAULT_MAX_CHARS_PER_SLOT


def _slots_of(section) -> Tuple[str, ...]:
    raw = section_value(section, "slots", None)
    if raw is None:
        return DEFAULT_SLOTS
    try:
        items = tuple(str(x).strip() for x in raw if str(x).strip())
    except TypeError:
        return DEFAULT_SLOTS
    return items or DEFAULT_SLOTS


def get_prose_context_runtime_settings() -> ProseContextRuntimeSettings:
    section = getattr(get_config(), "prose_context", None)
    defaults = ProseContextRuntimeSettings()
    return ProseContextRuntimeSettings(
        enabled=bool(section_value(section, "enabled", defaults.enabled)),
        slots=_slots_of(section),
        max_chars_per_slot=non_negative_int(
            section_value(section, "max_chars_per_slot", defaults.max_chars_per_slot),
            defaults.max_chars_per_slot,
        ),
    )


def prose_context_total_cap(settings: ProseContextRuntimeSettings | None = None) -> int:
    """「硬事实」整包的**总长上限**（字符）。

    builder 与 composer **共用**这一个公式 ⇒ 两边不会各算一套
    （此前 builder 6109 / composer 6000 的错位就是这么来的）。
    """
    current = settings or get_prose_context_runtime_settings()
    slots = max(len(current.slots), 1)
    return (current.max_chars_per_slot + PROSE_CONTEXT_SLOT_OVERHEAD_CHARS) * slots
