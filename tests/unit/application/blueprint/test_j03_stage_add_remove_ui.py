"""J-03：作者可以**增删阶段**（前端）+ 阶段数边界两侧不许漂。

背景
----
登记表 J-03 写「『用户能增删阶段』的 UI 仍没有（全仓无 addStage/removeStage）
⇒ 作者仍无法自定义阶段数」。

核实（读码）
------------
· 后端阶段数不变量 **I1 = [3, 8]**（``stage_plan_invariants.MIN_STAGES/MAX_STAGES``）；
· 断点口径只有一处（``chapter_ranges`` 补齐），前端不再有第二份；
· 保存时后端会**重新校验**（``validate_stage_plan_shape``）⇒ 非法阶段数存不进去。

⇒ 所以「增删阶段」是**纯前端**能力，但必须**守住 [3, 8]**（否则作者能提交一个
必然被后端拒的请求，报错还不透明）。

⚠ 前端**零测试设施**（无 vitest，只有 ``npm run build`` 门禁）
⇒ 守卫是**文件内容级**断言（与本仓其它前端守卫同款），局限写在下面。

本文件钉住
----------
1. 阶段编辑器**有**「添加阶段」与「删除」两个动作；
2. 前端的阶段数边界 == **后端的不变量**（跨层互锁 ⇒ 两侧不许各写一份漂掉）；
3. 追加阶段时**清空区间**（后端规则是「要么全填、要么全不填」，混用会被拒）；
4. 后端**仍然**在保存时校验阶段数（不变量没被前端绕开）。
"""
from __future__ import annotations

import re
from pathlib import Path

from application.blueprint.services.stage_plan_invariants import (
    MAX_STAGES,
    MIN_STAGES,
    validate_stage_plan_shape,
)

ROOT = Path(__file__).resolve().parents[4]
GUIDE = ROOT / "frontend/src/components/onboarding/NovelSetupGuide.vue"
MODEL = ROOT / "frontend/src/onboarding/plotOutlineModel.ts"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ─────────────────────── 1) 有增删两个动作 ───────────────────────


def test_the_stage_editor_offers_add_and_remove_actions():
    """阶段编辑器必须**有**「添加阶段」与「删除」两个动作。

    ⚠ 文件内容级断言（前端零测试设施）⇒ 它挡得住「动作被删掉/被改名」，
    挡不住「换了另一种写法实现同一个动作」。
    """
    source = _read(GUIDE)

    assert "添加阶段" in source, "阶段编辑器没有「添加阶段」"
    assert "删除" in source, "阶段编辑器没有「删除」"
    assert "addStage" in source and "removeStage" in source, (
        "阶段编辑器没有调用增删阶段的处理函数"
    )


# ─────────────────────── 2) 边界两侧不许漂 ───────────────────────


def test_the_frontend_stage_bounds_match_the_backend_invariant():
    """前端的阶段数边界必须 == **后端不变量 I1**。

    ⚠ 前端拿不到 Python 常量 ⇒ 这是一份**副本** ⇒ 这条守卫就是它的互锁。
    """
    source = _read(MODEL)

    min_match = re.search(r"MIN_STAGE_COUNT\s*=\s*(\d+)", source)
    max_match = re.search(r"MAX_STAGE_COUNT\s*=\s*(\d+)", source)
    assert min_match, "前端没有声明 MIN_STAGE_COUNT"
    assert max_match, "前端没有声明 MAX_STAGE_COUNT"

    assert int(min_match.group(1)) == MIN_STAGES, (
        f"前端 MIN_STAGE_COUNT={min_match.group(1)} 与后端 MIN_STAGES={MIN_STAGES} 不一致"
    )
    assert int(max_match.group(1)) == MAX_STAGES, (
        f"前端 MAX_STAGE_COUNT={max_match.group(1)} 与后端 MAX_STAGES={MAX_STAGES} 不一致"
    )


# ─────────────────────── 3) 追加阶段要清空区间 ───────────────────────


def test_adding_a_stage_clears_the_chapter_ranges():
    """追加阶段必须**清空所有区间**（回到「全不填」态）。

    ⚠ 后端规则：区间**要么全部不填**（系统按目标章数分配），**要么全部填**
    ⇒ 新加的阶段没有区间 ⇒ 不清空就变成「混用」⇒ 保存时被拒（报错不透明）。
    """
    source = _read(MODEL)

    body = source.split("export function addPlotStage(", 1)
    assert len(body) == 2, "前端没有 addPlotStage"
    fn = body[1].split("\n}", 1)[0]

    assert "chapter_start: undefined" in fn, "追加阶段没有清空 chapter_start"
    assert "chapter_end: undefined" in fn, "追加阶段没有清空 chapter_end"


# ─────────────────────── 4) 后端不变量仍在（真行为守卫） ───────────────────────


def test_the_backend_still_rejects_a_stage_count_outside_the_invariant():
    """前端能增删阶段，但**后端的不变量仍在**（不变量不被前端绕开）。"""
    too_few = [
        {"phase": "a", "label": "A", "chapter_start": 1, "chapter_end": 10},
        {"phase": "b", "label": "B", "chapter_start": 11, "chapter_end": 20},
    ]
    too_many = [
        {
            "phase": f"s{i}",
            "label": f"S{i}",
            "chapter_start": i * 2 + 1,
            "chapter_end": (i + 1) * 2,
        }
        for i in range(MAX_STAGES + 1)
    ]

    assert validate_stage_plan_shape(too_few), "阶段数少于下界竟被接受"
    assert validate_stage_plan_shape(too_many), "阶段数多于上界竟被接受"
