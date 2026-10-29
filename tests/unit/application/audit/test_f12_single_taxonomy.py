"""F-12：「AI 味」分类学的**单一事实源**守卫。

背景
----
改造前同一套审美标准有**两份**：扫描器（51 条正则，分类是中文标签）＋
提示词（11 大类别 A–K，输出 JSON 的 ``category`` 就是 ``"A/B/…/K"``）⇒
**机检报「微表情」、模型报「A」⇒ 两侧输出不可比**。

本文件钉住
----------
1. **扫描器的每个分类都必须在词表里登记**（映射到 A–K，或显式登记为 code_only）
   ⇒ 新增一个扫描器分类却忘了登记 ⇒ 红（两份分类学又漂了）。
2. **词表与提示词逐字一致**（双向）：类 id + 中文名必须逐字出现在提示词里；
   提示词里出现的每个类 id 必须在词表里。
3. **提示词的输出契约**里 ``category`` 的取值集合 == 词表的类 id 集合
   （模型被允许报的值 == 词表里的值）。
4. **接线**：真扫一句俗套 ⇒ 命中带上正确的类 id，而**旧的** ``category``
   中文标签**保持不变**（不破坏既有的建议生成逻辑）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from application.audit.services.anti_ai_taxonomy import (
    TAXONOMY_PATH,
    load_taxonomy,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
PROMPT_PATH = (
    REPO_ROOT
    / "infrastructure"
    / "ai"
    / "prompt_packages"
    / "nodes"
    / "anti-ai-chapter-audit"
    / "system.md"
)

#: 提示词里类目标题的正则（形如「【A类·微表情滥用】」）
_CLASS_HEADER_RE = re.compile(r"【([A-Z])类·([^】]+)】")


def _prompt_classes() -> dict[str, str]:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    return {m.group(1): m.group(2).strip() for m in _CLASS_HEADER_RE.finditer(text)}


def _scanner_categories() -> set[str]:
    """扫描器实际会产出的全部分类标签（正则表 ＋ 英文词检查）。"""
    from application.audit.services.cliche_scanner import ClicheScanner

    cats: set[str] = set()
    for item in ClicheScanner().compiled_patterns:  # type: ignore[attr-defined]
        if len(item) == 5:
            cats.add(str(item[3]))
    # 英文词检查那条不在正则表里（跨全文语义），单独登记
    cats.add("语言混杂")
    return cats


# ─────────────────────── 1) 扫描器分类必须已登记 ───────────────────────


def test_every_scanner_category_is_registered_in_the_taxonomy():
    """扫描器产出的每个分类都必须在词表里（映射到类目，或显式 code_only）。"""
    taxonomy = load_taxonomy()
    known = set(taxonomy.known_scanner_categories())

    missing = sorted(c for c in _scanner_categories() if c not in known)
    assert not missing, (
        f"扫描器有这些分类没在 {TAXONOMY_PATH.name} 登记: {missing}；"
        "要么映射到某个类目，要么显式登记进 code_only_categories"
    )


# ─────────────────────── 2) 词表 ↔ 提示词 双向逐字一致 ───────────────────────


def test_the_taxonomy_matches_the_prompt_headers_exactly():
    """词表里的每个类 id + 中文名，必须在提示词里**逐字**出现。"""
    prompt_classes = _prompt_classes()
    taxonomy = load_taxonomy()

    mismatched = []
    for cls in taxonomy.classes:
        actual = prompt_classes.get(cls.id)
        if actual is None:
            mismatched.append(f"{cls.id}（提示词里没有这一类）")
        elif actual != cls.label:
            mismatched.append(f"{cls.id}（提示词={actual!r} 词表={cls.label!r}）")
    assert not mismatched, f"词表与提示词的类目不一致：{mismatched}"


def test_the_prompt_has_no_class_outside_the_taxonomy():
    """提示词里出现的每个类 id 必须在词表里（词表不许多类）。"""
    prompt_classes = _prompt_classes()
    known = set(load_taxonomy().class_ids)

    extra = sorted(cid for cid in prompt_classes if cid not in known)
    assert not extra, f"提示词里这些类目不在词表里：{extra}"


def test_the_prompt_output_contract_allows_exactly_the_taxonomy_ids():
    """提示词输出 JSON 的 ``category`` 取值集合 == 词表的类 id 集合。"""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    m = re.search(r'"category"\s*:\s*"([^"]+)"', text)
    assert m, "提示词的输出契约里找不到 category 字段"
    allowed = {part.strip() for part in m.group(1).split("/") if part.strip()}

    assert allowed == set(load_taxonomy().class_ids), (
        f"提示词允许的 category 取值 {sorted(allowed)} 与词表 {sorted(load_taxonomy().class_ids)} 不一致"
    )


# ─────────────────────── 3) 接线：命中真的带上类 id ───────────────────────


@pytest.mark.asyncio
async def test_a_machine_hit_carries_the_same_class_id_as_the_prompt():
    """机检命中要带上类 id（A–K），而**旧的中文分类标签保持不变**。"""
    from application.audit.services.cliche_scanner import ClicheScanner

    scanner = ClicheScanner()
    hits = scanner.scan_cliches("他眼中闪过一丝不屑，嘴角勾起一抹冷笑。")

    assert hits, "没扫出任何命中 ⇒ 样本失效"
    hit = hits[0]
    assert hit.category == "微表情", "旧的中文分类标签变了（会打破既有建议逻辑）"
    assert hit.taxonomy_class == "A", (
        f"机检命中没有带上类 id（实际 {hit.taxonomy_class!r}）⇒ 与提示词口径不可比"
    )


@pytest.mark.asyncio
async def test_code_only_categories_have_no_class_id():
    """只在代码侧出现的分类（语言混杂）**没有**类 id（不猜）。"""
    from application.audit.services.cliche_scanner import ClicheScanner

    scanner = ClicheScanner()
    hits = scanner.scan_cliches("他推开门，The door was closed.")

    english_hits = [h for h in hits if h.category == "语言混杂"]
    assert english_hits, "没扫出英文夹带 ⇒ 样本失效"
    for hit in english_hits:
        assert hit.taxonomy_class == "", (
            "语言混杂在词表里是 code_only ⇒ 不该有类 id"
        )
