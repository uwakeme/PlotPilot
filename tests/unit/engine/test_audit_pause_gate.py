"""章末审阅闸门必须区分「评估过且不严重」与「本次没能评估」。

背景：`_run_anti_ai_audit` 在审计失败时按其 docstring 约定返回 `None`。
此前 `audit_delegate` 写的是

    anti_assessment = None            # 未评估 与 「不严重」同形
    anti_ai_severe = anti_assessment == "严重"    # None → False

于是**审计崩了会被当成「没问题」**，用户显式开启的 `audit_pause_on_anti_ai_severe`
静默失效 —— 配了保护却不起作用，比没配更糟。

同一函数上一行的 `hard_tension = not bool(drift_result.get("tension_evaluated", False))`
走的是**相反**方向（未评估即视为硬伤）。两处对「没测出来」的处理不该分裂。

修法：未评估用显式可见值 `ANTI_AI_UNASSESSED`（「未评估」）承载，并计入暂停条件。
`PAUSED_FOR_REVIEW` 是可恢复状态（下一 tick 继续），不会把书写死。
"""
from engine.runtime.audit_delegate import (
    ANTI_AI_UNASSESSED,
    _resolve_audit_pause_gate,
)
from domain.novel.value_objects.generation_preferences import GenerationPreferences


def _gate(*, anti_evaluated, anti_assessment, anti_pref, auto=False,
          hard_narrative=False, hard_voice=False, hard_tension=False):
    prefs = GenerationPreferences(
        audit_pause_on_anti_ai_severe=anti_pref,
        audit_pause_on_hard_fail=False,
        pause_after_each_chapter_audit=False,
    )
    return _resolve_audit_pause_gate(
        auto=auto,
        prefs=prefs,
        hard_narrative=hard_narrative,
        hard_voice=hard_voice,
        hard_tension=hard_tension,
        anti_evaluated=anti_evaluated,
        anti_assessment=anti_assessment,
    )


# ── 缺陷本体 ────────────────────────────────────────────────────────────────

def test_audit_failure_still_triggers_the_gate_user_turned_on():
    """审计没跑成时，用户开启的保护必须仍然拦住 —— 不能静默放行。"""
    pause_gate, _hard, triggered, _reason = _gate(
        anti_evaluated=False, anti_assessment=ANTI_AI_UNASSESSED, anti_pref=True
    )
    assert pause_gate is True, (
        "反 AI 审计失败被当成「没问题」，audit_pause_on_anti_ai_severe 静默失效"
    )
    assert triggered is True


def test_audit_failure_is_not_reported_as_severe():
    """反向守卫：因「未评估」而停时，理由不得写成「严重」。

    否则只是把「故障伪装成正常」换成「故障伪装成严重」——AI 味严重会
    驱动用户按错误方向去改文风，而真实原因是审计闸门压根没跑。
    """
    _pause, _hard, _triggered, reason = _gate(
        anti_evaluated=False, anti_assessment=ANTI_AI_UNASSESSED, anti_pref=True
    )
    assert "未评估" in reason
    assert "严重" not in reason, f"未评估被报成了严重：{reason}"


def test_unassessed_sentinel_is_outside_the_normal_assessment_range():
    """哨兵值必须落在正常档位取值域之外，否则无法与真实结论区分。"""
    normal = {"良好", "轻微", "中等", "严重"}
    assert ANTI_AI_UNASSESSED not in normal
    assert ANTI_AI_UNASSESSED == "未评估"


# ── 不许把闸门修成「审计一失败就无条件停」 ──────────────────────────────────

def test_audit_failure_does_not_pause_when_user_did_not_opt_in():
    """没开这个开关的用户，不该因为审计失败被拦。"""
    pause_gate, _hard, triggered, _reason = _gate(
        anti_evaluated=False, anti_assessment=ANTI_AI_UNASSESSED, anti_pref=False
    )
    assert pause_gate is False, "闸门变成了审计一失败就无条件暂停"
    assert triggered is False


def test_auto_approve_mode_is_not_paused_by_audit_failure():
    """自动批准模式下不进入待审阅，与既有语义一致。"""
    pause_gate, _hard, _triggered, _reason = _gate(
        anti_evaluated=False, anti_assessment=ANTI_AI_UNASSESSED, anti_pref=True, auto=True
    )
    assert pause_gate is False


# ── 正常路径不得回归 ────────────────────────────────────────────────────────

def test_severe_assessment_still_pauses():
    _pause, _hard, triggered, reason = _gate(
        anti_evaluated=True, anti_assessment="严重", anti_pref=True
    )
    assert triggered is True
    assert reason == "Anti-AI严重"


def test_clean_assessment_does_not_pause():
    """评估过且结论不严重 —— 这是「真的没问题」，必须放行。"""
    pause_gate, _hard, triggered, reason = _gate(
        anti_evaluated=True, anti_assessment="良好", anti_pref=True
    )
    assert pause_gate is False, "评估通过却被拦"
    assert triggered is False
    assert reason == "Anti-AI未触发"


def test_hard_fail_path_is_untouched():
    """张力未评估仍按硬伤处理（fail-closed），本改动不得影响它。"""
    pause_gate, hard_fail, _triggered, _reason = _gate(
        anti_evaluated=True, anti_assessment="良好", anti_pref=False, hard_tension=True
    )
    assert hard_fail is True
