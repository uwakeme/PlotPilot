"""N-1：中文正文里夹带的英文单词必须被**机检**出来。

背景 —— 真实跑一本真书时实测到：第 1 章正文里出现
「她坐在最后一排，膝头压着 **notebook**，屏幕亮起时连片名都像隔着一层水。」
同一处上下文别处都写「记事本」，**执行剧本里写的也是「记事本」**
⇒ 是模型自己把词换成了英文。

而反 AI 审计给的结论是 ``risk_flags=0``（= 没抓到）——
「审计没有『中文正文夹带英文词』这条口径」。

⚠ 本文件守的是**代码通道**（``ClicheScanner``）：
   命中后 ``total_hits`` / ``severity_score`` / ``overall_assessment`` /
   落库 / API **全部自动生效**（因为它产出的是标准 ``ClicheHit``）。

⚠ 已知边界（如实写清，不过度声称）：
   * ``risk_flags`` 那个字段**恒为空** —— LLM 通道的 JSON 契约里
     根本没有 ``risk_flags`` 键（``continuations.py`` 读 ``payload.get("risk_flags", [])``）
     ⇒ 这是**另一个独立缺陷**，不在本条范围内（本条只管代码通道）；
   * 专名白名单是**封闭清单** ⇒ 不在表里的专名会被标记（info 级，代价低）。
"""
from __future__ import annotations

import pytest

from application.audit.services.anti_ai_audit import AntiAIAuditor
from application.audit.services.cliche_scanner import (
    ENGLISH_PROPER_NOUN_ALLOWLIST,
    ClicheScanner,
)

PATTERN = "中文正文夹带英文词"


def _hits(text: str):
    return [h for h in ClicheScanner().scan_cliches(text) if h.pattern == PATTERN]


# ======================================================================
#  A. 正例：真实跑章抓到的那一句
# ======================================================================


def test_the_real_chapter_sentence_is_caught():
    """真实跑章里那句「膝头压着 notebook」必须被抓到。"""
    text = (
        "昨夜放映厅里骤然熄灭的灯光，短暂浮上林见秋眼前。她坐在最后一排，"
        "膝头压着 notebook，屏幕亮起时连片名都像隔着一层水。"
    )

    hits = _hits(text)

    assert hits, "中文正文里夹带的英文词没被抓到"
    hit = hits[0]
    assert hit.text == "notebook"
    assert hit.severity == "info", "严重性必须是 info（低噪声规则，用 warning 会让每章凭空跳级）"
    assert hit.category == "语言混杂"
    assert hit.start >= 0 and hit.end > hit.start
    assert text[hit.start:hit.end] == "notebook"


# ======================================================================
#  B. 反例：不许误伤
# ======================================================================


def test_pure_chinese_prose_is_not_flagged():
    """纯中文正文 ⇒ 0 命中（既有守卫 ``test_no_cliches_returns_empty`` 也要继续成立）。"""
    text = "他走进房间，坐下来，开始工作。她把灯拧暗，翻开那本记事本。"

    assert _hits(text) == []


def test_a_pure_foreign_paragraph_is_not_flagged():
    """纯外文段落（英文信函 / 外文引文）⇒ 不是「夹带」。

    ⚠ 判据是「词的左右各 8 字内有没有中文」⇒ 整段外文时不命中。
    """
    text = "Dear Mr. Smith,\n\nThank you for your letter. The archive will be open on Monday.\n\nYours sincerely,\nJohn"

    assert _hits(text) == []


def test_english_inside_quotes_is_not_flagged():
    """引号/书名号里的外文是**引文**，不是夹带。"""
    text = "她在稿纸上写下一行字：“Nothing is ever written in my own hand.”随后划掉了它。"

    assert _hits(text) == []


def test_known_proper_nouns_are_not_flagged():
    """白名单里的专名/缩写不报。"""
    text = "FBI 的卷宗、DNA 报告与 OK 邮件都摆在桌上。"

    assert _hits(text) == []
    for token in ("fbi", "dna", "ok"):
        assert token in ENGLISH_PROPER_NOUN_ALLOWLIST


def test_a_repeated_word_is_reported_but_with_a_different_hint():
    """反复出现的词仍会报，但提示语要区分「可能是专名」。"""
    text = "他把 notebook 合上，又把 notebook 推回桌上。"

    hits = _hits(text)

    assert len(hits) == 2
    assert "专名" in hits[0].replacement_hint, hits[0].replacement_hint


# ======================================================================
#  C. 指标链路：命中后分数/评级自动生效
# ======================================================================


def test_the_hit_flows_into_the_audit_metrics():
    """命中必须进 ``total_hits`` 与 ``severity_score``（否则这条规则等于没加）。"""
    from application.audit.services.anti_ai_thresholds import SEVERITY_WEIGHTS

    content = "她膝头压着 notebook，屏幕亮起时连片名都像隔着一层水。" + "灯影摇曳。" * 200
    baseline = "她膝头压着记事本，屏幕亮起时连片名都像隔着一层水。" + "灯影摇曳。" * 200

    auditor = AntiAIAuditor()
    with_english = auditor.scan_chapter("c1", content)
    without_english = auditor.scan_chapter("c1", baseline)

    assert with_english.metrics.total_hits == without_english.metrics.total_hits + 1
    # ⚠ 归一化口径是「**每千字**加权命中数」（第 16 批改的）⇒ 期望值用 1000 而不是 10000。
    expected_delta = (
        SEVERITY_WEIGHTS["info"] * 1000 / max(len(content), 1)
    )
    delta = with_english.metrics.severity_score - without_english.metrics.severity_score
    assert abs(delta - expected_delta) < 1.0, (
        f"分数增量 {delta} 与 info 权重算出来的 {expected_delta} 对不上"
    )


def test_the_existing_clean_sample_stays_clean():
    """既有守卫的样本必须仍然 0 命中（不许为了新规则把老用例打翻）。"""
    text = "他走进房间，坐下来，开始工作。"

    assert ClicheScanner().scan_cliches(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "",
        "。",
        "她说：好。",
    ],
)
def test_degenerate_inputs_do_not_crash(text: str):
    """空/极短输入不许炸。"""
    assert _hits(text) == []
