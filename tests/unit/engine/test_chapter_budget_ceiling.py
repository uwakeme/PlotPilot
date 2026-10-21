"""章节预算封顶：目标章数必须真的约束住"还能写几章、开几幕"。

复现的真实故障：《测试小说6》目标 5 章，最终写到 53 章 / 9 幕。
根因三层，本文件逐层锁死：

1. 结构引擎放大目标（5 章 → 3 幕 × 5 章 = 15 章）
   → ``test_structure_params_never_inflate_small_targets``
2. 宏观规划产出的每幕章数预算在落库时被丢弃
   → ``test_flatten_carries_act_budget_and_content``
3. 幕阶梯没有终点：写完一幕就再开一幕，且没有"最后一幕"
   → ``test_run_act_planning_refuses_act_beyond_structure_cap``
   → ``test_run_act_planning_stops_when_target_reached``
   → ``test_find_next_chapter_stops_at_target_ceiling``
   → ``test_act_prompt_marks_final_act_as_convergence``
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
)
from application.blueprint.services.structure_budget import (
    calculate_structure_params,
    resolve_act_ceiling,
    resolve_chapter_ceiling,
)
from domain.novel.entities.novel import NovelStage
from engine.runtime import act_planning_delegate
from engine.runtime.act_planning_delegate import _observed_acts_per_volume


# ======================================================================
# 1. 结构引擎：不得放大目标
# ======================================================================


@pytest.mark.parametrize(
    "target", [1, 3, 5, 8, 10, 12, 15, 20, 30, 45, 60, 100, 300, 600]
)
def test_structure_params_never_inflate_small_targets(target):
    """结构总章数最多比目标高 25%（向上取整余数），不允许数倍膨胀。"""
    params = calculate_structure_params(target)
    budget = params["total_chapter_budget"]
    assert budget >= target, "宁可略超，不可欠章（否则故事写不满目标）"
    assert budget <= max(target * 1.25, target + 1), (
        f"target={target} 被放大成 {budget} 章（{params['reasoning']}）"
    )


def test_five_chapter_target_plans_a_five_chapter_book():
    """用户设定的 5 章：结构上就是一个完整叙事弧，而不是 3 幕 × 5 章 = 15 章。"""
    params = calculate_structure_params(5)
    assert params["total_acts"] == 1
    assert params["total_chapter_budget"] == 5


def test_structure_params_tolerates_garbage_target():
    """脏目标值不得让结构计算抛异常。"""
    for bad in (None, 0, -3):
        params = calculate_structure_params(bad)
        assert params["total_acts"] >= 1
        assert params["chapters_per_act"] >= 1


def test_resolve_chapter_ceiling():
    assert resolve_chapter_ceiling(5) == 5
    assert resolve_chapter_ceiling(0) is None
    assert resolve_chapter_ceiling(None) is None
    assert resolve_chapter_ceiling("junk") is None


# ======================================================================
# 1b. 结构形状交给 LLM：引擎不再用公式网格决定部/卷/幕的层数
# ======================================================================


def test_act_ceiling_follows_llm_planned_acts_not_formula_grid():
    """幕数上限取「章节预算 + 已落库幕数」，与公式网格无关。

    这是本次改动的核心断言：结构层数由 LLM 依据剧情容量决定，引擎只保证
    "幕数不超过章节预算"这条不变量。任何让上限重新依赖 calculate_structure_params
    的改动都会让本用例失败。
    """
    # 5 章的书：LLM 规划 8 幕 → 上限抬到 8（尊重已落库结构），不再被公式的 1 幕卡死
    assert resolve_act_ceiling(5, 8) == 8
    # 300 章的书：LLM 只规划 6 幕 → 上限 300，仍可继续开新幕（不会被公式的 38 幕腰斩）
    assert resolve_act_ceiling(300, 6) == 300
    # 目标缺失时退回归格估算，绝不返回"无上限"
    assert resolve_act_ceiling(None, 3) >= 3


def test_act_ceiling_never_below_one():
    for target, planned in ((0, 0), (None, 0), (5, 0), ("junk", "junk")):
        assert resolve_act_ceiling(target, planned) >= 1


def test_quick_macro_prompt_has_no_hardcoded_grid():
    """宏观规划提示词不得再出现"建议共 N 部 × 每部 N 卷 × 每卷 N 幕"这类写死网格。"""
    from pathlib import Path

    import yaml

    node_dir = (
        Path(__file__).resolve().parents[3]
        / "infrastructure" / "ai" / "prompt_packages" / "nodes" / "planning-quick-macro"
    )
    system_md = (node_dir / "system.md").read_text(encoding="utf-8")

    for alias in (
        "rec_parts",
        "rec_volumes_per_part",
        "rec_acts_per_volume",
        "rec_chapters_per_act",
        "total_recommended_acts",
    ):
        assert f"{{{alias}}}" not in system_md, f"系统提示仍在用公式网格变量 {alias}"

    # 唯一硬约束是总章数
    assert "{target_chapters}" in system_md
    # package.yaml 里这些变量已不再是必填（模板不再引用）
    manifest = yaml.safe_load((node_dir / "package.yaml").read_text(encoding="utf-8"))
    required = {
        v["name"] for v in manifest.get("variables", []) if v.get("required")
    }
    assert not required & {
        "rec_parts",
        "rec_volumes_per_part",
        "rec_acts_per_volume",
        "rec_chapters_per_act",
        "total_recommended_acts",
    }


def test_quick_macro_variables_do_not_invent_a_grid():
    """运行时变量不再按公式算出推荐网格，只保留规划深度。"""
    svc = ContinuousPlanningService(
        story_node_repo=Mock(), chapter_element_repo=Mock(), llm_service=Mock()
    )
    vars_ = svc.build_quick_macro_variables({}, 5)
    assert vars_["planning_depth"] == "full"
    assert vars_["target_chapters"] == 5
    # 网格变量退化为占位常量，不再是公式产物
    assert vars_["rec_parts"] == 1
    assert vars_["total_recommended_acts"] == 1


def test_observed_total_acts_reads_persisted_structure():
    """收束幕判定必须读实际落库幕数，而不是公式预测值。"""
    acts = [
        SimpleNamespace(node_type=SimpleNamespace(value="act")),
        SimpleNamespace(node_type=SimpleNamespace(value="act")),
        SimpleNamespace(node_type=SimpleNamespace(value="volume")),
    ]
    svc = ContinuousPlanningService(
        story_node_repo=SimpleNamespace(
            get_tree_sync=Mock(
                return_value=SimpleNamespace(nodes=acts),
            )
        ),
        chapter_element_repo=Mock(),
        llm_service=Mock(),
    )
    assert svc._observed_total_acts("n-1") == 2

    # 读失败退化为 0，由调用方回退公式（不崩）
    svc.story_node_repo.get_tree_sync = Mock(side_effect=RuntimeError("boom"))
    assert svc._observed_total_acts("n-1") == 0
    assert svc._observed_total_acts("") == 0


def test_observed_acts_per_volume_follows_llm_plan():
    """换卷节奏取已落库的卷内幕数，避免公式网格把 LLM 规划的卷拦腰截断。"""
    vols = [
        SimpleNamespace(id="vol-1"),
        SimpleNamespace(id="vol-2"),
    ]
    # LLM 在第 1 卷规划了 6 幕，公式只说 3 幕 → 应以 6 为准
    acts = [SimpleNamespace(parent_id="vol-1") for _ in range(6)]
    assert _observed_acts_per_volume(acts, vols, fallback=3) == 6

    # 还没有任何幕时退回公式兜底
    assert _observed_acts_per_volume([], vols, fallback=3) == 3
    # B-08：公式更大时**也不上调** —— 公式是兜底，不是地板。
    # 旧断言是 `== 9`，那正是「公式当地板」的写法：它让公式在有观测值时
    # 仍能改写 LLM 规划的换卷节奏，与本函数 docstring
    # （「没有任何卷已有幕时退回公式兜底」）和登记表 B-08
    # （「观测值优先、公式仅兜底」）都相悖。
    # 新断言：观测到 6 幕就是 6 幕，公式的 9 不参与。
    assert _observed_acts_per_volume(acts, vols, fallback=9) == 6


def test_observed_acts_per_volume_takes_max_across_volumes():
    """反向守卫：「观测取 max」的语义不许被改（多个卷时取**最满**那一卷）。"""
    vols = [
        SimpleNamespace(id="vol-1"),
        SimpleNamespace(id="vol-2"),
    ]
    acts = [SimpleNamespace(parent_id="vol-1") for _ in range(2)]
    acts += [SimpleNamespace(parent_id="vol-2") for _ in range(5)]
    assert _observed_acts_per_volume(acts, vols, fallback=1) == 5


def test_observed_acts_per_volume_excludes_unknown_parent_when_a_known_volume_matches():
    """父卷不在卷列表里的幕**不参与**观测（脏数据不许污染换卷节奏）。"""
    vols = [SimpleNamespace(id="vol-1")]
    acts = [SimpleNamespace(parent_id="vol-1") for _ in range(2)]
    acts += [SimpleNamespace(parent_id="vol-ghost") for _ in range(9)]
    assert _observed_acts_per_volume(acts, vols, fallback=1) == 2


def test_observed_acts_per_volume_falls_back_to_all_counts_when_no_volume_matches():
    """一个已知卷都没匹配上时整批回退（脏数据不许把节奏压成 0）。"""
    vols = [SimpleNamespace(id="vol-1")]
    acts = [SimpleNamespace(parent_id="vol-ghost") for _ in range(4)]
    assert _observed_acts_per_volume(acts, vols, fallback=1) == 4


# ======================================================================
# 2. 落库：预算与幕内容不能丢
# ======================================================================


def _structure_with_acts():
    return [
        {
            "title": "第一部",
            "description": "缘起",
            "volumes": [
                {
                    "title": "第一卷",
                    "description": "卷简介",
                    "acts": [
                        {
                            "title": "第一幕",
                            "description": "幕简介",
                            "estimated_chapters": "6",  # LLM 常返回字符串
                            "narrative_goal": "主角查出幕后黑手",
                            "plot_points": ["潜入", "对峙"],
                            "emotional_arc": "紧张→爆发",
                            "core_conflict": "身份暴露",
                        },
                        {
                            "title": "第二幕",
                            "description": "收尾",
                            "estimated_chapters": 4,
                            "narrative_goal": "了结恩怨",
                        },
                    ],
                }
            ],
        }
    ]


def test_flatten_carries_act_budget_and_content():
    """LLM 规划出的每幕章数与内容必须落到节点上，否则只能回落硬编码默认值。"""
    svc = ContinuousPlanningService(
        story_node_repo=Mock(), chapter_element_repo=Mock(), llm_service=Mock()
    )

    nodes = svc._flatten_structure_to_nodes("n-1", _structure_with_acts())
    acts = [n for n in nodes if n["node_type"] == "act"]

    assert [n["title"] for n in acts] == ["第一幕", "第二幕"]

    first = acts[0]
    # "6"（字符串）必须被规整成整数 6
    assert first["suggested_chapter_count"] == 6
    assert first["content"] == "主角查出幕后黑手"
    assert first["key_events"] == ["潜入", "对峙"]
    assert first["narrative_arc"] == "紧张→爆发"
    assert first["conflicts"] == ["身份暴露"]

    # 原此处断言 metadata["is_final_act"]，那是登记表 C-04 登记的死通道：
    # 该键写进 metadata 后全仓无任何读取方，幕级规划并不据此判断收束——
    # 它走 _act_budget_context 的「幕号 vs 结构总幕数」统一重算
    # （continuous_planning_service.py 的 is_final = act_number >= total_acts）。
    # 断言它等于把死通道钉死在测试里，故随该键一并移除；
    # 收束判定的覆盖已由 test_dead_channels.py 的
    # test_act_budget_context_ignores_stale_metadata_final_flag 承担。


def test_flatten_act_budget_absent_leaves_none():
    """LLM 没给章数时保持 None（由调用方决定回落），不要伪造 0。"""
    svc = ContinuousPlanningService(
        story_node_repo=Mock(), chapter_element_repo=Mock(), llm_service=Mock()
    )
    structure = [
        {"title": "部", "volumes": [{"title": "卷", "acts": [{"title": "幕"}]}]}
    ]
    nodes = svc._flatten_structure_to_nodes("n-1", structure)
    act = next(n for n in nodes if n["node_type"] == "act")
    assert act["suggested_chapter_count"] is None


# ======================================================================
# 3. 幕阶梯封顶 + 终局
# ======================================================================


def _node(node_id, number, node_type, parent_id, **extra):
    return SimpleNamespace(
        id=node_id,
        number=number,
        node_type=SimpleNamespace(value=node_type),
        parent_id=parent_id,
        **extra,
    )


def _act_planning_host(nodes):
    host = Mock()
    host._is_still_running.return_value = True
    host.story_node_repo.get_by_novel = AsyncMock(return_value=nodes)
    host.story_node_repo.get_children_sync.return_value = []
    host._count_completed_chapters = Mock(return_value=0)
    host.planning_service.create_next_act_auto = AsyncMock(
        return_value={"success": True, "next_act": {"parent_id": "vol-1", "number": 99}}
    )
    host.planning_service.confirm_act_planning = AsyncMock()
    return host


def _novel(target, current_act):
    novel = Mock()
    novel.novel_id.value = "n-1"
    novel.target_chapters = target
    novel.current_act = current_act
    novel.current_stage = NovelStage.ACT_PLANNING
    return novel


@pytest.mark.asyncio
async def test_run_act_planning_refuses_act_beyond_structure_cap(monkeypatch):
    """幕数达到结构上限后不得再开新幕——这是"无限阶梯"的直接堵点。

    上限口径已从"公式网格预测的总幕数"改为"章节预算封顶 + 已落库幕数下界"
    （见 resolve_act_ceiling）：部/卷/幕层数由 LLM 决定，公式不再参与封顶。
    本例复刻《测试小说6》的失败形态：目标 5 章，LLM 规划了 8 幕且已全部开完。
    """
    target = 5
    nodes = [
        _node("vol-1", 1, "volume", "part-1", title="卷一"),
        *[_node(f"act-{n}", n, "act", "vol-1") for n in range(1, 9)],
    ]
    host = _act_planning_host(nodes)
    novel = _novel(target, 8)  # current_act=8 → 要找第 9 幕

    monkeypatch.setattr(act_planning_delegate, "_read_shared_state", lambda _nid: {})

    await act_planning_delegate.run_act_planning(host, novel)

    host.planning_service.create_next_act_auto.assert_not_awaited()
    # 结构写尽且没有待写章节 → 停下来等用户决定，不能在 WRITING/ACT_PLANNING 间空转
    assert novel.autopilot_status.name == "STOPPED"
    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    host._update_shared_state.assert_called_with(
        "n-1", autopilot_pause_reason=act_planning_delegate.STRUCTURE_EXHAUSTED_PAUSE_REASON
    )


@pytest.mark.asyncio
async def test_run_act_planning_opens_act_when_llm_planned_fewer_acts(monkeypatch):
    """LLM 只规划了 2 幕、但章节预算远未用完时，必须允许继续开新幕。

    这是"结构交给 LLM 决定"的正向断言：如果引擎仍拿公式网格当上限，
    300 章的书会被按 38 幕封顶而提前腰斩，或者 5 章的书被按 1 幕封顶而写不满。
    """
    target = 300
    nodes = [
        _node("vol-1", 1, "volume", "part-1", title="卷一"),
        _node("act-1", 1, "act", "vol-1"),
        _node("act-2", 2, "act", "vol-1"),
        # 前两幕各写满 8 章，远未触及 300 章目标
        *[_node(f"ch-{n}", n, "chapter", "act-2", word_count=2000) for n in range(1, 17)],
    ]
    host = _act_planning_host(nodes)
    host._count_completed_chapters = Mock(return_value=16)
    novel = _novel(target, 2)  # 要找第 3 幕

    monkeypatch.setattr(act_planning_delegate, "_read_shared_state", lambda _nid: {})
    host._find_parent_volume_for_new_act = Mock(return_value=None)

    await act_planning_delegate.run_act_planning(host, novel)

    # 预算充足 → 不应停在 structure_exhausted，而应走到建幕/父卷选择分支
    assert novel.current_stage != NovelStage.PAUSED_FOR_REVIEW


@pytest.mark.asyncio
async def test_run_act_planning_hands_back_to_audit_when_target_reached(monkeypatch):
    """写满目标且无剩余章节 → 交回章末审计做目标达成分流，而不是自行决定完结。"""
    target = 5
    nodes = [
        _node("vol-1", 1, "volume", "part-1", title="卷一"),
        _node("act-1", 1, "act", "vol-1"),
        # 5 章全部已写（word_count > 0），且没有第 6 章
        *[_node(f"ch-{n}", n, "chapter", "act-1", word_count=2000) for n in range(1, 6)],
    ]
    host = _act_planning_host(nodes)
    host._count_completed_chapters = Mock(return_value=5)
    novel = _novel(target, 1)

    monkeypatch.setattr(act_planning_delegate, "_read_shared_state", lambda _nid: {})

    await act_planning_delegate.run_act_planning(host, novel)

    host.planning_service.create_next_act_auto.assert_not_awaited()
    assert novel.current_stage == NovelStage.AUDITING


@pytest.mark.asyncio
async def test_run_act_planning_keeps_writing_when_pending_chapters_remain(monkeypatch):
    """封顶后若仍有预算内未动笔的章节，应继续写本幕而不是停机。"""
    target = 5
    nodes = [
        _node("vol-1", 1, "volume", "part-1", title="卷一"),
        _node("act-1", 1, "act", "vol-1"),
        _node("ch-1", 1, "chapter", "act-1", word_count=2000),
        _node("ch-2", 2, "chapter", "act-1", word_count=0),  # 待写
    ]
    host = _act_planning_host(nodes)
    host._count_completed_chapters = Mock(return_value=1)
    novel = _novel(target, 1)

    monkeypatch.setattr(act_planning_delegate, "_read_shared_state", lambda _nid: {})

    await act_planning_delegate.run_act_planning(host, novel)

    host.planning_service.create_next_act_auto.assert_not_awaited()
    assert novel.current_stage == NovelStage.WRITING


@pytest.mark.asyncio
async def test_run_act_planning_still_creates_act_within_budget(monkeypatch):
    """预算充足时必须照常建幕——封顶不能把正常长篇写作也掐死。"""
    target = 300  # 结构：38 幕
    nodes = [
        _node("vol-1", 1, "volume", "part-1", title="卷一"),
        *[_node(f"act-{n}", n, "act", "vol-1") for n in range(1, 6)],
    ]
    host = _act_planning_host(nodes)
    novel = _novel(target, 5)  # 要找第 6 幕

    monkeypatch.setattr(act_planning_delegate, "_read_shared_state", lambda _nid: {})
    host._find_parent_volume_for_new_act = Mock(return_value=None)

    await act_planning_delegate.run_act_planning(host, novel)

    # 无可用父卷 → 不会建幕；但关键是"因为没有卷"，不是"因为超预算"
    host.planning_service.create_next_act_auto.assert_not_awaited()


# ======================================================================
# 4. 单章硬闸
# ======================================================================


@pytest.mark.asyncio
async def test_find_next_chapter_stops_at_target_ceiling():
    """写作管线不得定位到超出目标章数的章节。"""
    from engine.pipeline.base import BaseStoryPipeline

    class _Pipeline(BaseStoryPipeline):
        async def _step_generate(self, ctx):  # pragma: no cover - 未走到
            raise AssertionError("不应进入生成步骤")

    pipeline = _Pipeline()
    chapter_nodes = [
        _node(f"ch-{n}", n, "chapter", "act-1", outline="大纲", description="", title="")
        for n in range(1, 9)
    ]
    # 第 1~5 章已完稿：目标 5 章已写满，第 6~8 章超上限，一律不得开写
    repo = SimpleNamespace(
        get_by_novel_and_number=Mock(
            side_effect=lambda _nid, number: (
                SimpleNamespace(status="completed") if number <= 5 else None
            )
        )
    )
    ctx = SimpleNamespace(
        story_node_repo=SimpleNamespace(get_by_novel=AsyncMock(return_value=chapter_nodes)),
        chapter_repository=repo,
        novel_repository=SimpleNamespace(
            get_by_id=Mock(return_value=SimpleNamespace(target_chapters=5))
        ),
        novel_id="n-1",
        metadata={},
    )

    result = await pipeline._step_find_next_chapter(ctx)

    assert not result.passed
    # 第 6 章超出上限被跳过，扫描走完没找到可写章节
    assert "所有章节已写完" in result.message


@pytest.mark.asyncio
async def test_find_next_chapter_allows_chapter_within_ceiling():
    """上限之内照常定位写作。"""
    from engine.pipeline.base import BaseStoryPipeline

    class _Pipeline(BaseStoryPipeline):
        async def _step_generate(self, ctx):  # pragma: no cover - 未走到
            raise AssertionError("不应进入生成步骤")

    chapter_nodes = [
        _node("ch-1", 1, "chapter", "act-1", outline="大纲", description="", title=""),
        _node("ch-2", 2, "chapter", "act-1", outline="大纲", description="", title=""),
    ]
    ctx = SimpleNamespace(
        story_node_repo=SimpleNamespace(get_by_novel=AsyncMock(return_value=chapter_nodes)),
        chapter_repository=SimpleNamespace(get_by_novel_and_number=Mock(return_value=None)),
        novel_repository=SimpleNamespace(
            get_by_id=Mock(return_value=SimpleNamespace(target_chapters=5))
        ),
        novel_id="n-1",
        metadata={},
    )

    result = await BaseStoryPipeline._step_find_next_chapter(
        _Pipeline(), ctx
    )

    assert result.passed
    assert ctx.chapter_number == 1


# ======================================================================
# 5. 收束幕：最后一幕必须被告知"这是结局"
# ======================================================================


def test_act_prompt_marks_final_act_as_convergence():
    """最后一幕的提示词必须带收束指令，否则故事永远在中段推进。"""
    captured = {}
    svc = ContinuousPlanningService(
        story_node_repo=Mock(),
        chapter_element_repo=Mock(),
        llm_service=Mock(),
        novel_repository=SimpleNamespace(
            get_by_id=Mock(return_value=SimpleNamespace(target_chapters=5))
        ),
    )
    svc._render_contract_prompt = lambda _c, vars_: (
        captured.update(vars_) or SimpleNamespace(user="", system="")
    )

    # 目标 5 章 → 1 幕，所以第 1 幕就是最后一幕
    act = SimpleNamespace(
        id="act-1", novel_id="n-1", number=1, title="终局", description="收尾", content=""
    )
    svc._build_act_planning_prompt(act, {}, None, 5)

    assert "【全书收束】" in captured["context"]


def test_act_prompt_marks_middle_act_as_progressing():
    """非最后一幕不应被误标为收束幕。"""
    captured = {}
    svc = ContinuousPlanningService(
        story_node_repo=Mock(),
        chapter_element_repo=Mock(),
        llm_service=Mock(),
        novel_repository=SimpleNamespace(
            get_by_id=Mock(return_value=SimpleNamespace(target_chapters=100))
        ),
    )
    svc._render_contract_prompt = lambda _c, vars_: (
        captured.update(vars_) or SimpleNamespace(user="", system="")
    )

    act = SimpleNamespace(
        id="act-1", novel_id="n-1", number=1, title="开端", description="", content=""
    )
    svc._build_act_planning_prompt(act, {}, None, 6)

    assert "【全书进度】" in captured["context"]
    assert "【全书收束】" not in captured["context"]
