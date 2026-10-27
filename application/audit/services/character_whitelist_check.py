"""K 类·越界角色的**确定性核对**（第 22 批）。

背景
----
K 类（越界角色）此前**只由 LLM 判**，没有任何机器口径 ⇒

1. LLM 把**白名单内**的角色报成越界 ⇒ 没人能驳回（面板上直接出现一个假越界角色）；
2. 「K 类**报得出来**」这件事在生产上**验不到** —— 实测那一章本来就没有越界角色
   （第 21 批：J/K 两类只验到「不误报」那一半）。

⚠ 为什么**不做**「全量检出白名单外角色」的机检
------------------------------------------------
中文具名实体识别在没有分词依赖的前提下**不可靠**。实测生产语料（593 章）：
「X说 / X道 / X问」这种说话人标注**极少**（对话多用「」+ 动作标注），
正则粗抽出来的候选全是「她头也不」「锁咔哒一」「一声留」这类碎片
⇒ 「姓 + 动词」口径的**召回接近 0**，而误报不可控。
⇒ 宁可**只核对已有结论**（集合运算，不依赖分词），不做全量检出。

口径
----
对审计报出的 **K 类命中**做**集合核对**：

- 白名单条目 → 展开成**可匹配的变体集合**：
  · 整串（「聂九枝（艺名玉惊鸿）」）
  · 括号前的裸名（「聂九枝」）
  · 括号里的别名（「玉惊鸿」「九爷」「闻聋子」「楚明谦」「贺松龄」）
  · 别名里的角色前缀要剥掉（「艺名玉惊鸿」→「玉惊鸿」「市井称闻聋子」→「闻聋子」）
  · 名部分（3 字名的后两字：「沈砚青」→「砚青」；**长度 < 2 不用** —— 太短会乱匹配）
- 命中名字落在变体集合里 ⇒ **驳回**（这是白名单内角色，LLM 误报）
- 名字不在集合里 ⇒ **确认**（白名单外角色）
- 白名单为空 / 名字解析不出来 / 形态像称谓 ⇒ **不下结论**（保守：保留原样，让人看）

⇒ 驳回**不许静默丢**（静默丢结论比误报更糟）⇒ 驳回项以
  ``越界角色·机检驳回`` 单独一条留在清单里，面板上可分辨。
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence

#: 白名单条目里可能出现的**角色前缀**（要剥掉才是别名本身）
ROLE_PREFIXES: tuple[str, ...] = (
    "市井称", "本名", "原名", "艺名", "法名", "道号", "笔名", "化名",
    "乳名", "外号", "又名", "又称", "俗称", "人称", "字", "号",
)

#: 引号类噪声（剥掉；**注意是「去掉」不是「两端剥」** —— 嵌套引号会露出来）
_QUOTE_NOISE = re.compile(r"[\"“”‘’'「」『』《》〈〉\[\]【】（）()\s]")

#: 名字里允许出现的**标点噪声**（只用于把命中原文剥成可比对的名字）
_PUNCT_NOISE = re.compile(r"[\s“”‘’\"'《》〈〉「」『』【】\[\]()（）:：,，。、!！?？…·—\-]")

#: K 类在审计输出里的类别写法（LLM 契约里是单个字母 "K"）
ROGUE_CATEGORY: str = "K"

#: K 类的中文标签（面板上显示用）
ROGUE_CATEGORY_LABEL: str = "越界角色"

#: 被机检**驳回**的越界角色所用的类别（**必须与真越界可分辨**）
REJECTED_CATEGORY: str = "越界角色·机检驳回"

#: 核对结论
CONFIRMED: str = "confirmed"
REJECTED: str = "rejected"
UNDECIDED: str = "undecided"

#: 名字末尾是这些**称谓/职衔**时 ⇒ 判不了（既可能是别名也可能是新角色）⇒ 不下结论
TITLE_SUFFIXES: tuple[str, ...] = (
    "探长", "队长", "班长", "先生", "小姐", "夫人", "太太", "老爷", "大爷",
    "大娘", "公子", "娘子", "师父", "师傅", "掌门", "门主", "总管", "掌柜",
    "和尚", "道士", "医生", "大夫", "警官", "警长", "所长", "局长", "科长",
    "处长", "师爷", "相公", "掌柜", "姑娘", "小子", "丫头", "老婆", "老公",
)

#: 名字最长允许多少字（含白名单名又带两三个字的形态，如「沈砚青妹」）
_MAX_NAME_CHARS = 12


def is_rogue_category(category: str) -> bool:
    """这一条命中是不是「K 类·越界角色」。

    ⚠ LLM 契约里 ``category`` 是单个字母（``"A"``…``"K"``），
    但也容忍模型写成 ``"K类"`` / ``"K 类·越界角色"``。
    """
    c = str(category or "").strip().upper()
    if not c:
        return False
    return c == ROGUE_CATEGORY or c.startswith(ROGUE_CATEGORY + "类") or c.startswith(
        ROGUE_CATEGORY + " 类"
    )


def _clean(text: str) -> str:
    """去掉引号与空白（**中间也去** —— 嵌套引号会露出来）。"""
    return _QUOTE_NOISE.sub("", str(text or ""))


def expand_whitelist(entries: Iterable[str]) -> set[str]:
    """把白名单条目展开成**可匹配的变体集合**。

    ⚠ 只保留**长度 ≥ 2** 的变体 —— 单字（姓、名的末字）会乱匹配。
    """
    variants: set[str] = set()
    for entry in entries:
        raw = str(entry or "").strip()
        if not raw:
            continue
        variants.add(raw)
        head = re.split(r"[（(]", raw, maxsplit=1)[0].strip()
        if head:
            variants.add(head)
        for segment in re.findall(r"[（(]([^）)]*)[）)]", raw):
            for piece in re.split(r"[、,，/／|]", segment):
                piece = _clean(piece)
                if not piece:
                    continue
                variants.add(piece)
                for prefix in ROLE_PREFIXES:
                    if piece.startswith(prefix) and len(piece) > len(prefix):
                        variants.add(piece[len(prefix):])
                        break
        # 名部分（3 字名的后两字）
        if len(head) >= 3:
            variants.add(head[-2:])
    return {v for v in variants if len(v) >= 2}


def normalize_name_text(text: str) -> str:
    """把命中原文剥成可比对的名字（去掉标点噪声）。"""
    return _PUNCT_NOISE.sub("", str(text or "")).strip()


def classify_rogue_character_hit(
    pattern: str, whitelist_variants: Sequence[str]
) -> tuple[str, str]:
    """核对一条 K 类命中 ⇒ ``(结论, 名字)``。

    - ``REJECTED``：指向的是**白名单内**的角色 ⇒ LLM 误报
    - ``CONFIRMED``：名字**不在**白名单里 ⇒ 确认越界
    - ``UNDECIDED``：白名单为空 / 名字解析不出来 / 形态像称谓 ⇒ 不下结论
    """
    variants = [v for v in (whitelist_variants or ()) if v]
    if not variants:
        return UNDECIDED, ""
    name = normalize_name_text(pattern)
    if not name or len(name) > _MAX_NAME_CHARS:
        return UNDECIDED, name
    if name in variants:
        return REJECTED, name
    # 名字里含白名单名（只允许多出两三个字：「沈砚青妹」算白名单内）
    for variant in variants:
        if variant in name and len(name) - len(variant) <= 2:
            return REJECTED, name
    # 末尾是称谓/职衔 ⇒ 判不了（郑探长 既可能是别名也可能是新角色）
    if any(name.endswith(suffix) for suffix in TITLE_SUFFIXES):
        return UNDECIDED, name
    return CONFIRMED, name
