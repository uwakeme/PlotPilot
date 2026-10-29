"""第 42 批：事实锁的「核心事件时间线」—— **只送最近的** + **带上完整字段** + **如实留痕**。

背景（生产实测，长书 529 章那本）
----------------------------------
三个叠在一起的缺陷：

1. **字段残缺**：每行只有 ``event``（中位 **13 字**，「陆深抉择补全」那种）
   ⇒ 而 ``description``（生产书 **100% 有值**）与 ``time_point``（**100% 有值**）
   **从来没进过模型** ⇒ 模型拿到的是最碎的那个字段；
2. **砍错位置**：时间线是事实锁的**最后一节** ⇒ 预算紧时**最先被砍**
   ⇒ 而最近的事件在**列表尾部** ⇒ **最该保留的先被砍掉**（最坏的砍法）；
3. **砍了不说**：2074 条只给一部分，却**没有任何提示**说还有更多。

⇒ 实测效果（改前 → 改后）：
* 长书：事实锁 **38911 → 6522 字**（时间线 36618 → 4229 字，2074 条 → 65 条）
  ⇒ **不再触发任何截断** ⇒ 硬约束 100% 送达（这条是**根因**，第 24 批埋的）；
* 小书：事实锁 4667 → **5719 字**（21 条时间线首次带上 time_point/description）。

⚠ 守卫纪律：期望值**写死字面量**；走**真实链路**；
  样本形状按**生产实测**造（长书 = 8 角色 + 2074 条时间线）。
"""
from __future__ import annotations

from types import SimpleNamespace

from application.engine.services.memory_engine import (
    TIMELINE_DESC_MAX_CHARS,
    TIMELINE_EVENT_MAX_CHARS,
    TIMELINE_MAX_CHARS,
    TIMELINE_MAX_ENTRIES,
    TIMELINE_TIME_POINT_MAX_CHARS,
    FactLockBuilder,
)

# ── 口径值（写死 ⇒ 改常量必红）────────────────────────────────────────
#: 时间线的字符预算（第 42 批）
TIMELINE_CHAR_BUDGET = 4000
#: 时间线的条数兜底
TIMELINE_ENTRY_CAP = 200
#: 单条「事件」限长
EVENT_MAX_CHARS = 40
#: 单条「时间点」限长
TIME_POINT_MAX_CHARS = 20
#: 单条「事件描述」限长
DESC_MAX_CHARS = 60


class _Char:
    def __init__(self, name, **kw):
        self.name = name
        self.description = kw.get("description", "")
        self.relationships = kw.get("relationships", [])
        self.public_profile = kw.get("public_profile", "")
        self.hidden_profile = kw.get("hidden_profile", "")
        self.reveal_chapter = kw.get("reveal_chapter", None)
        self.mental_state = kw.get("mental_state", "")
        self.gender = kw.get("gender", "")
        self.age = kw.get("age", "")


class _Bible:
    def __init__(self, characters, timeline_notes=None):
        self.characters = characters
        self.timeline_notes = timeline_notes or []
        self.locations = []


def _builder():
    return FactLockBuilder.__new__(FactLockBuilder)


def _note(index: int, event: str = "", time_point: str = "", description: str = ""):
    """一条时间线（形状对齐生产：``event`` / ``time_point`` / ``description``）。"""
    return SimpleNamespace(
        id=f"tl-{index:05d}",
        event=event or f"第{index}件事发生了",
        time_point=time_point or "同夜",
        description=description or f"第{index}件事的细节描述，说明它怎么发生、影响了谁。",
    )


def _notes(count: int):
    return [_note(i) for i in range(1, count + 1)]


def _long_notes(count: int):
    """条目**长**的样本（每条约 95 字）。

    ⚠ 为什么需要它：条数兜底（200 条）会**先**把样本截住
    ⇒ 若条目很短（15 字），200 × 15 = 3000 字 < 字符预算 ⇒
    「字符预算被拆掉」这个变异**抓不到**（条数兜底挡住了）
    ⇒ ⇒ 必须用长条目，让**条数兜底单独扛不住** ⇒ 字符预算才真的是硬约束。
    """
    return [
        SimpleNamespace(
            id=f"tl-{index:05d}",
            event=f"第{index}件事发生了而且牵涉到好几个人",
            time_point="同夜九十秒倒计时内",
            description=(
                f"第{index}件事的完整经过：起因、经过、结果都写清楚，"
                "并且说明它影响了谁、留下了什么后果。"
            ),
        )
        for index in range(1, count + 1)
    ]


# ── ① 口径钉住 ─────────────────────────────────────────────────────────


def test_the_timeline_budget_is_pinned():
    """⚠ 四个口径**写死** ⇒ 改被测模块的常量必红（否则是假绿）。"""
    assert TIMELINE_MAX_CHARS == TIMELINE_CHAR_BUDGET
    assert TIMELINE_MAX_ENTRIES == TIMELINE_ENTRY_CAP
    assert TIMELINE_EVENT_MAX_CHARS == EVENT_MAX_CHARS
    assert TIMELINE_TIME_POINT_MAX_CHARS == TIME_POINT_MAX_CHARS
    assert TIMELINE_DESC_MAX_CHARS == DESC_MAX_CHARS


# ── ② 只送最近的（尾部），不是最早的头部 ──────────────────────────────


def test_the_timeline_keeps_the_most_recent_entries():
    """⚠ **最该保留的先被砍掉**是第 42 批修掉的核心缺陷。

    ⇒ 生产实测：列表顺序 = 写入顺序 = 故事顺序（id 字典序单调递增）
    ⇒ 「尾部」= 最近 ⇒ 必须取尾部。

    ⚠ 样本必须**超过**字符预算（否则全送得下 ⇒ 这条验的是空气）：
      每条约 37 字 ⇒ 300 条 ≈ 11100 字 ⇒ 预算只吃下尾部约 1/3。
    """
    builder = _builder()
    bible = _Bible([], timeline_notes=_notes(300))

    lines = builder._build_timeline_lines(bible)

    assert "第300件事发生了" in "\n".join(lines), "没送到**最后**一条 ⇒ 取的不是尾部"
    assert "第1件事发生了" not in "\n".join(lines), "最早的那条还在 ⇒ 取的不是尾部"


def test_the_kept_entries_keep_the_story_order():
    """⚠ 送出来的时间线必须仍是**故事顺序**（模型读的是历史，不该倒着读）。"""
    builder = _builder()
    bible = _Bible([], timeline_notes=_notes(30))

    lines = builder._build_timeline_lines(bible)
    events = [line for line in lines if line.startswith("第")]
    numbers = [int(line[1:].split("件")[0]) for line in events]

    assert numbers == sorted(numbers), f"时间线顺序乱了：{numbers}"
    assert numbers[-1] == 30, numbers


# ── ③ 三个字段都送（此前只有最碎的 event）──────────────────────────────


def test_the_timeline_carries_time_point_and_description():
    """⚠ 生产实测：``time_point`` / ``description`` **100% 有值**
    ⇒ 此前它们**从来没进过模型** ⇒ 模型只拿到 13 字的碎事件。"""
    builder = _builder()
    bible = _Bible(
        [],
        timeline_notes=[
            SimpleNamespace(
                id="tl-1",
                event="封蜡融化倒计时启动",
                time_point="离案即始，限期一夜",
                description="蜡面泛起水纹、边缘下淌，融尽即启即投递。",
            )
        ],
    )

    line = builder._build_timeline_lines(bible)[0]

    assert "封蜡融化倒计时启动" in line, line
    assert "离案即始，限期一夜" in line, f"时间点没送：{line}"
    assert "蜡面泛起水纹" in line, f"事件描述没送：{line}"


def test_a_long_single_entry_is_capped():
    """⚠ 单条超长 ⇒ **限长 + 留痕**（不许静默截断 ⇒ 模型会当成完整事件）。"""
    builder = _builder()
    bible = _Bible(
        [],
        timeline_notes=[
            SimpleNamespace(
                id="tl-1",
                event="事" * 100,
                time_point="点" * 100,
                description="描" * 200,
            )
        ],
    )

    line = builder._build_timeline_lines(bible)[0]

    assert "…" in line, "超长字段没有留痕"
    assert "事" * 100 not in line, "事件没被限长"
    assert "点" * 100 not in line, "时间点没被限长"
    assert "描" * 200 not in line, "描述没被限长"


# ── ④ 按字符预算收口 + 如实留痕 ───────────────────────────────────────


def test_the_timeline_respects_its_char_budget():
    """⚠ 条数只是兜底，**字符预算**才是硬约束（与蓝图侧同一口径）。

    ⚠ 样本用**长条目**（每条约 95 字）⇒ 200 条的条数兜底单独扛不住
    （200 × 95 ≈ 19000 字 ≫ 预算）⇒ 这条才能真正盯住「字符预算」这一层。
    """
    builder = _builder()
    bible = _Bible([], timeline_notes=_long_notes(300))

    lines = builder._build_timeline_lines(bible)
    body = "\n".join(lines)

    assert len(body) <= TIMELINE_CHAR_BUDGET + 200, (
        f"时间线节 {len(body)} 字，超出预算 {TIMELINE_CHAR_BUDGET}"
    )


def test_the_dropped_entries_are_reported_honestly():
    """⚠ 砍了就要说（蓝图侧 ``_dropped_note`` 的同一措辞口径）。

    ⇒ 模型必须能区分「只有这些」和「有很多但只给了这些」。
    """
    builder = _builder()
    bible = _Bible([], timeline_notes=_notes(2074))

    lines = builder._build_timeline_lines(bible)
    notes = [line for line in lines if line.startswith("（另有")]

    assert len(notes) == 1, lines[-3:]
    assert "更早的时间线事件未注入" in notes[0], notes[0]
    assert "非本作品没有" in notes[0], notes[0]
    dropped = int(notes[0].split("另有 ")[1].split(" 条")[0])
    assert dropped == 2074 - (len(lines) - 1), (
        f"留痕说丢了 {dropped} 条，实际送了 {len(lines) - 1} 条 ⇒ 数字对不上"
    )


def test_a_short_timeline_is_delivered_whole():
    """⚠ 小书（21 条）⇒ **全送**，且**没有**留痕（不许虚报「有 N 条没给」）。"""
    builder = _builder()
    bible = _Bible([], timeline_notes=_notes(21))

    lines = builder._build_timeline_lines(bible)

    assert len(lines) == 21, lines
    assert not [line for line in lines if line.startswith("（另有")], lines


# ── ⑤ 两侧同源（生成侧 / 审计侧同一份）───────────────────────────────


def test_the_audit_side_uses_the_same_timeline_lines():
    """⚠ 审计基准必须与生成侧**同一份** ⇒ 审计比的与生成时被锁的是一回事。"""
    builder = _builder()
    bible = _Bible([_Char("甲")], timeline_notes=_notes(300))

    generated = builder._build_from_bible(bible, current_chapter=1)
    facts = builder._facts_from_bible(bible, current_chapter=1)
    joined = "\n".join(facts)

    assert "第300件事发生了" in generated and "第300件事发生了" in joined
    assert "第1件事发生了" not in generated and "第1件事发生了" not in joined


# ── ⑥ 根因守卫：长书形状的事实锁**不再被切** ───────────────────────────


def test_the_long_book_fact_lock_is_no_longer_cut():
    """⚠ **根因守卫**（第 24 批埋的）：长书形状的事实锁**整条送达**。

    ⚠ 第 42 批之前：2074 条时间线把事实锁撑到 38911 字 ⇒ 触发截断
    ⇒ 硬约束节虽然排在前面侥幸活下来，但**最近的事件**（列表尾部）先被砍。
    ⇒ 现在时间线按预算收口 ⇒ 事实锁 ≤ 每块上限 ⇒ **不触发任何截断**。
    """
    from application.engine.services.prose_context_settings import (
        get_prose_context_runtime_settings,
    )

    builder = _builder()
    characters = [
        _Char(
            f"角色{index}",
            description="定位" + "描述" * 20,
            public_profile="房里挂着名目的探员，同僚眼里的冷面笔杆子。" * 2,
            hidden_profile=(
                "第四张戏票此刻就在她大衣内袋，她尚未声张；她天生耳聪。"
                f"{index}。（POV防火墙：她的视角在后台认人之前，不得确知送票人身份{index}。）"
            ),
            mental_state="绷紧的孤勇",
            gender="女" if index % 2 == 0 else "男",
            age="25",
        )
        for index in range(8)
    ]
    bible = _Bible(characters, timeline_notes=_notes(2074))

    text = builder._build_from_bible(bible, current_chapter=1)
    cap = get_prose_context_runtime_settings().max_chars_per_slot

    assert len(text) <= cap, (
        f"长书形状的事实锁 {len(text)} 字 > 每块上限 {cap} ⇒ 还是会被切"
    )
    assert "已按上限截断" not in text, "长书形状的事实锁仍然被截断了"
    assert text.count("POV防火墙") == 8, "8/8 个 POV 防火墙没保住"
