"""H-02b：冲突检测的「实体-动作对」抽取口径与三态。

改造前这段是**代码用一张写死的关键词表**去猜（``magic_keywords`` 8 条 +
``weapon_keywords`` 4 条：「火球」「拔剑」…）：

- 词表里没有的写法一律**漏报**（「烈焰」「抽刀」「掷矛」「掌心凝火」…）；
- 词表是**代码代做的语义判断** ——「什么算用了火系魔法」本该看正文，不该看常量；
- 词表里的值是 ``fire_magic`` / ``use_sword`` 这种**封闭英文 id**，
  而设定库里的属性值是**中文**（「水系」「长剑」）⇒ 中间靠一张映射表对齐，
  而那张表只认 5 系 + 3 武器 ⇒ 本书用别的系（光系/暗系/空间系）永远查不出来。

现在由 **AI 抽实体-动作对**，代码只做**不越界**的校验：

1. **实体白名单** —— 实体名必须在候选清单里（AI 不得凭空造人）；
2. **kind 封闭枚举** —— 只有 ``magic`` / ``weapon`` 两种；
3. **值格式** —— 魔法值必须是「X系」形式（**与状态模型同口径**：
   ``character_state_extractor`` 取的就是 ``tag.endswith("系")`` 的那一个，
   所以设定库里根本不可能出现非「X系」的魔法属性 ⇒ 这一条不制造漏报）；
   武器值是**武器名**（不是「拔剑」这种动作）；
4. **证据必须逐字出现在「该实体的小句」里** —— 一条检查同时管两件事：
   - 「逐字出现在大纲里」⇒ AI 编出来的动作一律丢弃；
   - 「出现在**这个实体**的小句里」⇒ 动作不能挂到别人头上（H-02a）。

   为什么必须**合成一条**：拆成两条时，后一条会把前一条**顺带挡掉**
   （证据若不在大纲里，在小句里就近匹配必然也找不到）⇒ 前一条没有独立牙齿
   （复审 B3）。合成一条后，两种拒因互斥且都能独立触发。
5. **三态**：``ai``（AI 抽过）/ ``fallback``（AI 不可用 ⇒ 走代码关键词表，
   且来源可分辨）/ ``""``（未评估）。

**「AI 说没有动作」≠「未评估」**：AI 合法地返回空列表 ⇒ ``ai``（本场确实没有
魔法/武器动作，**不回退**）；AI 调用失败 / 输出不合契约 / 给的全是废数据 ⇒
``""`` ⇒ 调用方按**兜底**处理（用关键词表，与改造前逐位一致）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from application.audit.services.conflict_clause_attribution import (
    assign_clause_spans,
    clause_spans,
)

#: 来源标记
SOURCE_AI = "ai"
SOURCE_FALLBACK = "fallback"
SOURCE_NONE = ""

#: 动作类别（封闭枚举 —— AI 只能在这两类里选）
ACTION_KIND_MAGIC = "magic"
ACTION_KIND_WEAPON = "weapon"
ACTION_KINDS: Tuple[str, ...] = (ACTION_KIND_MAGIC, ACTION_KIND_WEAPON)

#: 属性值长度上限（魔法「X系」2-5 字；武器名 1-6 字）
MAX_VALUE_LEN = 6
#: 证据长度区间（原文片段）
MIN_EVIDENCE_LEN = 2
MAX_EVIDENCE_LEN = 40


@dataclass(frozen=True)
class EntityAction:
    """一条「实体做了什么」。

    ``kind`` 是封闭枚举；``value`` 是**归一化后的属性值**，与设定库里的
    ``magic_type`` / ``weapon`` **同一口径**（魔法「X系」、武器名），
    这样冲突判定就是两个字符串的机械比较，不需要第二张映射表。
    """

    kind: str
    value: str
    evidence: str = ""

    def __repr__(self) -> str:  # pragma: no cover - 仅为断言失败信息可读
        return f"EntityAction(kind={self.kind!r}, value={self.value!r})"


@dataclass(frozen=True)
class ConflictActionExtraction:
    """一次抽取的结果（含来源标记）。"""

    actions: Dict[str, List[EntityAction]] = field(default_factory=dict)
    source: str = SOURCE_NONE

    @property
    def is_usable(self) -> bool:
        """是否为「AI 真的答过了」（答「没有动作」也算答过）。"""
        return self.source == SOURCE_AI


class ConflictActionExtractionItem(BaseModel):
    """AI 返回的一条动作。"""

    entity_name: str = Field(description="实体名（必须来自候选清单）")
    kind: str = Field(description="magic | weapon")
    value: str = Field(description="归一化属性值：魔法「X系」/ 武器名")
    evidence: str = Field(default="", description="原文片段（逐字照抄）")


class ConflictActionExtractionLlmPayload(BaseModel):
    """AI 返回的抽取结果。"""

    actions: List[ConflictActionExtractionItem] = Field(default_factory=list)


def _is_valid_value(kind: str, value: str) -> bool:
    """属性值格式校验（代码保证的不变量之一）。"""
    if not value or len(value) > MAX_VALUE_LEN:
        return False
    if any(ch.isspace() for ch in value):
        return False
    if kind == ACTION_KIND_MAGIC:
        # 魔法值必须是「X系」——与设定库的 magic_type 同口径。
        return value.endswith("系")
    # 武器值是武器名，不是「拔剑」这种动作，也不该是「X系」那种魔法值。
    return not value.endswith("系")


def _evidence_in_ranges(
    text: str, ranges: List[Tuple[int, int]], evidence: str
) -> bool:
    """证据是否**逐字**出现在给定的小句范围内（**就近匹配**）。

    为什么是「就近」而不是「全篇首次出现」（复审 B2）：
    旧实现取 ``text.find(evidence)`` —— 只找**第一次**出现的位置。
    大纲里「李四拔出长剑。陈小握着长剑。」时，
    AI 报「陈小 / 长剑 / evidence='长剑'」，
    ``find`` 命中的是**李四**那句里的位置 ⇒ 落不进陈小的小句
    ⇒ **陈小的动作被静默丢掉** ⇒ 多角色同章时真冲突重新变成漏报
    —— 正是 H-02a 专门修好的东西，在新路径上被请回来。

    改为在**该实体自己的小句**里就近找 ⇒ 既修好漏报，
    又让「证据字面性」与「归属」变成同一条承重校验（见模块 docstring）。
    """
    if not evidence or not ranges:
        return False
    for lo, hi in ranges:
        if evidence in text[lo:hi]:
            return True
    return False


def normalize_extraction(
    data: Optional[Dict[str, Any]],
    *,
    valid_entity_names: Any,
    outline: str,
) -> ConflictActionExtraction:
    """把 AI 的输出收成可用的抽取结果。

    - 形状不对 ⇒ 未评估（``source=""``）；
    - 实体不在候选清单里 ⇒ 该条丢弃（**AI 不得凭空造人**）；
    - ``kind`` 不在封闭枚举里 ⇒ 该条丢弃；
    - 属性值格式不对 ⇒ 该条丢弃；
    - 证据不**逐字出现在该实体的小句里** ⇒ 该条丢弃
      （同时管住「AI 编的」与「挂到别人头上」两件事，见模块 docstring）；
    - AI **给了动作但一条都用不了** ⇒ 判为**未评估**（它的回答不可信），
      调用方按兜底处理；
    - AI 合法地回答「没有动作」（空列表）⇒ ``source="ai"``，
      **不回退**（模型说没有 ≠ 没评估）。
    """
    if not isinstance(data, dict):
        return ConflictActionExtraction({}, SOURCE_NONE)
    raw = data.get("actions")
    if not isinstance(raw, list):
        return ConflictActionExtraction({}, SOURCE_NONE)

    # 名单**保持调用方给的顺序** —— 归属规则里「一句话里多个实体取第一个作为
    # 主体」依赖这个顺序（与关键词表兜底路径同口径）。
    name_list = [str(n).strip() for n in (valid_entity_names or []) if str(n).strip()]
    allowed = set(name_list)
    text = outline or ""
    # 归属：哪些小句算在谁头上（H-02a 的规则，AI 路径复用同一份实现）
    spans = clause_spans(text)
    base_credited = assign_clause_spans(spans, name_list)

    actions: Dict[str, List[EntityAction]] = {}
    seen: set = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        entity_name = str(item.get("entity_name") or "").strip()
        if not entity_name or entity_name not in allowed:
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in ACTION_KINDS:
            continue
        value = str(item.get("value") or "").strip()
        if not _is_valid_value(kind, value):
            continue
        evidence = str(item.get("evidence") or "").strip()
        if len(evidence) < MIN_EVIDENCE_LEN or len(evidence) > MAX_EVIDENCE_LEN:
            continue
        # 归属校验（承重）：证据必须**逐字出现在这个实体的小句里**。
        # 这一条同时保证「证据字面出现在大纲里」（AI 编的动作一律丢弃）
        # 与「动作不挂到别人头上」（H-02a）。
        credited = base_credited
        if entity_name not in base_credited:
            # 不在名单里的人**单独**算一次归属：这样「实体白名单」这条规则
            # 才有独立的 teeth —— 挡人的只有白名单，不会被归属校验顺带挡掉。
            # （只算他一个，不混进已知实体 ⇒ 不会影响已知实体的归属。）
            credited = assign_clause_spans(spans, name_list + [entity_name])
        if not _evidence_in_ranges(text, credited.get(entity_name, []), evidence):
            continue
        key = (entity_name, kind, value)
        if key in seen:
            continue
        seen.add(key)
        actions.setdefault(entity_name, []).append(
            EntityAction(kind=kind, value=value, evidence=evidence)
        )

    if raw and not actions:
        # AI 明明给了动作，却一条都不可用 ⇒ 这份回答不可信 ⇒ 未评估
        return ConflictActionExtraction({}, SOURCE_NONE)
    return ConflictActionExtraction(actions, SOURCE_AI)
