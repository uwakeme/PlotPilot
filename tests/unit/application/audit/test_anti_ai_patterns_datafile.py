# -*- coding: utf-8 -*-
"""第 77 批（A）守卫：反 AI 俗套规则的**数据文件化**。

背景
----
``cliche_scanner.py`` 里那 51 条正则原先**硬编码在源码里** ⇒ 改一条规则要改代码、
没法让用户按自己的偏好调；而分类学（F-12）已经在 ``config/anti_ai_taxonomy.yaml``
里当 SSOT 了 ⇒ **两份「事实源」并存的局面**（一条管「怎么判」、一条管「判出来算什么」，
但前者在代码里）。

⇒ 现在规则清单搬进 ``config/anti_ai_patterns.yaml``，代码只做转发导入。

本文件钉住
----------
1. **清单只有一个来源**（代码里不再留第二份）；
2. **搬运无损**：清单条数与**校验和**钉死 ⇒ 改任何一条都会红（要改就显式改 + 更新守卫）；
3. 每条正则**能编译**（YAML 引号没写错）；
4. 每条的**分类**都能在分类学词表里找到对应（F-12 双向口径）；
5. **真的还在用**：扫描器仍能命中已知俗套（接线没断）。
"""
from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import re

from application.audit.services.anti_ai_patterns import (
    AI_CLICHE_PATTERNS_ENHANCED,
    PATTERNS_PATH,
)
from application.audit.services.anti_ai_taxonomy import (
    class_id_for_scanner_category,
    load_taxonomy,
)

ROOT = pathlib.Path(__file__).resolve().parents[4]
SCANNER = ROOT / "application" / "audit" / "services" / "cliche_scanner.py"

#: 第 77 批搬运时的**清单校验和**（51 条）。
#: ⚠ 改 YAML 里的任何一条 ⇒ 这里会红 ⇒ 逼着人显式确认「这次改动是有意的」。
EXPECTED_COUNT = 51
EXPECTED_SHA256 = "4f9ebfee692c03e88da62a789f1316957330734b1f9ef82028718e0b8df84f16"


def _canonical_sha256(items) -> str:
    canonical = json.dumps(
        [[p[0], p[1], p[2], p[3], p[4]] for p in items],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_the_rules_live_in_the_data_file():
    """⚠ 清单的**唯一来源**是 YAML（代码里不再留第二份）。"""
    assert PATTERNS_PATH.is_file(), f"规则清单文件不存在：{PATTERNS_PATH}"

    tree = ast.parse(SCANNER.read_text(encoding="utf-8-sig"))
    literals = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "AI_CLICHE_PATTERNS_ENHANCED":
                    value = node.value
                    if isinstance(value, ast.List):
                        literals = len(value.elts)
    assert literals == 0, (
        f"cliche_scanner.py 里还有一份 {literals} 条的清单字面量 ⇒ "
        "清单有两个来源 ⇒ 迟早会漂"
    )


def test_the_list_is_unchanged_since_the_move():
    """⚠ 搬运必须无损：条数 + **校验和**钉死。"""
    assert len(AI_CLICHE_PATTERNS_ENHANCED) == EXPECTED_COUNT, (
        f"规则清单变成 {len(AI_CLICHE_PATTERNS_ENHANCED)} 条（期望 {EXPECTED_COUNT}）"
    )
    assert _canonical_sha256(AI_CLICHE_PATTERNS_ENHANCED) == EXPECTED_SHA256, (
        "规则清单内容变了（校验和不匹配）⇒ 搬运不再是「无损」，"
        "要么是误改、要么是有意改动（那就要显式更新本守卫）"
    )


def test_every_pattern_compiles():
    """⚠ YAML 引号写错 ⇒ 正则编译失败 ⇒ 整条规则**静默失效**。"""
    for regex, name, _severity, _category, _hint in AI_CLICHE_PATTERNS_ENHANCED:
        try:
            re.compile(regex)
        except re.error as exc:  # pragma: no cover —— 出错就是清单坏了
            raise AssertionError(f"规则「{name}」的正则编译失败：{exc}（{regex}）") from exc


def test_every_category_maps_to_the_taxonomy():
    """⚠ F-12 双向口径：机检的中文分类必须能落到分类学（A–K）上。"""
    # ⚠ 「只在代码侧出现」的分类是**词表里显式登记**的（code_only_categories）
    #   ⇒ 那是**允许**的，不是漏登记。
    code_only = set(load_taxonomy().code_only_categories)
    missing = sorted(
        {
            category
            for _regex, _name, _severity, category, _hint in AI_CLICHE_PATTERNS_ENHANCED
            if not class_id_for_scanner_category(category) and category not in code_only
        }
    )
    assert not missing, (
        f"这些分类在分类学词表里找不到对应（也不在 code_only_categories 里）：{missing}"
    )


def test_the_scanner_still_catches_a_known_cliche():
    """⚠ 接线没断：已知俗套仍要被命中（数据文件化不能改行为）。"""
    from application.audit.services.cliche_scanner import ClicheScanner

    scanner = ClicheScanner()
    hits = scanner.scan_cliches("他嘴角勾起一丝冷笑，眼底闪过一丝轻蔑。")

    assert hits, "扫描器一个都没命中 ⇒ 规则清单没被真正用上"
    categories = {hit.category for hit in hits}
    assert "微表情" in categories, categories
