# -*- coding: utf-8 -*-
"""第 96 批：docx 导出**性能**与**排版保真**守卫。

⚠ 病灶（生产实测，600 章 / 111 万字）
------------------------------------
· 正文是「**一句一段 + 空行分隔**」的排版 ⇒ 111 万字 = **118316 行**（一半是空行）
· 旧写法逐行 ``add_paragraph`` ⇒ **163 秒**（1.38ms/段）
· ⇒ 大书导出 docx 会被前端 120 秒超时打断 ⇒ **根本导不出来**（epub 0.3s / pdf 10.4s / md 0.1s ⇒ 只有 docx 有问题）

守卫清单
--------
1. **排版保真**：导出的 docx 里 ``<w:br/>`` 的数量 == 原文的换行数
   ⇒ 不许为了快而把句间空行丢掉；
2. **段落数**：docx 的段落数 ≈ 章数 + 几个（**不是**行数）
   ⇒ 钉住「不许逐行建段落」；
3. **耗时上限**：导出 50 章要在宽松上限内完成（逐行写法会超）
   ⇒ 主断言是 ①②（确定性的），③ 只是辅助。
"""
from __future__ import annotations

import io
import time

import pytest

from application.core.services.export_service import ExportService


class _Char:
    def __init__(self, name):
        self.name = name


class _Novel:
    def __init__(self, title="测试书"):
        self.id = "novel-test"
        self.title = title
        self.author = "作者"
        self.premise = "简介"


class _Chapter:
    def __init__(self, number, title, content):
        self.id = "chapter-%s" % number
        self.number = number
        self.title = title
        self.content = content


class _Repo:
    def __init__(self, novel, chapters):
        self._novel = novel
        self._chapters = chapters

    def get_by_id(self, novel_id):
        return self._novel

    def list_by_novel(self, novel_id):
        return list(self._chapters)


def _chapters(count: int = 50):
    """造一批「一句一段 + 空行分隔」的章节（与生产正文同形）。"""
    out = []
    for i in range(1, count + 1):
        content = "\n\n".join(
            "这是第 %s 句正文，长度足够。" % j for j in range(1, 40)
        )
        out.append(_Chapter(i, "第 %s 章" % i, content))
    return out


def _export(chapters, fmt="docx"):
    svc = ExportService(
        novel_repository=_Repo(_Novel(), chapters),
        chapter_repository=_Repo(_Novel(), chapters),
    )
    started = time.time()
    data, _mime, _name = svc.export_novel("novel-test", fmt)
    return data, time.time() - started


def _break_count(docx_bytes: bytes) -> int:
    from docx import Document

    doc = Document(io.BytesIO(docx_bytes))
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    return len(doc.element.body.findall(".//%sbr" % ns))


def _paragraph_count(docx_bytes: bytes) -> int:
    from docx import Document

    doc = Document(io.BytesIO(docx_bytes))
    return len(doc.paragraphs)


def test_the_line_breaks_survive_the_export():
    """⚠ 排版必须**原样保留**（句间空行就是这本书的排版）。

    ⚠ 不许为了快就把换行丢掉 ⇒ 那会让 111 万字变成一堵墙。
    """
    chapters = _chapters(3)
    data, _ = _export(chapters)

    expected = sum((ch.content or "").count("\n") for ch in chapters)
    assert _break_count(data) == expected, (
        "docx 里的换行数=%s，原文换行数=%s ⇒ 排版被改动了"
        % (_break_count(data), expected)
    )


def test_the_export_does_not_build_one_paragraph_per_line():
    """段落数 ≈ 章数（**不是**行数）⇒ 不许逐行建段落。

    ⚠ 旧写法在这批数据上会产生 50 × 79 = 3950 个段落 ⇒ 导出要 5 秒以上
    ⇒ 大书的 118316 段落 = 163 秒 ⇒ 前端 120 秒超时 ⇒ 导不出来。
    """
    chapters = _chapters(50)
    data, _ = _export(chapters)

    paragraphs = _paragraph_count(data)
    assert paragraphs < len(chapters) * 3, (
        "段落数=%s（章数=%s）⇒ 又是逐行建段落 ⇒ 大书导出会超时"
        % (paragraphs, len(chapters))
    )


def test_the_export_is_fast_enough_for_the_ui_timeout():
    """⚠ 导出 50 章要在**宽松**上限内完成（前端默认超时 120 秒）。

    ⚠ 逐行写法在这批数据上要 5+ 秒；大书上是 163 秒。
    """
    chapters = _chapters(50)
    _data, elapsed = _export(chapters)

    assert elapsed < 15.0, (
        "导出 50 章用了 %.1fs ⇒ 逐行建段落没改掉 ⇒ 大书会超时" % elapsed
    )


def test_the_markdown_export_still_keeps_the_blank_lines():
    """⚠ 顺手钉住 markdown：句间空行也要在（它是「源稿」）。"""
    chapters = _chapters(2)
    data, _ = _export(chapters, fmt="markdown")
    text = data.decode("utf-8")
    assert "第 1 章" in text
    assert text.count("\n\n") >= len(chapters) * 10


def test_every_chapter_makes_it_into_the_export():
    """⚠ **完整性**：每一章都要在导出里（漏一章没人会发现）。

    ⚠ 600 章的书漏掉 1 章 ⇒ 用户拿到的手稿少一章。
    """
    chapters = _chapters(12)
    data, _ = _export(chapters)

    from docx import Document

    doc = Document(io.BytesIO(data))
    headings = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
    for ch in chapters:
        assert any(ch.title in h for h in headings), (
            "第 %s 章的标题没进导出 ⇒ 那一章被漏掉了" % ch.number
        )
