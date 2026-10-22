"""J-03 遗留：五阶段前端镜像 —— 前端零守卫 + 后端死代码清理。

背景
----
J-03 的主体（A-02 的前端面）已在 2026-10-17 完成：前端
``plotOutlineModel.ts`` 里的「按下标强制回贴固定 5 个 phase 名」与
「自己那份硬编码比例表」都已清掉，阶段名/阶段数/断点改由后端
``stage_plan_invariants.py`` 决定。

但那笔改造**零测试**（前端没有测试设施，只有 ``npm run build`` 门禁）
⇒ 本笔补两条**文件内容级**守卫，把「前端不再有第二份断点逻辑」钉住。

⚠ 守卫的局限（写在文件头，不假装它更强）
------------------------------------------
这是**文件内容级断言**，不是行为级断言：
- 它能挡住「前端又把固定 5 段表 / 硬编码比例表写回来」；
- 它**挡不住**「换个写法再镜像一份断点逻辑」。

真正的前端行为验证仍需 ``npm run build``（vue-tsc）+ 人工走查。

另一条：后端 ``setup_plot_outline_continuation.py`` 里还留着
``_chapter_ranges``（**第二份**硬编码比例表）—— 它是 A-02 改造遗留的
**孤儿死函数**（只有定义、零调用方）⇒ 本笔删掉。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
FRONTEND_MODEL = ROOT / "frontend/src/onboarding/plotOutlineModel.ts"
BACKEND = ROOT / "application/blueprint/services/setup_plot_outline_continuation.py"
STAGE_INVARIANTS = ROOT / "application/blueprint/services/stage_plan_invariants.py"


def _frontend_source() -> str:
    return FRONTEND_MODEL.read_text(encoding="utf-8")


def _frontend_code() -> str:
    """只取**代码**部分（去掉注释）。

    ⚠ 踩过两次：
    1. 前端文件里有**解释历史变更的注释**，里面写着
       「原先这是前端自己那份 ``STAGE_RANGE_RATIOS = [0.15, 0.40, ...]``」
       ⇒ 查全文会命中**自己写的说明文字**（假红）。
    2. 用「逐行判断 ``/*`` 开头」来剥块注释不可靠 ——
       JSDoc 是 ``/**`` 开头、``*/`` 结尾，逐行判断会把状态数错。
    ⇒ 这里用**正则**整体剥掉 ``/* ... */`` 与 ``// ...``。
    """
    source = FRONTEND_MODEL.read_text(encoding="utf-8")
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    without_line = re.sub(r"//[^\n]*", "", without_block)
    return without_line


# ── 1) 前端不再有第二份断点逻辑 ─────────────────────────────────────

def test_frontend_has_no_fixed_five_stage_table():
    """固定 5 段表不许回来（``STAGE_PHASE_META``）。"""
    code = _frontend_code()

    assert "STAGE_PHASE_META" not in code, "前端的固定 5 段表回来了"
    assert "STAGE_RANGE_RATIOS" not in code, "前端自己那份硬编码比例表回来了"


def test_frontend_does_not_hardcode_the_five_stage_ratios():
    """硬编码比例表不许回来（0.15/0.40/0.70/0.90/1.0 那一组）。"""
    code = _frontend_code()

    for ratio in ("0.15", "0.40", "0.70", "0.90"):
        assert ratio not in code, f"前端又写死了比例 {ratio}"


def test_frontend_uses_positional_fallback_not_index_forced_names():
    """阶段名缺失时按**位置**兜底，不是按固定名表回贴。

    ⚠ 判据是**调用点**（带左括号），不是函数名 ——
    变异把定义改名后，名字字符串仍在文件里 ⇒ 查名字会假绿。
    """
    code = _frontend_code()

    assert "stageFallback(" in code, "按位置兜底的调用没了"
    assert "STAGE_FALLBACK_LABELS" in code


def test_backend_remains_the_single_source_of_truth():
    """后端仍是断点的单一事实源（AI 定阶段数与断点）。"""
    source = STAGE_INVARIANTS.read_text(encoding="utf-8")

    assert "MIN_STAGES" in source
    assert "MAX_STAGES" in source
    assert "def chapter_ranges" in source
    assert "def validate_stage_plan_shape" in source


# ── 2) 后端死代码已清 ───────────────────────────────────────────────

def test_orphan_chapter_ranges_is_gone():
    """``_chapter_ranges``（第二份硬编码比例表）是孤儿死函数 ⇒ 已删。

    它只有定义、**零调用方**（真正在用的是
    ``stage_plan_invariants.chapter_ranges``，两参版本）。
    """
    source = BACKEND.read_text(encoding="utf-8")

    assert "def _chapter_ranges" not in source, "孤儿死函数 _chapter_ranges 还在"
    assert "[0.15, 0.40, 0.70, 0.90, 1.0]" not in source, (
        "setup_plot_outline_continuation 里还有一份硬编码比例表"
    )


def test_frontend_does_not_force_a_fixed_phase_name_back():
    """阶段名缺失时**不许**回贴成某个固定名（那正是 J-03 要拆的镜像）。

    ⚠ 判据是**不许出现「|| '固定名'」这种回贴写法**。
    """
    code = _frontend_code()

    for fixed in ("|| 'opening'", "|| 'development'", "|| 'climax'"):
        assert fixed not in code, f"前端又把阶段名回贴成固定值了: {fixed}"
    assert "|| fallbackStage.phase" in code, "阶段名该走按位置兜底"


def test_backend_uses_the_shared_stage_plan_invariants():
    """真的在用共享实现（不是自己另算一套）。

    ⚠ 判据是**调用点**（``chapter_ranges(...)``），不是「文件里出现过这个名字」——
    变异把调用换成自己算区间后，import 行的名字还在 ⇒ 查名字会假绿。
    """
    source = BACKEND.read_text(encoding="utf-8")

    assert "chapter_ranges(target_chapters, count)" in source, "断点区间没走共享实现"
    assert "validate_stage_plan_shape(" in source, "没走共享的形状校验"
    # 自己算区间的那份比例表不许回来
    assert "ratios = [" not in source, "后端又自己算了一份比例表"
