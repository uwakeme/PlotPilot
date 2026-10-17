"""题材节拍模板不许只注入命中的第一条（E-02）。

改造前 `ThemedStoryPipeline._enrich_script_context` 按 priority 降序遍历，
命中第一条就 `break` —— 一章最多注入一个节拍模板。

这不是「优先级」，是「丢数据」。题材模板的关键词可能被两个模板同时认领。
实测（生产代码 `application/engine/theme/agents/*.py`，2026-10-17，11 个题材
共 48 套模板，脚本按 keyword 反查归属）：

| 题材 | 模板数 | 重叠关键词 |
|---|---|---|
| **wuxia** | 5 | **`灭门`** 同时属于「门派危机」(80) 与「血债复仇」(85) |

**wuxia 是唯一一例** —— 其余 10 个题材没有任何关键词被两个模板共享。
（曾以为 suspense 的「追踪」也是重叠，实测不成立：它只是「深入调查」的
关键词，「生死时速」是后者某条 beat 的描述文本，不是关键词。）

一章大纲同时写「灭门 + 寻宝 + 报仇」会命中三套模板，改造前只有优先级最高的
血债复仇进来，门派危机与寻宝两套节拍结构被静默丢弃 —— 模型根本不知道它们
存在，于是导演剧本里那一章的结构是残的。

本文件锁：
1. 命中几套就注入几套（不再 break）；
2. 注入时如实报出命中数量与各自命中的关键词；
3. 一套都不命中时什么都不注入（不许为了「有内容」而无条件塞节拍）。
"""
import sys
from types import SimpleNamespace

import pytest

from engine.pipeline.context import PipelineContext
from engine.pipelines.themed_pipeline import ThemedStoryPipeline


def _template(priority: int, keywords, beats):
    return SimpleNamespace(priority=priority, keywords=keywords, beats=beats)


def _beats(n: int, tag: str):
    return [(f"{tag}节拍{i}", 500 + i, "focus") for i in range(n)]


def _pipeline_with(templates, genre="wuxia"):
    """构造一个只关心 `_enrich_script_context` 的管线实例。"""
    pipe = ThemedStoryPipeline.__new__(ThemedStoryPipeline)
    agent = SimpleNamespace(get_beat_templates=lambda: templates)
    pipe._get_theme_agent = lambda: agent
    pipe._genre_key = genre
    return pipe


def _run(templates, outline: str) -> str:
    ctx = PipelineContext(novel_id="n-1", chapter_number=1, outline=outline)
    _pipeline_with(templates)._enrich_script_context(ctx)
    return ctx.context_text


# 三套模板，关键词故意重叠 —— 复刻真实的 wuxia 场景。
#
# ⚠️ 定义顺序**故意不按 priority 排**（75 → 85 → 80）。这样
# `sorted(..., reverse=True)` 才是承重逻辑：一开始把模板按 85/80/75 声明时，
# 去掉排序这行测试照样全绿（输入顺序恰好就是期望顺序），是条空洞测试。
TEMPLATES = [
    _template(75, ["寻宝", "秘籍", "宝藏", "藏宝图", "遗迹", "地宫"], _beats(4, "寻宝")),
    _template(85, ["复仇", "报仇", "血债", "灭门", "寻仇"], _beats(4, "复仇")),
    _template(80, ["门派", "帮会", "掌门", "叛变", "灭门", "内乱"], _beats(4, "门派")),
]


def test_all_matched_templates_are_injected_not_just_the_first():
    """核心：命中 3 套就注入 3 套。"""
    outline = "门派遭灭门，主角下山寻仇，沿途寻得秘籍。"
    out = _run(TEMPLATES, outline)

    assert "复仇节拍" in out
    assert "门派节拍" in out, "优先级更低不代表不适用，被静默丢弃了"
    assert "寻宝节拍" in out


def test_injection_reports_how_many_matched():
    """命中数量必须如实报出，模型才知道自己拿到几套可选。"""
    out = _run(TEMPLATES, "门派遭灭门，主角下山寻仇，沿途寻得秘籍。")

    assert "命中 3 套" in out


def test_each_template_reports_the_keywords_that_matched():
    """报出命中关键词，才能解释为什么这一套被选中。

    必须断言**每个 section 各自带关键词标注**。只断言「灭门 出现在输出里」
    是空洞的 —— 抬头那行的「优先级 85（命中：灭门、寻仇）」里同样有这两个词，
    去掉 section 标注测试照样全绿。
    """
    out = _run(TEMPLATES, "门派遭灭门，主角下山寻仇，沿途寻得秘籍。")

    assert out.count("### 命中关键词：") == 3, "每套模板都要自带命中关键词标注"
    # 同一套里命中多个词时也要全列出来，不是只取第一个
    assert "灭门、寻仇" in out
    assert "灭门" in out


def test_templates_are_ordered_by_priority():
    """仍按 priority 降序 —— 顺序是提示，不是筛选。"""
    out = _run(TEMPLATES, "门派遭灭门，主角下山寻仇，沿途寻得秘籍。")

    assert out.index("复仇节拍") < out.index("门派节拍") < out.index("寻宝节拍")


def test_single_match_still_injects_exactly_one():
    """只命中一套时，措辞仍要说「1 套」，不能沿用复数的说法。"""
    out = _run(TEMPLATES, "主角在集市闲逛。")

    assert "节拍指令" not in out


def test_single_real_match_reports_one():
    out = _run(TEMPLATES, "主角偶得秘籍一部。")

    assert "命中 1 套" in out
    assert "寻宝节拍" in out
    assert "门派节拍" not in out


def test_no_match_injects_nothing():
    """一套都不命中时什么都不许加 —— 不能为了「有内容」而无条件塞节拍。"""
    out = _run(TEMPLATES, "他在书房读了一卷书。")

    assert out == ""


def test_model_is_told_it_may_choose_among_the_matched_sets():
    """取舍权交回模型：明说「由你判断取舍与融合顺序」。"""
    out = _run(TEMPLATES, "门派遭灭门，主角下山寻仇，沿途寻得秘籍。")

    assert "由你判断取舍与融合顺序" in out


def test_shared_keyword_routes_to_every_template_that_owns_it():
    """`灭门` 同时属于两套模板 —— 这是修复前被 break 吃掉的那一类。"""
    out = _run(TEMPLATES, "一夜之间灭门。")

    assert "命中 2 套" in out
    assert "复仇节拍" in out and "门派节拍" in out
    assert "寻宝节拍" not in out


def test_empty_keyword_does_not_match_everything():
    """空关键词会让 `kw in outline` 恒真 —— 一个空串就能命中所有模板。"""
    templates = [_template(90, [""], _beats(2, "空词"))]
    out = _run(templates, "任意大纲")

    assert out == ""
