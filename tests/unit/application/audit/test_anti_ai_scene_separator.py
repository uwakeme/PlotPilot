"""反 AI 扫描器：**场景分隔符不算 AI 味**。

背景 —— 真实跑一本真书（生产库）时实测到的**误报**：
第 1 章正文用「——」作**场景分隔符**（中文小说的标准排版，9 次）⇒
旧规则 ``(r"——", "破折号", ...)`` 是**无差别匹配** ⇒ 9 次命中
⇒ ``severity_score 44.3`` ⇒ 整章被判「**严重**」
⇒ **评分失真**（一章结构清楚、却被当成 AI 味重灾区）。

⇒ 规则改为：**只报行内的破折号**（两侧至少有一侧不是行边界）。
"""
from __future__ import annotations

import pytest

from application.audit.services.cliche_scanner import ClicheScanner

PATTERN = "破折号"


def _hits(text: str):
    return [h for h in ClicheScanner().scan_cliches(text) if h.pattern == PATTERN]


def test_an_inline_dash_is_still_reported():
    """行内破折号（插入语/对话）仍然要报。"""
    text = "他——顿了顿，把灯芯捻小了一点。"

    hits = _hits(text)

    assert hits, "行内破折号没被报出来"
    assert hits[0].text == "——"


def test_a_scene_separator_on_its_own_line_is_not_reported():
    """**独占一行**的「——」是场景分隔符（标准排版）⇒ 不算 AI 味。"""
    text = (
        "三份卷宗摞在案头。\n"
        "\n"
        "——\n"
        "\n"
        "入夜，巡捕房后门。\n"
        "\n"
        "——\n"
        "\n"
        "城南旧戏园区。\n"
    )

    assert _hits(text) == [], "场景分隔符被当成了 AI 味"


def test_a_dash_at_the_start_of_a_line_with_text_is_reported():
    """行首但后面还有内容的破折号 ⇒ 仍要报（它不是场景分隔符）。"""
    text = "——你早就知道了。\n"

    assert _hits(text), "行首接正文的破折号没被报出来"


def test_a_dash_at_the_end_of_a_line_is_reported():
    """行尾的破折号（省略号式停顿）⇒ 仍要报。"""
    text = "她没有说话——\n"

    assert _hits(text), "行尾破折号没被报出来"


def test_the_real_chapter_shape_scores_clean():
    """实测形态：一章里只有场景分隔符 ⇒ **不该**被判 AI 味。"""
    body = "三份卷宗摞在案头，牛皮纸袋口都封了火漆。"
    text = f"{body}\n\n——\n\n入夜，巡捕房后门。\n\n——\n\n城南旧戏园区。\n"

    assert _hits(text) == []
