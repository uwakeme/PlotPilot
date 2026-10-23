"""B2：前端阶段区间不再自算 —— 断点口径只留后端一处。

背景（第 3 批复审 B2）
----------------------
前端 ``plotOutlineModel.ts`` 有一份自己的章节区间公式，它把
「当前段之后还剩几段」误当成「当前段是不是最后一段」
（``index === remaining``）⇒ **奇数阶段数时算出越界章号**：

    total=100 stages=5 ⇒ (1,20) (21,40) (41,100) (101,101) (102,102)

实测（审查方用 Node 跑真实 TS 公式）：
``total=20 stages=5`` ⇒ 21/22 越界；``total=10 stages=3`` ⇒ 11 越界。
**默认就是 5 段** ⇒ 默认路径就是坏的。

更糟的是**后端静默接受**：三条不变量（阶段数 ∈[3,8] / 首尾相接 /
末段终点 ≥ 目标）**全过** —— 因为区间是连续的，末段终点 102 ≥ 100
⇒ 一份「有一段在书外」的阶段计划被静默写进库并驱动后续规划。

为什么不能只是「把 ``index === remaining`` 改成 ``index === n - 1``」
----------------------------------------------------------------------
改对公式只是把「越界」变成「不越界」，但**两份口径仍在**：
前端均分、后端 5 段用比例表 ``(0.15, 0.40, 0.70, 0.90, 1.0)``、
非 5 段用均分，且 Python ``round()`` 是银行家舍入、``Math.round`` 是四舍五入
⇒ **同样的输入两边算出不同的断点**。

而且前端算出来的区间会**被提交回后端**（``buildEditablePlotOutlinePayload``
原样带上 ``chapter_start``/``chapter_end``），后端对**显式**区间是照单全收的
⇒ 前端那份估算会直接落库，变成「事实上的第二份真相」。

⇒ **修法**：前端**不再算**区间。AI / 后端给了就用，没给就**留空**，
由后端在保存时用 ``stage_plan_invariants.chapter_ranges`` 补齐。
保存后前端用服务端返回的归一化结果重绘（``NovelSetupGuide.vue``
的 ``response.plot_outline || payload``）⇒ 口径只有一处。

⚠ 后端**允许**计划覆盖到比目标更大的章数（既有行为，见
``test_plan_longer_than_target_is_allowed``）⇒ 「不编造」这条防线
只能落在前端 ⇒ 守卫钉住「前端不再持有区间算法」。

⚠ 守卫的局限（写在文件头，不假装它更强）
----------------------------------------
前端**零测试设施**（只有 ``npm run build`` 门禁）⇒ 这里对前端的断言是
**文件内容级**（钉住「没有区间算法」这个**缺席**）。
「缺席」型断言比「存在」型可靠：多一份算法就会命中。
但它**挡不住**「换个写法再写一份」⇒ 前端行为仍需 ``npm run build`` + 人工走查。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from application.blueprint.services.setup_plot_outline_continuation import (
    normalize_setup_plot_outline_payload,
)
from application.blueprint.services.stage_plan_invariants import (
    MAX_STAGES,
    MIN_STAGES,
    chapter_ranges,
    validate_stage_plan_shape,
)

ROOT = Path(__file__).resolve().parents[4]
FRONTEND_MODEL = ROOT / "frontend/src/onboarding/plotOutlineModel.ts"


def _frontend_code() -> str:
    """只取**代码**部分（去掉注释）。

    ⚠ 前端文件里有解释历史变更的注释 ⇒ 查全文会命中**自己写的说明文字**
    （假红）。这里用**正则**整体剥掉 ``/* ... */`` 与 ``// ...``。
    """
    import re

    source = FRONTEND_MODEL.read_text(encoding="utf-8")
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    without_line = re.sub(r"//[^\n]*", "", without_block)
    return without_line


def _frontend_sources() -> list[tuple[str, str]]:
    """前端**全部** .ts / .vue 文件的代码部分（路径, 代码）。

    ⚠ 第 3 批三次复审（附注）：上一版守卫**只读一个文件**
    （``plotOutlineModel.ts``）⇒ 在别的文件里算区间它看不见
    ⇒ 声称被过度。
    ⇒ 现在扫**整个 frontend/src**。
    """
    import re

    frontend_src = ROOT / "frontend/src"
    out: list[tuple[str, str]] = []
    for path in sorted(frontend_src.rglob("*")):
        if path.suffix not in {".ts", ".vue"} or not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        without_line = re.sub(r"//[^\n]*", "", without_block)
        out.append((rel, without_line))
    return out


def _outline(stages):
    return {
        "main_story_overview": "概述",
        "expected_ending": "结局",
        "core_conflict": "冲突",
        "stage_plan": stages,
    }


def _stage(start=None, end=None, *, phase="", label="", summary="一段"):
    row = {"summary": summary}
    if start is not None:
        row["chapter_start"] = start
    if end is not None:
        row["chapter_end"] = end
    if phase:
        row["phase"] = phase
    if label:
        row["label"] = label
    return row


# ───────────────────────── 前端不再持有区间算法 ─────────────────────────


def test_frontend_has_no_chapter_range_algorithm():
    """前端**不许**再自己算章节区间。

    判据是**缺席**：区间算法的入口函数名不许存在，
    且不许出现「按段数循环切区间」的形状。

    ⚠ 这条断言钉的是「没有」，不是「有」⇒ 比存在型断言可靠
    （多写一份就命中），但挡不住「换个写法再写一份」。
    """
    code = _frontend_code()

    assert "buildStageChapterRanges" not in code, (
        "前端又自己算章节区间了 —— 断点口径必须只有后端一处"
    )
    # 均分公式的形状：round(total * (index + 1) / n)
    assert "Math.round((total * (index + 1)) / n)" not in code, (
        "前端又出现了均分切区间的公式"
    )


def test_frontend_does_not_fabricate_ranges_when_missing():
    """阶段**没带**区间时，前端不许替它编一个。

    ⚠ 第 3 批二次复审 S6：上一版只查「有没有出现某个字符串」，
    而**把同一份错误公式换个写法再塞回去，守卫照样全绿**
    （复审用 M2c 变异证明：编译后确实复现了越界区间）。
    ⚠ 第 3 批三次复审（附注）：再上一版改成「枚举**每一处**行首的属性赋值」，
    但它**只读一个文件**、且正则只认「行首」⇒ 三种逃逸全绿：
      ① 单行 ``Object.assign(next, { chapter_start: … })``
      ② ``next['chapter_start'] = …``
      ③ 在**别的前端文件**里算区间

    ⇒ 现在：**扫整个 frontend/src**，并识别**四种赋值形态**：
      - 行首/属性位置 ``chapter_start: …``
      - 单行对象字面量里的 ``chapter_start: …``（同行）
      - 括号/下标赋值 ``next['chapter_start'] = …``
      - 点号赋值 ``next.chapter_start = …``
    ⇒ 任何一处「从别处算出来的」赋值都会被抓。

    ⚠ 第 3 批四次复审：上一版**声称 4 种、实际只支持 2 种** ——
    「读 vs 写」靠「prefix 以 ``.`` 结尾 ⇒ 判为读」这个启发式，
    于是 ``next.chapter_start = 5`` 被当成**读**而放行；
    而 ``next['chapter_start'] = 5`` 压根不匹配（后面是 ``']`` 不是 ``[:=]``）。
    ⇒ 现在改用**分隔符**判断：
      - ``=`` ⇒ 一定是**写**（赋值）；
      - ``:`` ⇒ 对象字面量属性（写）或 TS 类型标注（不是写）；
      - 三元表达式里的**读**（``? stage.chapter_start : undefined``）靠
        「同一行前面出现过 ``?``」排除。

    ⚠ **这四种形态是实测过的，不是声称的**：
    第 3 批四次复审指出上一版「声称 4 种、实际只支持 2 种」
    ⇒ 现在用真实代码注入 4 种写法逐个验证（变异脚本
    ``_mut_s6_forms.py``）：4 种**全部被抓住**。
    ⇒ 改这份清单前，**先跑那个变异脚本**，别再写一个没验证过的清单。
    """

    import re

    # 允许的赋值形态（只允许「原样透传」与「留空」）
    allowed_value = re.compile(
        r"^(?:"
        r"rawStart"          # 原样透传（AI/后端给的值）
        r"|rawEnd"
        r"|undefined"        # 留空（交给后端补）
        r"|keepManualRange\s*\?\s*rawStart\s*:\s*undefined"
        r"|keepManualRange\s*\?\s*rawEnd\s*:\s*undefined"
        r"|typeof\s+stage\.chapter_start\s*===\s*'number'\s*\?.*"
        r"|typeof\s+stage\.chapter_end\s*===\s*'number'\s*\?.*"
        r"|number|string|boolean|any"  # TS **类型标注**（不是赋值）
        r")$"
    )

    # 匹配「给 chapter_start/chapter_end 赋值」的各种形态。
    # - 前置负向断言 ⇒ ``estimated_chapter_start`` 这类**同后缀**名字不算；
    # - ``(?:['\"]\]\s*)?`` ⇒ 支持下标写法 ``next['chapter_start'] = …``
    #   （注意是 **引号 + 右方括号**，不是 ``[']`` —— 那是键在括号外的另一种写法）；
    # - 分隔符必须是**单个** ``=`` / ``:`` ⇒ ``===`` / ``==``（比较运算）不算；
    # - 捕获分隔符 ⇒ 用它区分「写」与「读」。
    assign_re = re.compile(
        r"(?<![A-Za-z0-9_])chapter_(start|end)\b\s*(?:['\"]\]\s*)?"
        r"(:(?!=)|=(?!=))\s*([^,;\n}]+)"
    )

    offenders: list[str] = []
    for rel, code in _frontend_sources():
        # 只看**处理阶段计划**的文件（其余文件的 chapter_start 是别的概念：
        # 故事线的 estimated_chapter_start、审稿的 chapter_start …）
        if "stage_plan" not in code:
            continue
        for line in code.splitlines():
            stripped = line.strip()
            if "chapter_start" not in stripped and "chapter_end" not in stripped:
                continue
            for match in assign_re.finditer(stripped):
                separator = match.group(2)
                value = match.group(3).strip().rstrip(",")
                prefix = stripped[: match.start()]
                if separator == ":" and "?" in prefix:
                    # 三元表达式里的**读**：``? stage.chapter_start : 0``
                    # （``:`` 前面出现过 ``?`` ⇒ 这个 ``:`` 是三元运算符的值槽）
                    continue
                if not allowed_value.match(value):
                    offenders.append(f"{rel}: {stripped}")

    assert not offenders, (
        "前端出现了「自己算章节区间」的赋值 —— 断点口径必须只有后端一处：\n"
        + "\n".join(f"  - {line}" for line in offenders)
    )


def test_the_save_gate_tolerates_empty_ranges():
    """保存门禁必须**放行「区间为空」**（由后端补齐），只拦「有值但非法」。

    ⚠ 第 3 批二次复审 N4：前端不再产出区间（AI 不给、前端不自算），
    而保存门禁仍要求「每段都得有数字区间」⇒ 请求根本发不出去
    ⇒ **向导第 4 步过不去**，后端那份补齐逻辑永远用不上。
    ⇒ 现在：两端都为空 ⇒ 放行；只有一端有值 / 倒挂 / 小于 1 ⇒ 拦。
    """
    code = _frontend_code()

    assert "if (start === undefined && end === undefined) return false" in code, (
        "保存门禁必须放行「两端都为空」的区间（后端会补齐）"
    )
    assert "if (start === undefined || end === undefined) return true" in code, (
        "只填了一端 ⇒ 区间非法 ⇒ 必须拦"
    )


def test_frontend_keeps_the_provided_ranges():
    """AI / 后端给了区间 ⇒ 前端原样保留（不许改写）。

    ⚠ R2（第 3 批三次复审）：此前用 ``keepManualRange``（要求两端都有且
    start ≤ end）⇒ 作者只填了「起始章」时**两端一起被丢掉**
    ⇒ 刚敲的数字**静默消失**。现在**两端各自保留**。
    """
    code = _frontend_code()

    assert "chapter_start: rawStart," in code, "前端必须保留 AI / 后端给出的区间"
    assert "chapter_end: rawEnd," in code, "前端必须保留 AI / 后端给出的区间"


# ───────────────────────── 后端是唯一补齐方 ─────────────────────────


@pytest.mark.parametrize("count", list(range(MIN_STAGES, MAX_STAGES + 1)))
@pytest.mark.parametrize("total", [3, 4, 5, 6, 7, 8, 9, 20, 80, 100, 500])
def test_backend_ranges_are_always_inside_the_book(count, total):
    """后端自己算的区间**永远**在书内、首尾相接、覆盖整本书。

    这条是前端「不编造」之后的**兜底保证**：即便有人再写一份算法，
    后端补出来的值仍然是安全的。
    """
    ranges = chapter_ranges(total, count)
    assert ranges[0][0] == 1, (count, total, ranges)
    assert ranges[-1][1] == total, (count, total, ranges)
    for (a, b), (c, d) in zip(ranges, ranges[1:]):
        assert b + 1 == c, (count, total, ranges)
        assert a <= b, (count, total, ranges)
        assert 1 <= a <= b <= total, (count, total, ranges)


@pytest.mark.parametrize("count", list(range(MIN_STAGES, MAX_STAGES + 1)))
def test_backend_fills_missing_ranges_on_save(count):
    """阶段**没带**区间时，后端在保存时补齐（前端留下的空白由它收口）。"""
    stages = [_stage(summary=f"第{i}段") for i in range(count)]
    out = normalize_setup_plot_outline_payload(_outline(stages), target_chapters=90)

    got = [(s["chapter_start"], s["chapter_end"]) for s in out["stage_plan"]]
    assert got == chapter_ranges(90, count)
    assert got[-1][1] == 90
    assert validate_stage_plan_shape(out["stage_plan"], target_chapters=90) == []


def test_the_old_buggy_frontend_output_is_not_what_the_backend_produces():
    """回归：旧前端公式的产物（末两段在书外）**不会**由后端产生。

    后端**允许**显式的超长计划（既有行为）⇒ 这条不是「后端会拒绝它」，
    而是「后端自己算的时候不会产出它」—— 两件事要分清。
    """
    buggy = [(1, 20), (21, 40), (41, 100), (101, 101), (102, 102)]
    produced = chapter_ranges(100, 5)

    assert produced != buggy, "后端又产出了越界区间"
    assert produced[-1][1] == 100
    for (_, b), (c, _) in zip(produced, produced[1:]):
        assert b + 1 == c
