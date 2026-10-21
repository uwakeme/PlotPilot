"""H-02b：冲突检测的「小句归属」口径 —— 动作算在谁头上。

这块逻辑原来长在 ``conflict_detection_service`` 里（关键词表路径专用）。
H-02b 之后它有**两个**用户：

- 兜底路径：关键词表按小句归属（改造前的行为，逐位不变）；
- **AI 路径**：代码要能验证「AI 说这个人做了这件事」不是编的 ——
  证据的起点必须落在**这个人被归属的小句**里。

⇒ 抽成共享实现，避免「同一逻辑写两份」。

为什么按小句而不是整句
----------------------
中文大纲常常一逗到底：「陈小拔出长剑，李四仓守后退」。
按整句切 ⇒ 两个人共享一个动作 ⇒ 不佩剑的角色也拿到一条假的设定冲突。
按小句切（逗号级）才能分清谁做的。

连接词主体沿用
--------------
「李明释放火球术，然后拔剑攻击」——后半句**没有主语**，主体沿用上一小句。
少了这条继承，「李明」就只会被记到火球、记不到拔剑。
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: 小句分隔符：句末标点 + 逗号级分隔符 + 换行
_CLAUSE_BREAKS = "。！？；…，、：\n"

#: 连接词（用于「主体沿用上一小句」）
_CLAUSE_CONNECTIVES = (
    "然后", "接着", "继而", "随后", "紧接着",
    "又", "并", "同时", "而后", "随即", "接着就",
)

#: 小句区间：(起始偏移, 结束偏移, 文本)
ClauseSpan = Tuple[int, int, str]


def split_into_clauses(text: str) -> List[str]:
    """把中文正文切成**小句**（逗号级）。

    切分点：句末标点 。！？；… 与换行，加上小句分隔符 ，、：。
    引号内的句号不特殊处理 —— 大纲文本短，误切只会让归属更保守
    （更少误报），方向是安全的。
    """
    return [span[2] for span in clause_spans(text)]


def clause_spans(text: str) -> List[ClauseSpan]:
    """切成小句并保留**原文偏移**（AI 路径要按偏移校验证据归属）。"""
    if not text:
        return []
    spans: List[ClauseSpan] = []
    buf: List[str] = []
    seg_start = 0
    for i, ch in enumerate(text):
        buf.append(ch)
        if ch in _CLAUSE_BREAKS:
            raw = "".join(buf)
            clause = raw.strip()
            if clause:
                lead = len(raw) - len(raw.lstrip())
                lo = seg_start + lead
                spans.append((lo, lo + len(clause), clause))
            buf = []
            seg_start = i + 1
    raw = "".join(buf)
    clause = raw.strip()
    if clause:
        lead = len(raw) - len(raw.lstrip())
        lo = seg_start + lead
        spans.append((lo, lo + len(clause), clause))
    return spans


def assign_clause_spans(
    spans: Sequence[ClauseSpan], names: Iterable[str]
) -> Dict[str, List[Tuple[int, int]]]:
    """把小句归属给实体，返回每个实体被归属的**小句区间**。

    规则：

    1. 小句里出现实体名 ⇒ 该实体被归属；
    2. 小句里没有实体名但以连接词开头（「然后」「接着」…）⇒ 主体沿用上一小句；
    3. 一句里有多个实体时取第一个作为主体（推理粗，但不会造成误报）。
    """
    name_list = [str(n) for n in names if str(n)]
    result: Dict[str, List[Tuple[int, int]]] = {}
    last_subject: Optional[str] = None
    for lo, hi, clause in spans:
        mentioned = [n for n in name_list if n and n in clause]
        if not mentioned and last_subject is not None and clause.startswith(
            _CLAUSE_CONNECTIVES
        ):
            mentioned = [last_subject]
        for name in mentioned:
            result.setdefault(name, []).append((lo, hi))
        if mentioned:
            last_subject = mentioned[0]
    return result


def clause_credits_entity(
    outline: str,
    entity_name: str,
    evidence: str,
    credited: Optional[Dict[str, List[Tuple[int, int]]]] = None,
) -> bool:
    """证据是否真的能证明「**这个实体**做了这件事」。

    判据：证据的**起点**必须落在该实体被归属的小句里。

    - 「陈小拔剑」但 AI 说是李四 ⇒ 证据起点在陈小的小句 ⇒ **不成立** ⇒ 丢弃；
    - 「李明释放火球术，然后拔剑攻击」里 AI 说李明拔剑、证据是「拔剑攻击」⇒
      该小句按连接词沿用归给李明 ⇒ **成立** ✓；
    - 证据不在大纲里 ⇒ **不成立**。
    """
    if not outline or not entity_name or not evidence:
        return False
    start = outline.find(evidence)
    if start < 0:
        return False
    if credited is None:
        credited = assign_clause_spans(clause_spans(outline), [entity_name])
    ranges = credited.get(entity_name, [])
    return any(lo <= start < hi for lo, hi in ranges)
