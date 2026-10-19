"""第八轮复审三条必须修复项的回归守卫。

三条各自的教训：
1. `Tuple` 未导入 —— 本地 3.14 的 PEP 649 懒求值**掩盖**了它，CI 的 3.11 会 import 就炸。
   ⇒ 守卫不能只靠「模块能 import」，必须主动求值注解。
2. NaN / Inf / 超大数 —— 崩溃被调用方的 `except` 吞成 warning ⇒ 该章状态更新静默跳过。
   ⇒ 红线是「不抛异常」，不是「返回某个值」。
3. 越界 float 假 note —— note 随 registry JSON 落库，与实际取值矛盾即假 provenance。
   ⇒ note 里出现的章数/偏移必须与返回值一致。
"""
import math
import typing

import pytest

from application.world.services.foreshadow_pacing import (
    MAX_STORYLINE_END,
    RESOLVE_OFFSET_MAX,
    RESOLVE_OFFSET_MIN,
    resolve_offset,
    resolve_storyline_end,
)


# ── 1. Python 3.11 注解求值 ────────────────────────────────────────────────

def test_state_updater_annotations_resolve_under_eager_evaluation():
    """⚠ 本地 3.14 用 PEP 649 懒求值，`import` 成功**不能**证明注解里的名字存在。

    第八轮复审的阻塞项就是这个：`state_updater.py` 的 `_resolve_plant_offset`
    返回注解写了 `Tuple` 而 import 行没有，本地全绿、CI（3.11）import 即 NameError。
    这里主动求值，等价于 3.11 的行为。
    """
    import application.analyst.services.state_updater as su

    targets = [
        su.StateUpdater.update_from_chapter,
        su._resolve_plant_offset,
        su.ForeshadowingUpdateReport.__init__,
    ]
    for fn in targets:
        hints = typing.get_type_hints(fn)  # 3.11 下这步就会 NameError
        assert isinstance(hints, dict), fn


@pytest.mark.parametrize(
    "rel", [
        "application/analyst/services/state_updater.py",
        "application/world/services/foreshadow_pacing.py",
        "application/world/services/chapter_narrative_sync.py",
    ],
)
def test_every_public_annotation_resolves(rel):
    """把该模块所有函数/方法的注解都求值一遍。

    守卫名单里「加了这个文件」只等于让 unbound 探针去看它；
    这条从**运行时**再压一道，覆盖探针尚未扫描的签名形态。
    """
    import importlib

    mod = importlib.import_module(rel.replace("/", ".").removesuffix(".py"))
    checked = 0
    for name in dir(mod):
        obj = getattr(mod, name)
        if not callable(obj) or not getattr(obj, "__module__", "").startswith(
                rel.replace("/", ".").removesuffix(".py")):
            continue
        checked += 1
        typing.get_type_hints(obj)
        for attr in dir(obj):
            if attr.startswith("__"):
                continue
            try:
                member = getattr(obj, attr)
            except Exception:
                continue
            if callable(member) and getattr(member, "__qualname__", "").startswith(
                    f"{obj.__qualname__}."):
                typing.get_type_hints(member)
    assert checked, f"{rel} 没有找到可检查的函数/类"


# ── 2. 非有限值不得崩溃 ────────────────────────────────────────────────────

_NON_FINITE = [
    ("float nan", float("nan")),
    ("float inf", float("inf")),
    ("float -inf", float("-inf")),
    ("string nan", "nan"),
    ("string inf", "inf"),
    ("string 1e400", "1e400"),
    ("string -1e400", "-1e400"),
    ("huge int", 10 ** 400),
]


@pytest.mark.parametrize("label,raw", _NON_FINITE, ids=[n for n, _ in _NON_FINITE])
def test_resolve_offset_survives_non_finite_values(label, raw):
    """NaN / ±Inf / 1e400 / 400 位整数：走 fallback，**不得抛异常**。

    旧实现 `int(raw)` 在 try 内 ⇒ 安全回落；改成 `float(raw)` 后 `int()`
    被挪到 try 外 ⇒ 崩溃回归。调用方的 except 只记 warning，结果是该章
    伏笔/故事线/角色状态更新被静默跳过 —— 是数据丢失，不是崩溃。
    """
    offset, source, note = resolve_offset(raw)

    assert isinstance(offset, int), f"{label}: 返回值必须是 int"
    assert math.isfinite(offset), f"{label}: 返回非有限数 {offset}"
    assert source == "fallback", f"{label}: 应记 fallback，实为 {source!r}"
    assert note, f"{label}: 缺说明"


@pytest.mark.parametrize("label,raw", _NON_FINITE, ids=[n for n, _ in _NON_FINITE])
def test_resolve_storyline_end_survives_non_finite_values(label, raw):
    """同一条红线，G-03 侧。"""
    end, source, note = resolve_storyline_end(10, raw)

    assert isinstance(end, int) and 0 < end <= MAX_STORYLINE_END, f"{label}: {end}"
    assert source == "fallback", f"{label}: 应记 fallback，实为 {source!r}"
    assert note


def test_resolve_offset_never_raises_for_any_json_representable_value():
    """`json.loads` 默认接受 NaN / Infinity / 1e400 ⇒ 这些值能直达裁决器。

    这里把 JSON 能产出的所有「怪值」形态一次过一遍，断言裁决器不抛。
    """
    import decimal
    import json

    raw_values = [
        json.loads('{"x": NaN}')["x"],
        json.loads('{"x": Infinity}')["x"],
        json.loads('{"x": -Infinity}')["x"],
        json.loads('{"x": 1e400}')["x"],
        json.loads('{"x": ' + "1" * 400 + "}")["x"],
        decimal.Decimal("NaN"),
        decimal.Decimal("Infinity"),
    ]
    for raw in raw_values:
        offset, source, _ = resolve_offset(raw)
        assert source == "fallback", f"{raw!r} → {source!r}"
        end, source, _ = resolve_storyline_end(10, raw)
        assert source == "fallback", f"{raw!r} → {source!r}"


# ── 3. note 必须自述最终采用值 ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expect",
    [
        (1.9, 2), (35.5, 30), (40.5, 30), (31.2, 30),
        (0.4, 2), (-3.2, 2), (7.9, 7), (2.5, 2), (29.5, 29),
        # ⚠ 第九轮复审补：整数钳位分支原先写「被钳到 [2, 30]」，
        # 只给区间不给采用值 —— 读者无法知道实际用的是 2 还是 30。
        (0, 2), (-100, 2), (-5, 2), (999, 30), (31, 30), (40, 30),
        (True, 2), (False, 2),
    ],
)
def test_offset_note_cites_the_value_actually_returned(raw, expect):
    """note 必须以**结论句式**点明最终采用值。

    第八轮复审抓到：`1.9 → 2` 但 note 写「已截断为 1」，`35.5 → 30` 但写「35」。
    第九轮复审抓到：整数钳位写「被钳到 [2, 30]」——只给区间，采用值 2 不出现。

    ⚠ 判据是「结论句式」，**不是**「note 末位数字」。
    修正后的措辞是「被钳到 2（区间 [2, 30]）」——结论在前、区间在括号里，
    末位是区间上界 30 属正常。「末位 == 采用值」这条判据在这种措辞下
    必然假红，会逼着把 note 改成更难读的顺序。
    """
    import re

    offset, source, note = resolve_offset(raw)

    assert offset == expect, f"{raw!r}: 实际 {offset}，期望 {expect}"
    assert source != "ai", f"{raw!r} 被代码改写过，不该标 ai"
    # 采用值必须以「钳到 N」「截断为 N」这种结论句式出现，
    # 而不是只在区间括号里顺带出现。
    assert re.search(rf"(?:钳到|截断为)\s*{expect}\b", note), (
        f"{raw!r} → {offset}：note 未以结论句式点明采用值：{note}"
    )


def test_offset_note_ends_with_the_exact_returned_int():
    """note 里必须出现返回值，且是**结论句式**。

    ⚠ 判据**不是**「note 末位数字 == 返回值」。修正后的措辞是
    「被钳到 2（区间 [2, 30]）」—— 结论在前、区间在括号里，
    末位是区间上界 30。强求末位相等只会逼出更难读的 note 顺序。

    ⚠ 已知的**不可观测变异**（写下来免得后人重复踩）：
    「不钳位分支把 `{bounded}` 写成 `{int(numeric)}`」测不出来 ——
    不钳位意味着截断值已落在 [2,30] 内，此时两者**恒等**，渲染出同一字符串。
    越界截断（`1.9 → 2`、`35.5 → 30`）的 note 由
    `test_offset_note_cites_the_value_actually_returned` 覆盖，那条是真能红的。
    """
    import re

    for raw in [1.9, 2.5, 7.9, 29.5, 35.5, 40.5, 0.4, -3.2, 31.2, 0, -100, 999]:
        offset, _src, note = resolve_offset(raw)
        assert re.search(rf"(?:钳到|截断为)\s*{offset}\b", note), (
            f"{raw!r} → {offset}：note 未以结论句式点明采用值：{note}"
        )


def test_offset_note_never_grows_unbounded_with_the_raw_value():
    """⚠ 第九轮复审 §3.4：`{raw!r}` 会让 `10**400` 产出 401 位数字的 note。

    note 随 registry JSON / Storyline 落库，provenance 只需要
    「模型给了个荒谬的大数」这个事实，不需要完整数值。
    """
    for raw in (10 ** 400, 10 ** 30, float("1e308")):
        _off, _src, note = resolve_offset(raw)
        assert len(note) < 200, (
            f"resolve_offset raw 长 {len(str(raw))} 位 → note {len(note)} 字符，未截断"
        )


def test_every_note_channel_is_truncated():
    """⚠ 第十轮复审 §2.4：`_brief` 上一轮只接进了 `resolve_offset`。

    另外两条通道仍会把超长 repr 写进 note：
      · `_resolve_plant_offset` 的 fallback（落 registry JSON）
      · `resolve_storyline_end` 的 >MAX 分支（落 storylines.note）
    实测两者曾分别产出 437 / 342 字符的 note。
    """
    # 通道 2：state_updater 的 G-01 纯函数
    from application.analyst.services.state_updater import _resolve_plant_offset

    for raw in (10 ** 400, "x" * 5000):
        _off, _src, note = _resolve_plant_offset(1, raw)
        assert len(note) < 200, (
            f"_resolve_plant_offset raw={raw!r:.20} → note {len(note)} 字符，未截断"
        )

    # 通道 3：storyline 的 >MAX 分支
    for raw in (float("1e308"), 10 ** 400):
        _end, _src, note = resolve_storyline_end(10, raw)
        assert len(note) < 200, (
            f"resolve_storyline_end raw={raw!r:.20} → note {len(note)} 字符，未截断"
        )


def test_storyline_end_bool_note_declares_its_type():
    """bool 是 int 子类，`float(True)` == 1.0 ⇒ note 必须自述类型。

    与 offset 侧对齐（那里第九轮已修）。
    """
    for raw in (True, False):
        end, source, note = resolve_storyline_end(10, raw)
        assert "bool" in note.lower() or "布尔" in note, (
            f"storyline bool note 未自述类型：{raw!r} → {note}"
        )
        assert source == "ai_adjusted", (
            f"{raw!r} 不是模型给的收尾章，不该标 ai：{source!r}"
        )
        assert end > 10, f"{raw!r} → {end}"


def test_storyline_end_never_exceeds_the_cap_on_model_values():
    """⚠ `MAX_STORYLINE_END` 是**模型值**上限，不约束 current/兜底/抬升。

    第十轮复审：上一版用 `min(current + step, MAX)` 一刀切，
    于是 `resolve_storyline_end(15000, None)` 返回 10000 ≤ current，
    直接违反本模块声明的「收尾章 > 当前章」不变量，note 还写成
    「按兜底 +-5000 章」（负跨度）。
    """
    for current, raw in [(10, MAX_STORYLINE_END + 1), (10, 10 ** 400),
                         (9999, MAX_STORYLINE_END + 1), (10, float("1e308"))]:
        end, source, _note = resolve_storyline_end(current, raw)
        assert source == "fallback", (
            f"current={current} raw={raw!r:.12}: 超出上限应记 fallback，实为 {source!r}"
        )
        assert end > current, (
            f"current={current} raw={raw!r:.12}: end={end} 未越过 current"
        )


def test_storyline_end_always_stays_after_the_current_chapter():
    """不变量「收尾章 > 当前章」必须在**每一条路径**上成立。

    ⚠ 这是上一轮漏掉的断言：当时只断言 `end ≤ MAX`，
    于是「为了守住 MAX 而破坏不变量」的变异全绿。

    MAX 只该约束模型给的值；current 是代码自己维护的写作进度，
    不该被护栏按下去。现实章数远小于 MAX，但一旦可达就必须自洽。
    """
    import re

    cases = [
        (10000, None), (15000, None), (10 ** 6, None), (10 ** 9, "十几章"),
        (15000, 2), (MAX_STORYLINE_END, 1), (MAX_STORYLINE_END + 5, 1),
        (MAX_STORYLINE_END, None), (9999, None), (10, 10001),
        (10, None), (10, 42), (3, 2), (0, None),
    ]
    for current, raw in cases:
        end, source, note = resolve_storyline_end(current, raw)
        assert end > current, (
            f"resolve_storyline_end({current}, {raw!r:.12}) → {end} "
            f"[{source}]：违反「收尾章 > 当前章」不变量。note={note}"
        )
        # 兜底跨度不得是负数（曾出现「按兜底 +-5000 章」）
        if "兜底" in note:
            span_txt = re.search(r"兜底 \+(-?\d+) 章", note)
            assert span_txt, f"兜底 note 未写明跨度：{note}"
            assert int(span_txt.group(1)) > 0, f"兜底跨度非正：{note}"
        # 抬升 note 里的章号必须等于返回值，且确实晚于 current
        if "抬到第" in note:
            m = re.search(r"抬到第 (\d+) 章", note)
            assert m, f"抬升 note 未写明章号：{note}"
            assert int(m.group(1)) == end == current + 1, (
                f"抬升 note 与返回值/不变量不一致：end={end} current={current} "
                f"note={note}"
            )


def test_isfinite_guard_is_not_the_only_line_of_defence():
    """⚠ 冗余防御的诚实记录，不是防空转。

    `int(float('nan'))` 抛 ValueError、`int(float('inf'))` 抛 OverflowError，
    两者都在 try 内 ⇒ **即使删掉 `math.isfinite` 也不会崩**（已变异验证：
    该变异 GREEN）。它防的是「未来有人把 `int()` 挪出 try」这类重构。

    这条断言的作用是把上述事实钉在测试里：谁删了 isfinite 并宣称
    「测试全绿所以没事」，这里会指出它当时靠的是另一道防线。
    """
    import inspect

    from application.world.services import foreshadow_pacing as fp

    for fn_name in ("resolve_offset", "resolve_storyline_end"):
        src = inspect.getsource(getattr(fp, fn_name))
        assert "math.isfinite" in src, f"{fn_name} 丢了 isfinite 防线"
        # int() 必须在 try 内 —— 这是 isfinite 之外的那道真防线
        assert "except (TypeError, ValueError, OverflowError)" in src, (
            f"{fn_name} 的 except 未覆盖 OverflowError"
        )


@pytest.mark.parametrize(
    "raw,expect_end,expect_source",
    [
        (11.9, 11, "ai_adjusted"),
        (42, 42, "ai"),
        (8, 11, "ai_adjusted"),       # 8 <= 10 ⇒ 抬升
        (None, 22, "fallback"),
        ("", 22, "fallback"),
        ("  ", 22, "fallback"),
        ("十几章", 22, "fallback"),
        (MAX_STORYLINE_END + 1, 22, "fallback"),   # 超上限
        (10 ** 400, 22, "fallback"),
    ],
)
def test_storyline_end_tristate(raw, expect_end, expect_source):
    """G-03 三态对齐：float 截断标 adjusted、越界标 adjusted、脏值标 fallback。"""
    end, source, note = resolve_storyline_end(10, raw)

    assert end == expect_end, f"{raw!r}: 实际 {end}，期望 {expect_end}"
    assert source == expect_source, f"{raw!r}: {source!r} != {expect_source!r}"
    assert note


def test_storyline_end_never_returns_an_absurd_chapter_marked_ai():
    """反向守卫：超大整数不得原样落库并标 ai。"""
    for raw in (10 ** 400, 10 ** 100, MAX_STORYLINE_END + 1):
        end, source, _ = resolve_storyline_end(10, raw)
        assert end <= MAX_STORYLINE_END, f"{raw!r} → {end} 超出上限"
        assert source != "ai", f"{raw!r} 标成了 ai"


def test_storyline_end_note_cites_the_value_actually_returned():
    """note 必须以**结论句式**点明最终采用值。

    ⚠ 判据**不是**「note 里出现的所有章号都等于落库值」。
    一条 note 可以合法地提到中间值：「不是整数，已截断为 1；…已由代码抬到第 11 章」
    —— 截断值 1 与采用值 11 同时出现，且都如实标注了角色。
    红线是：**结论（最终采用值）必须出现在结论句式里**。

    ⚠ 第九轮复审 §3.8 的另一面：ai 分支的 note
    （`模型给出的预估收尾章 42`）不含「第 N 章」字样，
    正则抽不出章号 ⇒ 空集 ≤ {end} 恒成立，这条断言对它是**空转**的。
    所以 ai 分支单独断言「note 点明了取值」。
    """
    import re

    for current, raw in [(10, 8), (10, 11.9), (10, 42), (10, 1.2), (3, 2),
                         (10, float("nan")), (10, "十几章")]:
        end, source, note = resolve_storyline_end(current, raw)
        if source == "ai":
            assert str(end) in note, f"ai 分支的 note 未点明取值 {end}：{note}"
            continue
        # 结论：note 必须说「已由代码抬到第 N 章」且 N == end
        lifted = re.search(r"抬到第 (\d+) 章", note)
        if lifted:
            assert int(lifted.group(1)) == end, (
                f"current={current} raw={raw!r} → {end}，"
                f"note 的抬升章号 {lifted.group(1)} 不符：{note}"
            )
        elif source == "fallback":
            span_txt = re.search(r"兜底 \+(\d+) 章", note)
            assert span_txt, f"fallback note 未写明跨度：{note}"
            assert current + int(span_txt.group(1)) == end, (
                f"fallback 跨度与落库值不符："
                f"current={current} end={end} note={note}"
            )
        else:
            # adjusted 且无需抬升：结论句式是「已截断为 N」，N 必须等于 end
            trunc = re.search(r"已截断为 (\d+)", note)
            assert trunc, (
                f"current={current} raw={raw!r} [{source}]："
                f"note 既非抬升也无截断结论句式：{note}"
            )
            assert int(trunc.group(1)) == end, (
                f"current={current} raw={raw!r} → {end}，"
                f"note 的截断值 {trunc.group(1)} 与落库值不符：{note}"
            )
        if source == "ai_adjusted":
            # ⚠ 不断言整句文案（措辞会改），只断言「声明了非模型原值」这一事实
            assert "非模型原值" in note, (
                f"adjusted note 必须声明取值非模型原值：{note}"
            )

def test_resolve_offset_accepts_scientific_notation():
    """⚠ 第九轮复审 §3.8 记录的行为变更（此前未被任何测试锁定）。

    改造前用 ``int(raw)``，``"1e3"`` 不可解析 ⇒ fallback（5 章）。
    改造后用 ``float(raw)``，``"1e3"`` = 1000.0 ⇒ 钳到 30 并标 ``ai_adjusted``。

    **这是更合理的解释**（模型确实给了个数字，1000），但它是一次行为变更，
    必须被测试钉住，否则以后有人改回去都不会有人发现。
    """
    offset, source, note = resolve_offset("1e3")
    assert offset == RESOLVE_OFFSET_MAX, f'"1e3" → {offset}，期望 {RESOLVE_OFFSET_MAX}'
    assert source == "ai_adjusted", (
        f'"1e3" 给了有效数值却标 {source!r} —— 取值被钳位，不是模型原值'
    )
    assert str(offset) in note


def test_resolve_offset_bool_is_treated_as_a_number_not_a_missing_value():
    """⚠ `True`/`False` 的 note 曾写「偏移 1/0」，读起来像模型给了个 1。

    Python 里 `bool` 是 `int` 的子类，`float(True)` = 1.0。这是既定行为
    （模型输出 JSON 里出现 `true` 是可能的），但 note 必须自述**类型**，
    否则 provenance 会误导读者以为模型给了数字 1。
    """
    for raw in (True, False):
        offset, source, note = resolve_offset(raw)
        assert offset == RESOLVE_OFFSET_MIN, f"{raw!r} → {offset}"
        assert source == "ai_adjusted"
        assert "bool" in note.lower() or "布尔" in note, (
            f"{raw!r} 的 note 应自述是布尔值而非数字：{note}"
        )
