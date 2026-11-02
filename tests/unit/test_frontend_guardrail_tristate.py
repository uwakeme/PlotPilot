# -*- coding: utf-8 -*-
"""第 81 批（N-03）：护栏路径的**三态**守卫（前端）。

⚠ 起因（独立验收）：通用三态守卫的 ``TRISTATE_FIELDS`` 只收两个字段名
  （``narrative_sync_ok`` / ``drift_alert``）⇒ **护栏路径根本没被守**
  ⇒ 前端把「未评估」画成红色 0 分，标签还说「✗ 未通过」，
     同屏还挂着绿色「所有维度检查通过」。

⚠ 前端**没有测试设施**（只有 ``npm run build``）⇒ 这里做**文件内容级**断言
  （与 ``test_frontend_tristate_consumers.py`` / ``test_frontend_dag_metrics_contract.py``
  同一纪律）。

本文件钉住
----------
1. 前端**类型**不许骗人（``overall_score`` / ``score`` 必须是可空）；
2. 可空分数**不许**直接做算术（``null * 100`` ⇒ 0 分）⇒ 必须走 null 安全 helper；
3. 「通过 / 未通过 / **未评估**」三态标签必须**显式判别** ``passed === null``；
4. 绿色「所有维度检查通过」**只在真的评过时**出现；
5. 着色 helper：未评估 ⇒ **中性色**，不是红色。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "src"
PANEL = FRONTEND / "components" / "workbench" / "QualityGuardrailPanel.vue"
TYPES = FRONTEND / "api" / "engineCore.ts"
DOMAIN = FRONTEND / "domain" / "chapterWriting.ts"

#: 护栏面板里「可空」的字段（null = 未评估）。
NULLABLE_FIELDS = ("overall_score", "passed", "score")

#: null 安全的 helper（面板必须经它们取值）。
NULL_SAFE_HELPERS = ("scorePercent(", "scoreText(", "scoreColor(", "isUnassessed(")


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _code_lines(text: str) -> list[tuple[int, str]]:
    """去注释后的代码行（注释里提到字段不算违规）。"""
    out: list[tuple[int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line.startswith("//") or line.startswith("*") or line.startswith("<!--"):
            continue
        out.append((number, raw))
    return out


def test_the_frontend_types_admit_null():
    """⚠ 类型不许骗人：后端会给 null（未评估）。"""
    types = _read(TYPES)
    assert "overall_score: number | null" in types, (
        "GuardrailCheckResponse.overall_score 没声明可空 ⇒ 前端把它当真值用"
    )
    assert "passed: boolean | null" in types, (
        "GuardrailCheckResponse.passed 没声明可空 ⇒ 未评估会被说成「未通过」"
    )
    assert re.search(r"score:\s*number\s*\|\s*null", types), (
        "GuardrailDimensionScore.score 没声明可空 ⇒ 未评估的维度会被当成 0 分"
    )


def test_no_arithmetic_is_done_directly_on_a_nullable_score():
    """⚠ ``Math.round(dim.score * 100)`` ⇒ null 变 0 分（未评估被当成零分）。

    ⚠ 必须经 ``scorePercent`` / ``scoreText`` ⇒ null ⇒ 「—」。
    """
    panel = _read(PANEL)
    offenders: list[str] = []
    for number, line in _code_lines(panel):
        for field in ("overall_score", "score"):
            # ⚠ 只看**成员访问**（``dim.score`` / ``lastReport.overall_score``）
            #   ⇒ helper 内部的裸参数名（``score``）不算（那里 null 已判过）。
            if not re.search(rf"\.\s*{field}\b", line):
                continue
            # 直接做算术（乘法/加法）⇒ 违规
            if not re.search(rf"\.\s*{field}\b\s*[*/+]", line) and not re.search(
                rf"[*/+]\s*\.\s*{field}\b", line
            ):
                continue
            if any(helper in line for helper in NULL_SAFE_HELPERS):
                continue
            offenders.append("%d: %s" % (number, line.strip()))
    assert not offenders, (
        "可空分数被直接做算术（null ⇒ 0 分 ⇒ 未评估被画成红色零分）：\n  "
        + "\n  ".join(offenders)
    )


def test_the_panel_has_null_safe_helpers_and_uses_them():
    """⚠ helper 必须存在且被用上（否则上面那条规则会「因为没有算术」而空过）。"""
    panel = _read(PANEL)
    for helper in ("function scorePercent(", "function scoreText("):
        assert helper in panel, f"面板缺少 null 安全 helper：{helper}"
    assert "scorePercent(lastReport.overall_score)" in panel, "总分没走 scorePercent"
    assert "scorePercent(dim.score)" in panel, "维度分没走 scorePercent"
    assert "scoreText(lastReport.overall_score)" in panel, "总分文字没走 scoreText"
    assert "scoreText(dim.score)" in panel, "维度分文字没走 scoreText"


def test_the_passed_tag_distinguishes_unassessed():
    """⚠ 「未评估」必须**自己一档**，不许被说成「未通过」。"""
    panel = _read(PANEL)
    assert "lastReport.passed === null" in panel, (
        "通过标签没有判别 passed === null ⇒ 未评估被显示成「✗ 未通过」"
    )
    assert "未评估" in panel, "面板没有「未评估」这个说法"


def test_the_green_all_passed_alert_is_guarded():
    """⚠ 绿色「所有维度检查通过」只在**真的评过**时出现。"""
    panel = _read(PANEL)
    green_at = panel.find("所有维度检查通过")
    assert green_at != -1, "找不到绿色结论块（结构变了？）"
    window = panel[max(0, green_at - 1200) : green_at]
    assert "lastReport.passed === null" in window, (
        "绿色「所有维度检查通过」没有用未评估判别挡住 ⇒ 未评估时也在说「通过」"
    )


def test_the_score_color_helper_treats_null_as_neutral():
    """⚠ 未评估 ⇒ **中性色**，不是红色（红 = 0 分 = 判了不及格）。"""
    domain = _read(DOMAIN)
    m = re.search(
        r"export function getGuardrailScoreColor\([^)]*\)[^{]*\{(.*?)\n\}",
        domain,
        re.S,
    )
    assert m, "找不到 getGuardrailScoreColor"
    body = m.group(1)
    assert "null" in body, "着色函数没有判 null ⇒ 未评估会被画成红色"
    assert "#ef4444" in body, "红色档没了（守卫失效？）"
    # null 分支必须**先于**红色返回
    assert body.find("null") < body.find("#ef4444"), (
        "null 判别在红色之后 ⇒ 未评估仍会落到红色"
    )
