from application.blueprint.services.setup_main_plot_suggestion_service import (
    SetupMainPlotSuggestionService,
)


def _ctx():
    return {
        "target_chapters": 60,
        "fusion_axis": {
            "core_promise": "核心承诺",
            "central_conflict": "中心冲突",
            "false_mystery": "表层谜团",
            "true_mystery": "真实谜团",
            "forbidden_mainline_competitors": ["竞品A"],
            "taboos": ["禁忌A"],
        },
    }


def _three_options(sublines_block=None):
    """构造 3 个候选（合同要求恰好 3 个），可给第 1 个挂上 sublines。"""
    first = {
        "id": "option_a_survival",
        "type": "生存求证",
        "title": "绝境中的第一枪",
        "logline": "log",
        "core_conflict": "conflict",
        "starting_hook": "hook",
    }
    if sublines_block is not None:
        first["sublines"] = sublines_block
    others = [
        {
            "id": f"option_{x}",
            "type": "t",
            "title": "t",
            "logline": "l",
            "core_conflict": "c",
            "starting_hook": "h",
        }
        for x in ("b_conspiracy", "c_anomaly")
    ]
    return {"plot_options": [first, *others]}


def _svc():
    return SetupMainPlotSuggestionService.__new__(SetupMainPlotSuggestionService)


def _parse(sublines_block=None):
    svc = _svc()
    payload = _three_options(sublines_block)
    import json

    return svc.parse_suggested_options(json.dumps(payload, ensure_ascii=False), ctx=_ctx())


def test_parse_suggested_options_normalizes_declared_options():
    parsed = _parse()

    assert len(parsed) == 3
    assert parsed[0]["id"] == "option_a_survival"
    assert parsed[0]["main_axis"]
    assert parsed[0]["opening_pressure"]
    assert parsed[0]["forbidden_drift"]


def test_no_sublines_is_not_fabricated():
    """A-01 核心：这份 fixture 的 LLM 输出**没有** sublines。

    改造前代码会整条编造 —— id/name/role/purpose/description/guard 全部代写，
    ``merge_chapter`` 用 ``target_chapters * 0.22/0.3/0.26/0.25`` 四个公式算。
    于是「模型规划了支线」与「代码随手补了一条」在数据上完全一样。

    现在不编造：列表为空，且来源被显式标记。
    """
    parsed = _parse()

    assert parsed[0]["sublines"] == [], "LLM 没给支线时不应再编造一条"
    assert parsed[0]["sublines_source"] == "none", (
        "必须能分辨「模型没给」与「模型给了」"
    )


def test_ai_given_sublines_are_preserved_not_replaced():
    """反向对照：模型给了就**原样保留**。

    与上面那条配对 —— 一起钉住「不编造」不等于「丢弃」。
    """
    parsed = _parse([
        {
            "id": "my_line",
            "name": "我设计的支线",
            "role": "dark",
            "purpose": "服务主线",
            "description": "描述",
            "guard": "边界",
            "merge_chapter": 42,
        }
    ])

    assert parsed[0]["sublines_source"] == "ai"
    sub = parsed[0]["sublines"][0]
    assert sub["id"] == "my_line", "模型给的 id 被兜底覆盖了"
    assert sub["name"] == "我设计的支线", "模型给的 name 被兜底覆盖了"
    assert sub["role"] == "dark"
    assert sub["purpose"] == "服务主线"
    assert sub["guard"] == "边界"
    assert sub["merge_chapter"] == 42, "模型给的汇流章被改写了"
    assert parsed[0]["subline_issues"] == []


def test_merge_chapter_over_target_is_reported_not_silently_fixed():
    """不变量：汇流章 ≤ 目标章数。越界要**报出来**，不是默默改掉。"""
    parsed = _parse([{"id": "x", "name": "越界线", "merge_chapter": 999}])

    issues = parsed[0]["subline_issues"]
    assert issues, "越界应被报告"
    assert any("超过目标总章数" in i for i in issues), issues
    # 值保持原样，不替 AI 改
    assert parsed[0]["sublines"][0]["merge_chapter"] == 999


def test_missing_merge_chapter_is_not_zeroed():
    """没给汇流章 ⇒ None 且报出来。

    0 会同时违反「≤ 目标章数」与「晚于引入章」两条不变量，且完全静默。
    """
    parsed = _parse([{"id": "x", "name": "无线索章"}])

    sub = parsed[0]["sublines"][0]
    assert sub["merge_chapter"] is None, "没给汇流章不得塞 0"
    assert sub["merge_chapter_source"] == "none"
    assert any("merge_chapter" in i for i in parsed[0]["subline_issues"])


def test_merge_chapter_not_after_introduction_is_reported():
    """不变量：汇流章 > 引入章（第 1 章引入 ⇒ 不能在第 1 章汇流）。"""
    parsed = _parse([{"id": "x", "name": "同章线", "merge_chapter": 0}])

    assert any("不晚于引入章" in i for i in parsed[0]["subline_issues"]), (
        parsed[0]["subline_issues"]
    )
