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
    ],
)
def test_offset_note_cites_the_value_actually_returned(raw, expect):
    """note 必须自述最终采用值。

    第八轮复审抓到：`1.9 → 2` 但 note 写「已截断为 1」，`35.5 → 30` 但写「35」。
    假 note 会随 registry JSON 落库，下游据此判断 provenance。

    ⚠ 这里**不**禁止 note 提到中间值 —— 「先截断为 1、再钳到 2」是正确且
    更完整的表述。真正的红线是：**note 里最后一个数字必须是最终采用值**，
    也就是不能停在中间值上让读者以为那就是结果。
    """
    import re

    offset, source, note = resolve_offset(raw)

    assert offset == expect, f"{raw!r}: 实际 {offset}，期望 {expect}"
    assert source != "ai", f"{raw!r} 被代码改写过，不该标 ai"
    assert str(expect) in note, f"{raw!r} → {offset}，note 未提采用值：{note}"

    nums = re.findall(r"\d+", note)
    assert nums, f"{raw!r} 的 note 里没有数字：{note}"
    assert nums[-1] == str(expect), (
        f"{raw!r} → {offset}，note 停在中间值 {nums[-1]}，"
        f"读者会误以为那是采用值：{note}"
    )


def test_offset_note_ends_with_the_exact_returned_int():
    """note 末位数字必须**逐字**等于返回值。

    ⚠ 已知的**不可观测变异**（写下来免得后人重复踩）：
    「不钳位分支把 `{bounded}` 写成 `{int(numeric)}`」测不出来 ——
    不钳位意味着截断值已落在 [2,30] 内，此时两者**恒等**，渲染出同一字符串。
    越界截断（`1.9 → 2`、`35.5 → 30`）的 note 由
    `test_offset_note_cites_the_value_actually_returned` 覆盖，那条是真能红的。
    """
    import re

    for raw in [1.9, 2.5, 7.9, 29.5, 35.5, 40.5, 0.4, -3.2, 31.2]:
        offset, _src, note = resolve_offset(raw)
        nums = re.findall(r"\d+", note)
        assert nums[-1] == str(offset), (
            f"{raw!r}: note 末位 {nums[-1]} != 返回值 {offset}\n  {note}"
        )
        # 采用值必须以结论句式出现（钳到/截断为），而不是只在「原始值 X」处顺带出现
        assert re.search(rf"(?:钳到|截断为|被钳到)\s*{offset}\b", note), (
            f"{raw!r} → {offset}：note 未以结论句式点明采用值：{note}"
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
    """与 G-01 同一条红线：note 自述的章号必须等于落库值。"""
    import re

    for current, raw in [(10, 8), (10, 11.9), (10, 42), (10, 1.2), (3, 2)]:
        end, _source, note = resolve_storyline_end(current, raw)
        cited = set(re.findall(r"第 (\d+) 章|截断为 (\d+)", note))
        flat = {x for pair in cited for x in pair if x}
        assert flat <= {str(end)}, (
            f"current={current} raw={raw!r} → {end}，note 引用了非落库章号 "
            f"{flat - {str(end)}}：{note}"
        )
