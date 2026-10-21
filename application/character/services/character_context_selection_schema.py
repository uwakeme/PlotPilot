"""D-04：角色上下文「按章取材」的取值口径与三态。

改造前这段是代码按固定条数切事实：``scars[:2]`` / ``motivations[:2]`` /
voice ``[:3]`` / catchphrases ``[:3]`` / 知识边界 ``[:3]`` / 关系 ``[:2]``，
而 T2 措辞对所有角色一字不差（「允许过场/提及，禁止抢走主线焦点」）。

「这一章该给模型看这个角色的哪几条记忆」是**创作判断**，被写成了常量 ——
登记表 D-04 的判定是 C。

本模块定义 AI 取材结果的口径：

- **代码给候选，AI 只挑不新增** —— 候选 id 不在清单里的一律丢弃
  （AI 不得凭空写出角色没有的记忆）。
- **角色锚点必进** —— 名字 / 公开面 / 核心信念 / 禁忌 / 当前状态由代码保证
  永远在上下文里；AI 只能**增删事实条目**，动不了锚点；T2 的 ``- {名字}：``
  前缀也由代码拼，AI 只写后半句。
- **三态**：``ai``（AI 挑过）/ ``fallback``（AI 不可用 ⇒ 用代码固定切片，
  且在程序层面可分辨）/ ``""``（未评估）。
  来源标记**不进 prompt 文本** —— 那是写给模型的，不能污染写作提示。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

#: 来源标记
SOURCE_AI = "ai"
SOURCE_FALLBACK = "fallback"
SOURCE_NONE = ""

#: 事实类别（代码产出候选，AI 只在这些类别里挑）
FACT_CATEGORIES: Tuple[str, ...] = (
    "scars",
    "motivations",
    "voice",
    "catchphrases",
    "knowledge",
    "relationships",
)

#: 类别 → 候选 id 前缀（稳定 id：同一份 projection 每次生成同一批 id）
CATEGORY_PREFIX: Dict[str, str] = {
    "scars": "s",
    "motivations": "m",
    "voice": "v",
    "catchphrases": "c",
    "knowledge": "k",
    "relationships": "r",
}


class CharacterContextSelectionLlmPayload(BaseModel):
    """LLM 返回的结构化取材结果。"""

    characters: List[Dict[str, Any]] = Field(default_factory=list)


@dataclass(frozen=True)
class CharacterFactSelection:
    """单个角色的取材结果。"""

    character_id: str
    fact_ids: Tuple[str, ...] = ()
    t2_instruction: str = ""
    source: str = SOURCE_NONE

    @property
    def is_usable(self) -> bool:
        return self.source == SOURCE_AI


@dataclass(frozen=True)
class CharacterContextSelection:
    """整章的取材结果。"""

    characters: Dict[str, CharacterFactSelection] = field(default_factory=dict)
    source: str = SOURCE_NONE

    @property
    def is_usable(self) -> bool:
        return self.source == SOURCE_AI

    def for_character(self, character_id: str) -> Optional[CharacterFactSelection]:
        return self.characters.get(str(character_id))

    def to_payload(self) -> Dict[str, Any]:
        """转成可沿调用链下传的纯 dict（要跨 4 层函数签名）。"""
        return {
            "source": self.source,
            "characters": {
                cid: {
                    "fact_ids": list(sel.fact_ids),
                    "t2": sel.t2_instruction,
                }
                for cid, sel in self.characters.items()
            },
        }


def selection_from_payload(payload: Any) -> Optional[CharacterContextSelection]:
    """把下传链上的纯 dict 还原成取值对象。

    形状不对 / 不是 AI 来源 ⇒ 返回 ``None``（调用方按兜底处理）。
    """
    if not isinstance(payload, dict):
        return None
    if str(payload.get("source") or "") != SOURCE_AI:
        return None
    raw = payload.get("characters")
    if not isinstance(raw, dict):
        return None
    characters: Dict[str, CharacterFactSelection] = {}
    for cid, item in raw.items():
        if not isinstance(item, dict):
            continue
        ids = item.get("fact_ids")
        if not isinstance(ids, list):
            ids = []
        characters[str(cid)] = CharacterFactSelection(
            character_id=str(cid),
            fact_ids=tuple(str(x) for x in ids),
            t2_instruction=str(item.get("t2") or ""),
            source=SOURCE_AI,
        )
    if not characters:
        return None
    return CharacterContextSelection(characters, SOURCE_AI)


def normalize_selection(
    data: Optional[Dict[str, Any]],
    *,
    valid_fact_ids: Dict[str, set],
) -> CharacterContextSelection:
    """把 LLM 输出收成可用的取材结果。

    - 角色 id 不在 ``valid_fact_ids`` 里 ⇒ 整个角色丢弃（不存在的人）。
    - 事实 id 不在该角色的候选清单里 ⇒ 丢弃（**AI 不得凭空造记忆**）。
    - 一个角色既没挑事实也没给 T2 ⇒ 该角色不产出选择（走兜底）。
    """
    if not isinstance(data, dict):
        return CharacterContextSelection({}, SOURCE_NONE)
    raw_characters = data.get("characters")
    if not isinstance(raw_characters, list):
        return CharacterContextSelection({}, SOURCE_NONE)

    selected: Dict[str, CharacterFactSelection] = {}
    for item in raw_characters:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("character_id") or "").strip()
        if not cid or cid not in valid_fact_ids:
            continue
        raw_ids = item.get("selected_fact_ids")
        if not isinstance(raw_ids, list):
            raw_ids = []
        allowed = valid_fact_ids[cid]
        kept = tuple(
            fid for fid in (str(x).strip() for x in raw_ids) if fid and fid in allowed
        )
        t2 = str(item.get("t2_instruction") or "").strip().splitlines()[0][:200].strip() if item.get("t2_instruction") else ""
        if not kept and not t2:
            continue
        selected[cid] = CharacterFactSelection(
            character_id=cid,
            fact_ids=kept,
            t2_instruction=t2,
            source=SOURCE_AI,
        )

    if not selected:
        return CharacterContextSelection({}, SOURCE_NONE)
    return CharacterContextSelection(selected, SOURCE_AI)
