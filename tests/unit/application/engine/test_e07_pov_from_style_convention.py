"""E-07（第 54 批）：叙事视角改由 AI 按题材定（三个正文提示词包 + 上下文实指）。

背景
----
AI **早就在定视角**：CPMS 节点 ``bible-style-convention`` 的第 1 维就是
「叙事视角：第几人称，限制视角还是全知视角」⇒ 那份公约落进 Bible 的
``style_notes`` ⇒ 再进正文提示词的**创作契约**槽位（硬事实三块之一）。

但三个正文提示词包**各自硬写**「第三人称限制视角」⇒ **两处会打架**
（AI 按题材定了第一人称，提示词却按铁律要求第三人称）⇒ AI 的决定被压掉
⇒ 这正是登记表 E-07 说的「POV 不许选」。

⇒ 现在：**视角以【文风与叙述公约】为准**（AI 说了算）；
公约没写明时才回落到第三人称限制视角（**默认值不丢**）。

本文件钉住
----------
1. 三个包都不许再把视角写成**硬法**（必须以文风与叙述公约为准）；
2. 三个包都保留「默认第三人称」兜底（不许把默认值也删掉 ——
   公约没写明时模型就没有任何 POV 指引）；
3. **引用不许悬空**：提示词点名的「【文风与叙述公约】」必须**真的**出现在
   正文提示词的上下文里（名字对不上 ⇒ 模型按名字找不到那块 ⇒ 空头支票）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
NODES = REPO_ROOT / "infrastructure" / "ai" / "prompt_packages" / "nodes"

#: 三个正文提示词包（各自硬写过「第三人称限制视角」）
PROSE_PACKAGES = (
    NODES / "prose-from-script" / "system.md",
    NODES / "chapter-generation-main" / "system.md",
    NODES / "workflow-chapter-generation" / "package.yaml",
)

#: 上下文里那块东西的**真实块名**（`format_style_notes_for_prompt` 渲染的标题）
STYLE_BLOCK_NAME = "【文风与叙述公约】"

#: 旧写法：把视角写成硬法（模型只能照第三人称限制视角写）
HARD_POV_MARK = "第三人称限制视角。你只能写"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", PROSE_PACKAGES, ids=lambda p: p.parent.name)
def test_the_pov_is_taken_from_the_style_convention_not_hardcoded(path: Path):
    """视角必须「以【文风与叙述公约】为准」，不许再是硬法。"""
    text = _text(path)

    assert "以%s为准" % STYLE_BLOCK_NAME in text, (
        f"{path.parent.name}: 叙事视角没有改成「以文风与叙述公约为准」"
    )
    assert HARD_POV_MARK not in text, (
        f"{path.parent.name}: 还把「第三人称限制视角」写成硬法 ⇒ AI 的决定会被压掉"
    )


@pytest.mark.parametrize("path", PROSE_PACKAGES, ids=lambda p: p.parent.name)
def test_the_default_third_person_fallback_stays(path: Path):
    """公约没写明时仍要有默认视角（不许把兜底也删掉）。"""
    text = _text(path)

    assert "默认第三人称" in text, (
        f"{path.parent.name}: 没有保留「默认第三人称」兜底 ⇒ 公约缺项时模型没有 POV 指引"
    )


def test_the_referenced_style_block_really_reaches_the_prose_context():
    """⚠ **不许悬空引用**：提示词点名的块必须真的在正文上下文里。

    （第一批写的是「以【文风公约】为准」，而上下文里的真实块名是
    「【文风与叙述公约】」⇒ 模型按名字找不到那块 ⇒ 这条守卫就是冲这件事来的。）
    """
    from domain.bible.entities.bible import Bible
    from domain.bible.entities.style_note import StyleNote
    from domain.novel.value_objects.novel_id import NovelId
    from application.world.services.narrative_contract_text import (
        build_narrative_contract_block,
    )

    bible = Bible(id="b1", novel_id=NovelId("n-1"))
    bible.add_style_note(
        StyleNote(
            id="sn-1",
            category="文风公约",
            content="叙事视角：第一人称。节奏偏慢，重内心戏。",
        )
    )

    block = build_narrative_contract_block(bible=bible)

    assert STYLE_BLOCK_NAME in block, (
        "正文上下文里没有「%s」这个块 ⇒ 提示词对它的引用是**悬空**的" % STYLE_BLOCK_NAME
    )
    assert "第一人称" in block, "文风公约的内容没进正文上下文 ⇒ AI 定的视角到不了模型"
