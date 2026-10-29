"""F-12：「AI 味」分类学的**单一事实源**（类目词表 + 扫描器分类的映射）。

为什么需要
----------
改造前，同一套审美标准有**两份**互不相干的定义：

1. ``application/audit/services/cliche_scanner.py`` 的 51 条正则，每条带一个
   **中文分类**（微表情 / 声线 / 比喻 / 生理 / 句式 / 俗套 / 情绪 / 严禁词）
   ＋ 英文词检查的「语言混杂」；
2. ``infrastructure/ai/prompt_packages/nodes/anti-ai-chapter-audit/system.md``
   的 **11 大类别**（A–K），其输出 JSON 契约里 ``category`` 写的就是
   ``"A/B/C/D/E/F/G/H/I/J/K"``。

⇒ 机检报「微表情」、模型报「A」⇒ **审阅时没法把机检结果映射到提示词的评分口径**
⇒ 这就是登记表 F-12 说的「分类学双份、可互相矛盾」。

现在
----
``config/anti_ai_taxonomy.yaml`` 是**类目词表的唯一来源**：

* 扫描器的每条命中带上 ``taxonomy_class``（A–K）⇒ 与模型输出**同一套口径**；
* 只在代码侧出现的分类（泛用桶 / 专用检查）**显式登记**在 ``code_only_categories``
  ⇒ 不是「悄悄漏掉」；
* 提示词里的类目标题必须与本文件**逐字一致**（守卫双向盯着）。

⚠ 正则模式（那 51 条）**仍在代码里** —— 正则是「怎么判」，分类学是「判出来算什么」；
把正则搬进数据文件会动 51 条行为，不在本条范围内。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

#: 词表文件（仓库内唯一来源）
TAXONOMY_PATH = Path(__file__).resolve().parents[3] / "config" / "anti_ai_taxonomy.yaml"


@dataclass(frozen=True)
class TaxonomyClass:
    """一个类目（A–K）。"""

    id: str
    label: str
    code_checkable: bool
    scanner_categories: Tuple[str, ...]


@dataclass(frozen=True)
class AntiAITaxonomy:
    """「AI 味」分类学词表。"""

    classes: Tuple[TaxonomyClass, ...]
    code_only_categories: Tuple[str, ...]

    @property
    def class_ids(self) -> Tuple[str, ...]:
        return tuple(c.id for c in self.classes)

    @property
    def labels(self) -> Dict[str, str]:
        return {c.id: c.label for c in self.classes}

    def scanner_class_map(self) -> Dict[str, str]:
        """扫描器分类 → 类 id（A–K）。"""
        out: Dict[str, str] = {}
        for c in self.classes:
            for name in c.scanner_categories:
                out[name] = c.id
        return out

    def known_scanner_categories(self) -> Tuple[str, ...]:
        """全部「已登记」的扫描器分类（映射到类目的 ＋ 代码侧专有的）。"""
        return tuple(self.scanner_class_map().keys()) + self.code_only_categories

    def class_id_for(self, scanner_category: str) -> str:
        """扫描器分类 → 类 id；没登记 ⇒ 空串（**不是**猜一个）。"""
        return self.scanner_class_map().get(str(scanner_category or "").strip(), "")


_lock = threading.Lock()
_cached: Optional[AntiAITaxonomy] = None


def load_taxonomy() -> AntiAITaxonomy:
    """读词表（带缓存；读不到 ⇒ 空词表，不猜）。"""
    global _cached
    with _lock:
        if _cached is not None:
            return _cached
        classes: List[TaxonomyClass] = []
        code_only: List[str] = []
        try:
            raw = yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            raw = {}
        for item in raw.get("classes") or []:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            label = str(item.get("label") or "").strip()
            if not cid or not label:
                continue
            classes.append(
                TaxonomyClass(
                    id=cid,
                    label=label,
                    code_checkable=bool(item.get("code_checkable", False)),
                    scanner_categories=tuple(
                        str(x).strip()
                        for x in (item.get("scanner_categories") or [])
                        if str(x).strip()
                    ),
                )
            )
        for item in raw.get("code_only_categories") or []:
            name = str(item.get("name") if isinstance(item, dict) else item or "").strip()
            if name:
                code_only.append(name)
        _cached = AntiAITaxonomy(classes=tuple(classes), code_only_categories=tuple(code_only))
        return _cached


def class_id_for_scanner_category(category: str) -> str:
    """扫描器分类 → 类 id（A–K）；未登记 ⇒ 空串。"""
    return load_taxonomy().class_id_for(category)


def taxonomy_class_ids() -> Tuple[str, ...]:
    """全部类 id（A–K）。"""
    return load_taxonomy().class_ids
