"""Bible 注入 prompt 不许「砍完装作那就是全部」（B-09）。

改造前 `_format_bible_context` 的默认参数是 `character_limit=5,
location_limit=5`，五个调用点还不一致（宏观用 5、卷用 8）。而
`timeline_notes` 复用了 `location_limit`。

生产库实测（`data/plotpilot.db`，2026-10-17）：

| 表 | 实测 |
|---|---|
| `unified_characters` | 5 / 7 / 1 条（按书） |
| `bible_locations` | 10 / 10 条 |
| `bible_timeline_notes` | **2074 / 38 条** |

2074 条时间线事件只注入 5 条 —— 99.76% 被静默丢弃；且 `sort_order` 全为 0，
`[:5]` 取到的是任意切片。模型拿着 5 条时间线去规划整本书，却没有任何提示说
还有 2069 条没给它。

**上限本身是对的**，prompt 要有 token 约束。错的是砍完不吭声。所以本文件锁
两件事：

1. 实测数据量级（2074 条）不再被砍到只剩 5 条；
2. **砍掉必须留痕** —— 区块末尾出现「另有 N 条未注入」。

第 2 条比第 1 条重要。只放宽上限而不留痕，等于把「99.76% 丢弃」换成
「37% 丢弃」，模型照样不知道。
"""
from application.blueprint.services.bible_context_budget import (
    MACRO_PLANNING,
    SCENE_PLANNING,
    VOLUME_PLANNING,
    format_bible_context,
    format_locations_context,
    format_worldbuilding_context,
)


# 生产库实测体量，不是编的数
REAL_TIMELINE_NOTES = 2074
REAL_LOCATIONS = 10
REAL_CHARACTERS = 7


def _notes(n: int):
    return [{"event": f"事件{i}", "time_point": f"纪元{i}", "description": f"内容{i}"} for i in range(n)]


def _locations(n: int):
    return [{"name": f"地点{i}", "description": f"描述{i}"} for i in range(n)]


def _characters(n: int):
    return [{"name": f"角色{i}", "description": f"设定{i}"} for i in range(n)]


# ======================================================================
#  1. 不再砍到只剩 5 条
# ======================================================================


def test_macro_planning_keeps_far_more_than_five_timeline_notes():
    """实测 2074 条时间线，宏观规划至少要给出可用的规模 —— 不是 5。"""
    out = format_bible_context({"timeline_notes": _notes(REAL_TIMELINE_NOTES)}, budget=MACRO_PLANNING)

    kept = out.count("- 事件")
    assert kept > 100, f"宏观规划只保留了 {kept} 条时间线（改造前是 5）"
    assert "未注入" in out


def test_all_characters_are_kept_when_budget_allows():
    """实测 7 个角色、上限 5 —— 改造后不能只给模型看 5 个。"""
    out = format_bible_context({"characters": _characters(REAL_CHARACTERS)}, budget=MACRO_PLANNING)

    for i in range(REAL_CHARACTERS):
        assert f"- 角色{i}" in out
    assert "未注入" not in out  # 全给出去了，就不该留「有遗漏」的假痕迹


def test_all_locations_are_kept_when_budget_allows():
    out = format_bible_context({"locations": _locations(REAL_LOCATIONS)}, budget=MACRO_PLANNING)

    assert out.count("- 地点") == REAL_LOCATIONS


# ======================================================================
#  2. 砍掉必须留痕（本文件的核心）
# ======================================================================


def test_truncated_timeline_says_how_many_were_dropped():
    """预算不够时必须报数，且报的是真实丢掉的数量。"""
    out = format_bible_context(
        {"timeline_notes": _notes(REAL_TIMELINE_NOTES)}, budget=SCENE_PLANNING
    )

    kept = out.count("- 事件")
    assert "未注入" in out, "截断了却一声不吭 —— 模型会以为世界线就长这样"
    # 留痕里的数字必须与实际丢弃数一致，否则是在编造
    dropped_claim = int(out.split("另有 ")[1].split(" 条")[0])
    assert dropped_claim == REAL_TIMELINE_NOTES - kept


def test_no_truncation_marker_when_nothing_was_dropped():
    """没砍东西就不许出现「另有 N 条未注入」—— 假痕迹比没痕迹更坏。"""
    out = format_bible_context({"characters": _characters(3)}, budget=MACRO_PLANNING)

    assert "未注入" not in out


HEADERS = ("【世界观】", "【关键地点】", "【时间线事件】", "【世界设定】", "【角色设定】", "【角色关系】")


def _block(out: str, header: str) -> str:
    """取出某个命名区块的文本，与区块在文中出现的先后无关。

    区块顺序是刻意排的（见 `test_block_order_is_the_documented_priority`），
    所以不能靠 `split(other)[0]` 按位置切 —— 那样调整顺序后会给出误导性失败。
    """
    start = out.index(header)
    later = [out.index(h) for h in HEADERS if h != header and out.find(h) > start]
    return out[start : min(later)] if later else out[start:]


def test_truncation_is_reported_per_block_not_globally():
    """角色被砍就该在**角色块**里报数，不该让地点块背锅。

    角色描述要写够长才撑得破预算 —— 生产实测体量（2074 条时间线 × 平均
    55 字）下单条成本就是这个量级，样本太短会误判成「没超预算」。
    """
    out = format_bible_context(
        {
            "characters": [
                {"name": f"角色{i}", "description": "很长的角色设定文本" * 20}
                for i in range(500)
            ],
            "locations": _locations(2),
        },
        budget=MACRO_PLANNING,
    )

    char_block = _block(out, "【角色设定】")
    loc_block = _block(out, "【关键地点】")

    kept = char_block.count("- 角色")
    assert kept < 500, "样本没撑破预算，这条测试等于没测"
    assert f"另有 {500 - kept} 条角色设定未注入" in char_block
    assert "未注入" not in loc_block, "地点块没被砍，不该出现截断痕迹"


# ======================================================================
#  3. 预算真的收得住（上限本身不能被顺手删掉）
# ======================================================================


def test_char_budget_actually_bounds_the_output():
    """字符预算必须是硬约束 —— 2074 条全注入就是回到老问题。"""
    fat = [
        {"event": f"事件{i}", "time_point": "纪元", "description": "很长的描述文本" * 20}
        for i in range(5000)
    ]
    out = format_bible_context({"timeline_notes": fat}, budget=SCENE_PLANNING)

    # 粗略上界：留痕行 + 标题 + 余量
    assert len(out) <= SCENE_PLANNING.max_chars * 1.5, (
        f"输出 {len(out)} 字符，远超预算 {SCENE_PLANNING.max_chars}"
    )
    assert len(out) > SCENE_PLANNING.max_chars * 0.5, "预算收得太狠，几乎没注入"


def test_timeline_limit_is_an_independent_knob():
    """时间线有自己的条数上限（它是唯一会破千量级的区块）。"""
    out = format_bible_context(
        {"timeline_notes": _notes(5000)}, budget=VOLUME_PLANNING
    )

    kept = out.count("- 事件")
    assert kept <= VOLUME_PLANNING.timeline_limit


# ======================================================================
#  4. 三个预设的收窄意图要看得出来
# ======================================================================


def test_narrower_presets_inject_less_than_macro():
    """卷级 / 场景级刻意收窄（提高字段完整度），但仍是同一套预算逻辑。"""
    ctx = {"timeline_notes": _notes(5000), "characters": _characters(200)}

    macro = format_bible_context(ctx, budget=MACRO_PLANNING)
    volume = format_bible_context(ctx, budget=VOLUME_PLANNING)
    scene = format_bible_context(ctx, budget=SCENE_PLANNING)

    # 必须是**严格**收窄。用 <= 时，把 VOLUME_PLANNING 改回与 MACRO 同值
    # （也就是把两个预设合并）测试照样全绿 —— 而那正是本次要防的漂移。
    assert len(scene) < len(volume) < len(macro)


def test_presets_form_a_strict_narrowing_ladder():
    """预设之间必须是严格递减的两级阶梯，不允许有人把它们改成同值。"""
    assert SCENE_PLANNING.max_chars < VOLUME_PLANNING.max_chars < MACRO_PLANNING.max_chars
    assert (
        SCENE_PLANNING.timeline_limit
        < VOLUME_PLANNING.timeline_limit
        < MACRO_PLANNING.timeline_limit
    )


# ======================================================================
#  5. quick 规划的两个变量同样受预算约束
# ======================================================================


def test_locations_and_worldbuilding_helpers_leave_traces_too():
    """quick 规划的两个变量共用同一套预算，同一个截断留痕规矩。"""
    ctx = {
        "locations": [{"name": f"地点{i}", "description": "很长的地点描述" * 20} for i in range(400)],
        "timeline_notes": [{"event": f"事件{i}", "description": "很长的时间线描述" * 20} for i in range(3000)],
    }

    locs = format_locations_context(ctx, budget=SCENE_PLANNING)
    world = format_worldbuilding_context(ctx, budget=SCENE_PLANNING)

    assert "未注入" in locs
    assert "未注入" in world
    # 留痕里的数要与实际注入数对得上
    assert int(locs.split("另有 ")[1].split(" 条")[0]) == 400 - locs.count("- 地点")


# ======================================================================
#  7. 服务侧接线（否则上面所有守卫都测不到生产路径）
# ======================================================================


def test_quick_macro_variables_use_the_budgeted_formatter():
    """`build_quick_macro_variables` 是 quick 规划真正走的入口。

    上面那些守卫只锁 `bible_context_budget` 模块本身 —— 如果服务侧把调用
    换回硬编码 `character_limit=5`，模块守卫一条都不会红。所以这里从
    **生产接缝**（quick 规划的变量装配）上验一次。

    样本用生产实测比例（7 角色 / 10 地点 / 2074 时间线），不是极端胖数据：
    这条测的是 B-09 本身，不是区块优先级（那是下面那条）。
    """
    from unittest.mock import Mock

    from application.blueprint.services.continuous_planning_service import (
        ContinuousPlanningService,
    )

    svc = ContinuousPlanningService(
        story_node_repo=Mock(),
        chapter_element_repo=Mock(),
        llm_service=Mock(),
    )
    ctx = {
        "characters": [{"name": f"角色{i}", "description": "中等长度的角色设定" * 5} for i in range(REAL_CHARACTERS)],
        "locations": [{"name": f"地点{i}", "description": "中等长度的地点描述" * 5} for i in range(REAL_LOCATIONS)],
        "timeline_notes": _notes(REAL_TIMELINE_NOTES),
    }

    variables = svc.build_quick_macro_variables(ctx, target_chapters=100)

    worldview = variables["worldview"]
    assert worldview.count("- 事件") > 100, "生产路径仍在按 5 条截断时间线"
    assert "未注入" in worldview, "生产路径截断了却不留痕"
    assert variables["locations"]["list"].count("- 地点") > 5
    assert worldview.count("- 角色") == REAL_CHARACTERS


def test_block_order_is_the_documented_priority():
    """区块顺序 = 优先级顺序，必须把时间线排在角色前面。

    这是实测踩出来的：300 条长描述角色会吃光 24000 字符预算，把 2000 条
    时间线挤到只剩 1 条。改顺序等于改优先级，不该是无声发生的。
    """
    ctx = {
        "characters": [{"name": f"角色{i}", "description": "很长的角色设定" * 40} for i in range(300)],
        "timeline_notes": [{"event": f"事件{i}", "description": "中等长度的时间线" * 5} for i in range(2000)],
    }

    out = format_bible_context(ctx, budget=MACRO_PLANNING)

    i_locations = out.find("【关键地点】")
    i_timeline = out.find("【时间线事件】")
    i_characters = out.find("【角色设定】")
    assert -1 < i_timeline < i_characters, "时间线必须排在角色之前，否则会被饿死"
    assert out.count("- 事件") > 100, "角色把预算吃光后时间线几乎没注入"


def test_service_module_has_no_hardcoded_character_limits():
    """静态守卫：服务模块里不得再出现 `character_limit=` / `location_limit=`。

    用 ast 精确比对调用实参名，不用 `contains` 子串匹配 —— `contains` 会把
    `budget.character_limit` 之类的合法引用一起命中。
    """
    import ast
    import pathlib

    import application.blueprint.services.continuous_planning_service as svc_mod

    tree = ast.parse(pathlib.Path(svc_mod.__file__).read_bytes().decode("utf-8-sig"))
    forbidden = {"character_limit", "location_limit", "relationship_limit", "world_setting_limit"}
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in forbidden:
                    hits.append((kw.arg, node.lineno))

    assert hits == [], f"服务里仍有硬编码条数上限: {hits}"


# ======================================================================
#  8. 空数据仍走 empty_hint，且不出现截断痕迹
# ======================================================================


def test_empty_context_falls_back_to_hint_without_truncation_noise():
    out = format_bible_context({}, budget=MACRO_PLANNING)

    assert "暂无详细设定" in out
    assert "未注入" not in out
