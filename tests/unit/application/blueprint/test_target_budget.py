"""J-01：目标章数与终局预算的提案/钳制下沉到后端 + 终局假承诺修复。

红线（本文件负责钉死）：

1. **提案在后端一处** —— 前端不再自己算「再写多少章」。
2. **上限由后端钳** —— ``target_chapters`` 有硬上限（不靠前端输入框）。
3. **终局预算 [5, 200] 钳制** —— 写路径与读路径共用同一个函数。
4. **终局不假承诺** —— ask 策略下用户选过「写终局」后，预算写完**真正完结**，
   而不是再把用户问一遍。
5. **既有策略行为不变** —— continue / finale / complete 三条路径口径不动。
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from application.blueprint.services.target_budget import (
    AUTO_EXTEND_BLOCK_MIN,
    CONTINUE_BLOCK_MIN,
    FINALE_BUDGET_DEFAULT,
    FINALE_BUDGET_MAX,
    FINALE_BUDGET_MIN,
    TARGET_CHAPTERS_MAX,
    auto_extend_block,
    clamp_finale_budget,
    clamp_target_chapters,
    propose_continue_block,
    propose_continue_target,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]


# ── 1. 终局预算钳制 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [(1, 5), (5, 5), (20, 20), (200, 200), (999, 200), (0, 5), (-3, 5)],
)
def test_finale_budget_is_clamped_to_the_invariant_range(raw, expected):
    assert clamp_finale_budget(raw) == expected


def test_finale_budget_unparsable_falls_back_to_the_default():
    assert clamp_finale_budget("不是数字") == FINALE_BUDGET_DEFAULT
    assert clamp_finale_budget(None) == FINALE_BUDGET_DEFAULT


def test_preferences_write_path_uses_the_shared_clamp():
    """写路径（merge_patch）也必须钳 —— 旧实现只在读取时钳。"""
    from domain.novel.value_objects.generation_preferences import (
        GenerationPreferences,
    )

    base = GenerationPreferences()
    patched = GenerationPreferences.merge_patch(base, {"finale_chapter_budget": 9999})

    assert patched.finale_chapter_budget == FINALE_BUDGET_MAX

    patched_low = GenerationPreferences.merge_patch(base, {"finale_chapter_budget": 1})
    assert patched_low.finale_chapter_budget == FINALE_BUDGET_MIN


# ── 2. 目标章数上限（后端不变量） ───────────────────────────────────


def test_target_chapters_has_a_backend_ceiling():
    assert clamp_target_chapters(10 ** 9) == TARGET_CHAPTERS_MAX
    assert clamp_target_chapters(300) == 300
    assert clamp_target_chapters(0) == 0
    assert clamp_target_chapters("不是数字") == 0


def test_update_request_rejects_a_target_above_the_ceiling():
    """API 层也要拒 —— 上限不能只靠业务函数自觉。"""
    from pydantic import ValidationError

    from interfaces.api.v1.core.novels import UpdateNovelRequest

    UpdateNovelRequest(target_chapters=TARGET_CHAPTERS_MAX)
    with pytest.raises(ValidationError):
        UpdateNovelRequest(target_chapters=TARGET_CHAPTERS_MAX + 1)


# ── 3. 提案口径（与旧前端逐位相同，但住在后端）─────────────────────


def test_continue_block_matches_the_old_frontend_formula():
    """旧前端：``max(20, round(target * 0.2))``。"""
    assert propose_continue_block(target_chapters=250) == max(
        CONTINUE_BLOCK_MIN, round(250 * 0.2)
    )
    assert propose_continue_block(target_chapters=250) == 50
    assert propose_continue_block(target_chapters=0) == CONTINUE_BLOCK_MIN


def test_continue_target_matches_the_old_frontend_formula():
    """旧前端：``done + max(20, round(target * 0.2))``。"""
    assert propose_continue_target(
        target_chapters=250, completed_chapters=300
    ) == 300 + max(CONTINUE_BLOCK_MIN, round(250 * 0.2))


def test_auto_extend_block_keeps_the_gate_formula():
    """闸门的自动续写口径不动（比手动保守，避免无人值守时无限膨胀）。"""
    assert auto_extend_block(target_chapters=300) == max(
        AUTO_EXTEND_BLOCK_MIN, round(300 * 0.1)
    )
    assert auto_extend_block(target_chapters=0) == AUTO_EXTEND_BLOCK_MIN


# ── 4. 前端不再自己算公式 ──────────────────────────────────────────


def test_frontend_no_longer_computes_the_continue_target():
    panel = (
        REPO_ROOT / "frontend/src/components/autopilot/AutopilotPanel.vue"
    ).read_bytes().decode("utf-8")

    assert "Math.round((targetChapters.value || 0) * 0.2)" not in panel
    assert "targetBudgetProposal" in panel


# ── 5. 终局不假承诺：ask + finale_mode ⇒ 真正完结 ──────────────────


def _make_novel(**prefs_kwargs):
    from domain.novel.value_objects.generation_preferences import GenerationPreferences

    return SimpleNamespace(
        novel_id=SimpleNamespace(value="n-1"),
        target_chapters=300,
        generation_prefs=GenerationPreferences(**prefs_kwargs),
    )


def _make_host(completed=300):
    host = MagicMock()
    host._count_completed_chapters.return_value = completed
    return host


def test_ask_with_finale_mode_completes_instead_of_asking_again():
    """用户选过「写终局」后，预算写完 ⇒ 真正完结（不再是假承诺 + 反复打断）。"""
    from engine.runtime.target_reached_gate import resolve_target_reached

    novel = _make_novel(finale_mode=True)
    host = _make_host(completed=330)

    assert resolve_target_reached(host, novel, 330) == "complete"
    assert novel.generation_prefs.finale_mode is False
    host._save_novel_state.assert_called_once()


def test_ask_without_finale_mode_still_pauses():
    from engine.runtime.target_reached_gate import resolve_target_reached

    novel = _make_novel()
    host = _make_host(completed=300)

    assert resolve_target_reached(host, novel, 300) == "pause"
    assert novel.generation_prefs.finale_mode is False
    host._save_novel_state.assert_not_called()


def test_continue_policy_keeps_its_block_size():
    from engine.runtime.target_reached_gate import resolve_target_reached

    novel = _make_novel(target_reached_policy="continue")
    host = _make_host(completed=300)

    assert resolve_target_reached(host, novel, 300) == "extended"
    assert novel.target_chapters == 330  # 300 + max(10, 300*0.1)


def test_finale_policy_keeps_its_budget_behaviour():
    from engine.runtime.target_reached_gate import resolve_target_reached

    novel = _make_novel(target_reached_policy="finale")
    host = _make_host(completed=300)

    assert resolve_target_reached(host, novel, 300) == "extended"
    assert novel.target_chapters == 320  # 300 + 20（默认终局预算）
    assert novel.generation_prefs.finale_mode is True


# ── 6. 提案端点存在且返回后端口径 ─────────────────────────────────


def test_proposal_endpoint_is_registered():
    source = (
        REPO_ROOT / "interfaces/api/v1/core/novels.py"
    ).read_bytes().decode("utf-8")
    tree = ast.parse(source)
    paths = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(target, ast.Attribute) and target.attr == "get":
                    for arg in dec.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            paths.add(arg.value)
    assert "/{novel_id}/target-budget-proposal" in paths
