"""端到端跑 `StateUpdater.update_from_chapter`，验证 G-01 的 source/note 落到产物。

为什么不用源码结构断言：第六轮复审指出
`test_state_updater_uses_the_real_update_function` 的「调用点在直接语句里」
无法用变异证明承重 —— 调用本来就在直接语句里，改用哪种遍历都找得到，
「挪进嵌套 def」这个绕过需要真的移动生产代码才能测。

改成跑真实链路：fake 掉**仓储**（唯一重依赖），其余全是真对象，
从 ChapterState 一路走到 Foreshadowing 上的 `resolve_offset_source/note`。
这样「决策函数被绕开 / 被内联 / source 被压平」都会被产物差异抓到。
"""
import pytest

from application.analyst.services.state_updater import StateUpdater
from domain.novel.entities.foreshadowing_registry import ForeshadowingRegistry
from domain.novel.value_objects.novel_id import NovelId
from domain.novel.value_objects.chapter_state import ChapterState


class _ForeshadowRepo:
    """只提供 G-01 那条路径用到的两个方法；真跑真实 registry。"""

    def __init__(self):
        self.registry = ForeshadowingRegistry(id="reg-1", novel_id=NovelId("n1"))
        self.saved = 0

    def get_by_novel_id(self, _novel_id):
        return self.registry

    def save(self, registry):
        self.saved += 1


class _UnusedRepo:
    """本用例不该碰到的依赖；被碰到就立刻炸，而不是静默 None。"""

    def __getattr__(self, name):
        raise AssertionError(f"本用例不应调用 {name}")


def _run(planted: dict):
    repo = _ForeshadowRepo()
    updater = StateUpdater(
        bible_repository=_UnusedRepo(),
        foreshadowing_repository=repo,
    )
    cs = ChapterState(
        new_characters=[],
        character_actions=[],
        relationship_changes=[],
        foreshadowing_planted=[planted],
        foreshadowing_resolved=[],
        events=[],
    )
    report = updater.update_from_chapter(
        novel_id="n1", chapter_number=3, chapter_state=cs
    )
    items = repo.registry.foreshadowings
    assert len(items) == 1, f"应产出 1 条伏笔，实际 {len(items)}"
    assert not report.unresolved
    return items[0]


@pytest.mark.parametrize(
    "planted,expect_source,expect_delta,expect_note_frag",
    [
        ({"description": "x", "chapter": 3, "importance": "medium",
          "suggested_resolve_offset": 7},
         "ai", 7, "采用模型给出的回收偏移"),

        ({"description": "x", "chapter": 3, "importance": "medium",
          "suggested_resolve_offset": 0},
         "ai_adjusted", 2, "钳到"),

        ({"description": "x", "chapter": 3, "importance": "medium",
          "suggested_resolve_offset": 40},
         "ai_adjusted", 30, "钳到"),

        ({"description": "x", "chapter": 3, "importance": "high"},
         "fallback", 15, "模型未给出回收偏移"),

        ({"description": "x", "chapter": 3, "importance": "medium",
          "suggested_resolve_offset": "十几章"},
         "fallback", 10, "不可解析"),
    ],
)
def test_update_from_chapter_end_to_end_source_and_note(
    planted, expect_source, expect_delta, expect_note_frag
):
    """真跑 update_from_chapter → 产物上的 source/note 必须与实际取值一致。"""
    import re

    f = _run(planted)
    assert f.resolve_offset_source == expect_source, (
        f"source={f.resolve_offset_source!r}，期望 {expect_source!r}"
    )
    assert f.suggested_resolve_chapter == 3 + expect_delta, (
        f"落库章数={f.suggested_resolve_chapter}，期望 {3 + expect_delta}"
    )
    assert expect_note_frag in f.resolve_offset_note, (
        f"note 缺 {expect_note_frag!r}：{f.resolve_offset_note!r}"
    )
    # note 里出现的偏移数字必须只有实际采用的那一个（落库文本，不是日志）
    nums = set(re.findall(r"\+(\d+)", f.resolve_offset_note))
    assert nums <= {str(expect_delta)}, (
        f"note 出现与实际取值不符的偏移数字：{nums}（实际 +{expect_delta}）"
    )
    if expect_source == "fallback":
        assert "兜底 5 章" not in f.resolve_offset_note, (
            f"fallback note 仍带裁决器的「按兜底 5 章」，与实际 +{expect_delta} 矛盾"
        )


class _StorylineRepo:
    """记录建档产物；G-03 的 source/note 必须出现在真 Storyline 上。"""

    def __init__(self):
        self.saved = []

    def save(self, storyline):
        self.saved.append(storyline)

    def get_by_novel_id(self, _novel_id):
        return list(self.saved)


def _run_storyline(ai_end, *, chapter_number=3):
    """走 G-03 那条路径（new_storylines），返回落库的那条 Storyline。"""
    repo = _StorylineRepo()
    updater = StateUpdater(
        bible_repository=_UnusedRepo(),
        foreshadowing_repository=_ForeshadowRepo(),
        storyline_repository=repo,
    )
    cs = ChapterState(
        new_characters=[],
        character_actions=[],
        relationship_changes=[],
        foreshadowing_planted=[],
        foreshadowing_resolved=[],
        events=[],
        new_storylines=[{"name": "主线", "type": "main", "description": "d",
                         "estimated_chapter_end": ai_end}],
    )
    updater.update_from_chapter(
        novel_id="n1", chapter_number=chapter_number, chapter_state=cs
    )
    assert len(repo.saved) == 1, f"应建档 1 条故事线，实际 {len(repo.saved)}"
    return repo.saved[0]


@pytest.mark.parametrize(
    "ai_end,expect_end,expect_source,expect_note_frag",
    [
        (40, 40, "ai", "模型给出的预估收尾章"),
        # 当前章 3，模型给 2 ⇒ 违反「收尾章 > 当前章」，抬到 4 ⇒ ai_adjusted
        (2, 4, "ai_adjusted", "已由代码抬到第 4 章"),
        (None, 15, "fallback", "按兜底 +12 章"),
        # ⚠ 第八轮复审：不可解析与非有限值**同类**，note 统一用「不可用」。
        # 这里只断言 note 非空且点明原因，具体措辞由 round8 守卫盯。
        ("十几章", 15, "fallback", ""),
    ],
)
def test_storyline_end_source_matches_the_persisted_value(
    ai_end, expect_end, expect_source, expect_note_frag
):
    """G-03 真跑 update_from_chapter → 产物上的取值/source/note 三者必须自洽。"""
    import re

    sl = _run_storyline(ai_end)
    assert sl.estimated_chapter_end == expect_end, (
        f"收尾章落库 {sl.estimated_chapter_end}，期望 {expect_end}"
    )
    assert sl.estimated_chapter_end_source == expect_source, (
        f"source={sl.estimated_chapter_end_source!r}，期望 {expect_source!r}"
    )
    assert sl.estimated_chapter_end_note, "note 不得为空"
    if expect_note_frag:
        assert expect_note_frag in sl.estimated_chapter_end_note, (
            f"note 缺 {expect_note_frag!r}：{sl.estimated_chapter_end_note!r}"
        )
    # note 里提到的章号必须就是真正落库的那个（否则是假 provenance）
    cited = set(re.findall(r"第 (\d+) 章", sl.estimated_chapter_end_note))
    assert cited <= {str(expect_end)}, (
        f"note 引用了未落库的章号 {cited}（实际落库 {expect_end}）"
    )


def test_storyline_end_is_never_labelled_ai_when_the_code_rewrote_it():
    """反向守卫：被代码抬过的值，source 绝不能是 ai。"""
    sl = _run_storyline(3)  # 模型给 3，不晚于当前章 3 ⇒ 抬到 4
    assert sl.estimated_chapter_end == 4
    assert sl.estimated_chapter_end_source == "ai_adjusted", (
        f"模型给 3、代码抬到 4，source 却是 {sl.estimated_chapter_end_source!r}"
    )


def test_storyline_end_is_never_labelled_ai_when_the_model_is_silent():
    """反向守卫：模型没给，source 绝不能是 ai。"""
    for raw in (None, "", "   ", "十几章"):
        sl = _run_storyline(raw)
        assert sl.estimated_chapter_end_source == "fallback", (
            f"模型未给有效值（{raw!r}），source 却是 "
            f"{sl.estimated_chapter_end_source!r}"
        )


@pytest.mark.parametrize(
    "chapter_number,ai_end,expect_end,expect_source",
    [
        (10, 42, 42, "ai"),          # 模型给了合法值 ⇒ 原值直用
        (10, 8, 11, "ai_adjusted"),  # 8 <= 当前章 10 ⇒ 抬到 11，不是模型原值
        (10, None, 22, "fallback"),  # 模型没给 ⇒ chapter + 12
        (10, "几十章", 22, "fallback"),  # 给了但不可解析 ⇒ 同样兜底
    ],
)
def test_cns_producer_persists_the_source_it_adjudicated(
    chapter_number, ai_end, expect_end, expect_source
):
    """G-03 **producer 写入路径**：`_make_storyline_from_progress_item` 真跑。

    ⚠ 为什么这条必须独立于落库往返测试：往返测试是**直接构造 Storyline 再存**，
    所以把 producer 里的 source 强制改成 `'ai'` 时它照样全绿。
    第五轮复审 §3.2 点名的正是这个洞 —— 「代码改写了取值却标成 ai」
    当初就是从 producer 进来的。

    ⚠ 本条只保留行为断言。原来那句
    ``assert "estimated_chapter_end_end" not in src`` 是无意义笔试守卫：
    它断言的是一个**从未存在过**的标识符拼写，任何实现都必然通过，
    删掉它不损失任何覆盖（第六轮复审 §3.5）。
    """
    from application.world.services import chapter_narrative_sync as cns

    sl = cns._make_storyline_from_progress_item(
        "n1", chapter_number, "支线", "婚约", "推进", ai_end)
    assert sl.estimated_chapter_end == expect_end, (
        f"producer 落库 {sl.estimated_chapter_end}，期望 {expect_end}"
    )
    assert sl.estimated_chapter_end_source == expect_source, (
        f"producer 把 source 标成 {sl.estimated_chapter_end_source!r}，"
        f"期望 {expect_source!r}"
    )
    assert sl.estimated_chapter_end_note, "producer 没写 provenance 说明"

    # ⚠ 第八轮复审 §3.2：原来只断言 note 非空，把 producer note 改成
    # 任意错误文案照样全绿。这里断言 note 必须**引用实际落库的章号**。
    import re

    cited = set(re.findall(r"第 (\d+) 章", sl.estimated_chapter_end_note))
    assert cited <= {str(expect_end)}, (
        f"producer note 引用了未落库的章号 {cited - {str(expect_end)}}："
        f"{sl.estimated_chapter_end_note!r}"
    )
    if expect_source == "ai":
        assert f"模型给出的预估收尾章 {expect_end}" in sl.estimated_chapter_end_note, (
            f"ai 分支的 note 应自述模型原值 {expect_end}：{sl.estimated_chapter_end_note!r}"
        )
    if expect_source == "fallback":
        assert "兜底" in sl.estimated_chapter_end_note, (
            f"fallback 分支的 note 必须点明是兜底：{sl.estimated_chapter_end_note!r}"
        )


def test_cns_producer_log_agrees_with_what_it_persisted(caplog):
    """producer 的日志措辞必须与落库 source 同向。

    日志与落库是同一个 ``source`` 变量，但两处**消费**分开 ——
    只断言落库值，日志被改成无差别措辞不会红。
    """
    import logging

    from application.world.services import chapter_narrative_sync as cns

    frags = {"ai": "采用模型判断",
             "ai_adjusted": "被代码改写",
             "fallback": "兜底"}
    for ai_end, expect in [(42, "ai"), (8, "ai_adjusted"), (None, "fallback")]:
        with caplog.at_level(
            logging.INFO, logger="application.world.services.chapter_narrative_sync"
        ):
            caplog.clear()
            sl = cns._make_storyline_from_progress_item(
                "n1", 10, "支线", "婚约", "推进", ai_end)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert any("收尾章" in m for m in
                   (r.getMessage() for r in caplog.records)), (
            f"ai_end={ai_end!r} 未产生收尾章日志：\n{text}")
        assert frags[expect] in text, (
            f"ai_end={ai_end!r} 的日志缺 {frags[expect]!r}：\n{text}")
        for other, frag in frags.items():
            if other != expect:
                assert frag not in text, (
                    f"ai_end={ai_end!r} 判为 {expect}，日志却出现 {other} 的措辞 "
                    f"{frag!r}：\n{text}"
                )
        assert sl.estimated_chapter_end_source == expect, (
            f"日志说 {expect}、落库是 {sl.estimated_chapter_end_source!r}"
        )


def test_update_from_chapter_reads_the_legacy_offset_key():
    """兼容旧键 `resolve_offset`（对齐键尚未上线时的输出）。"""
    f = _run({"description": "x", "chapter": 3, "importance": "low",
               "resolve_offset": 9})
    assert f.resolve_offset_source == "ai"
    assert f.suggested_resolve_chapter == 12


def test_new_key_wins_over_legacy_key():
    """两个键都存在时，新键 `suggested_resolve_offset` 优先。"""
    f = _run({"description": "x", "chapter": 3, "importance": "low",
               "suggested_resolve_offset": 7, "resolve_offset": 20})
    assert f.suggested_resolve_chapter == 10, "新键未优先"


def test_update_from_chapter_never_writes_ai_for_adjusted():
    """反向守卫：产物上的 source 绝不能把代码改写过的值说成 ai。"""
    for raw, delta in ((0, 2), (40, 30), (7.9, 7)):
        f = _run({"description": "x", "chapter": 3, "importance": "medium",
                  "suggested_resolve_offset": raw})
        assert f.suggested_resolve_chapter == 3 + delta
        assert f.resolve_offset_source != "ai", (
            f"{raw!r} 被改写成了 {3 + delta}，source 却是 'ai'"
        )
