"""H-01：死亡角色在大纲里出现时，是「活跃登场」还是「回忆 / 遗物 / 传闻」。

改造前
------
``EvolutionGateService`` 的 ``DEAD_CHARACTER_REAPPEARS`` 规则是：

```python
hit = _outline_mentions(outline, labels)
if status == "dead" and hit:
    → blocking 违规，消息写着「当前大纲仍让其以活跃人物出现」
```

它只知道**名字出现在大纲里**。于是「回忆《林澈》当年的约定」、
「桌上还摆着林澈的旧剑」这类**合法的回忆 / 遗物**同样被判成
「活跃登场」⇒ 误报。

⇒ 而且这条规则**自相矛盾**：它给的建议是「改为回忆/遗物/传闻」，
可改了之后**照样**被拦（因为判据就是「名字出现了」）。

现在由 **AI** 判：它看到大纲里这个死亡角色的**上下文**，
判断这是「他在场行动」还是「只是被想起 / 被纪念 / 被提及」。

三态
----
- ``ai``：AI 判过了（哪怕它说「只是回忆」也算判过）；
- ``""``（未评估）：调用失败 / 输出不合契约 / 给的全是废数据
  ⇒ 调用方回落**规则判定**（命中即拦，与改造前逐位一致），
  且来源可分辨（``rule``）。

不变量（代码保证，AI 动不了）
----------------------------
1. **角色白名单**：``character_id`` 必须来自候选清单（AI 不得凭空造人）；
2. **verdict 封闭枚举**：只有 ``active_appearance`` / ``memory_or_relic`` /
   ``unclear`` 三种；
3. **有结论必须有依据**：``verdict != unclear`` 时 ``reason`` 不得为空。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

#: 来源标记
SOURCE_AI = "ai"
SOURCE_NONE = ""

#: 活跃登场 —— 角色在当下时间线里真的出现了（**真违规**）
VERDICT_ACTIVE = "active_appearance"
#: 回忆 / 遗物 / 传闻 —— 只是被想起、被纪念、被提及（**不是违规**）
VERDICT_MEMORY = "memory_or_relic"
#: 判不出来 —— 信息不足，**不下结论**（≠ 违规，也 ≠ 没违规）
VERDICT_UNCLEAR = "unclear"

#: verdict 封闭枚举（AI 只能在这三个里选）
VERDICTS = (VERDICT_ACTIVE, VERDICT_MEMORY, VERDICT_UNCLEAR)


@dataclass(frozen=True)
class DeadCharacterVerdict:
    """AI 对**一个**死亡角色在本章里的出场方式的判定。"""

    character_id: str
    verdict: str
    reason: str

    @property
    def is_violation(self) -> bool:
        """是否构成「死亡角色活跃登场」（真违规）。"""
        return self.verdict == VERDICT_ACTIVE

    @property
    def is_unassessed(self) -> bool:
        """是否「判不出来」（显式未评估，≠ 没违规）。"""
        return self.verdict == VERDICT_UNCLEAR

    def __repr__(self) -> str:  # pragma: no cover - 仅为断言失败信息可读
        return (
            f"DeadCharacterVerdict(character_id={self.character_id!r}, "
            f"verdict={self.verdict!r})"
        )


@dataclass(frozen=True)
class DeadCharacterCheck:
    """一次检查的结果（含来源标记）。"""

    verdicts: Dict[str, DeadCharacterVerdict] = field(default_factory=dict)
    source: str = SOURCE_NONE

    @property
    def is_usable(self) -> bool:
        """是否为「AI 真的答过了」。"""
        return self.source == SOURCE_AI


class DeadCharacterVerdictItem(BaseModel):
    """AI 返回的一条判定。"""

    character_id: str = Field(default="", description="角色 ID（必须来自候选清单）")
    verdict: str = Field(
        default="",
        description=(
            "active_appearance（活跃登场）| memory_or_relic（回忆/遗物/传闻）"
            " | unclear（判不出来）"
        ),
    )
    reason: str = Field(default="", description="判断依据（verdict != unclear 时不得为空）")


class DeadCharacterCheckLlmPayload(BaseModel):
    """evolution-dead-character-check 节点的 LLM 输出。"""

    verdicts: List[DeadCharacterVerdictItem] = Field(default_factory=list)
    reason: str = Field(default="", description="整体说明（可空）")


def normalize_dead_character_check(
    data: Optional[Dict[str, Any]],
    *,
    valid_character_ids: Any,
) -> DeadCharacterCheck:
    """把 AI 的输出收成可用的检查结果。

    - 形状不对 ⇒ **未评估**（``source=""``）；
    - ``character_id`` 不在候选清单 ⇒ 该条丢弃（**AI 不得凭空造人**）；
    - ``verdict`` 不在封闭枚举里 ⇒ 该条丢弃；
    - ``verdict != unclear`` 而 ``reason`` 为空 ⇒ 该条丢弃
      （不许给一个没有理由的判定）；
    - AI **给了判定但一条都用不了** ⇒ 判为**未评估**（这份回答不可信），
      调用方按规则兜底处理；
    - AI 合法地说「只是回忆」⇒ ``source="ai"``，**不回退**。
    """
    if not isinstance(data, dict):
        return DeadCharacterCheck({}, SOURCE_NONE)
    raw = data.get("verdicts")
    if not isinstance(raw, list):
        return DeadCharacterCheck({}, SOURCE_NONE)

    allowed = {
        str(c).strip() for c in (valid_character_ids or []) if str(c).strip()
    }

    out: Dict[str, DeadCharacterVerdict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        character_id = str(item.get("character_id") or "").strip()
        if not character_id or character_id not in allowed:
            continue
        verdict = str(item.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            continue
        reason = str(item.get("reason") or "").strip()
        if verdict != VERDICT_UNCLEAR and not reason:
            continue
        if character_id in out:
            continue
        out[character_id] = DeadCharacterVerdict(
            character_id=character_id,
            verdict=verdict,
            reason=reason,
        )

    if raw and not out:
        return DeadCharacterCheck({}, SOURCE_NONE)
    return DeadCharacterCheck(out, SOURCE_AI)
