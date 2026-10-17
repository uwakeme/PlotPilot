"""两条死通道的行为守卫（C-04 / C-05）。

C-04 —— `metadata.is_final_act` 曾经只写不读。本文件锁定的是**重算口径**本身：
    收束幕判定必须来自「幕号 vs 已落库幕数」，而不是节点上的历史标记。
    节点 metadata 即使带着与事实相反的标记，也不能改变判定结果。

C-05 —— `_get_previous_acts_summary` 曾恒返回 None，导致幕级规划提示词里
    永远没有「前情提要」这一段。本文件锁定的是它的**产出内容**：
    有前序幕就必须汇集出真实事实；没有前序幕返回 None 是正确行为。

刻意不使用 `inspect.getsource` 之类的源码文本断言：那种写法只能证明
「源码里没有这几个字符」，证明不了行为。这里全部是行为断言。
"""
from types import SimpleNamespace
from unittest.mock import Mock

from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
)
from domain.structure.story_node import NodeType, StoryNode


# ======================================================================
#  替身：用真实 StoryNode VO 而不是手搓的 SimpleNamespace，
#  避免「替身字段名和真实 VO 对不上」这类假红。
# ======================================================================


class _TreeRepo:
    """只提供本文件用到的方法；get_tree_sync / get_tree 都返回同一棵树。"""

    def __init__(self, nodes):
        self._nodes = list(nodes)

    def get_tree_sync(self, novel_id):
        return SimpleNamespace(novel_id=novel_id, nodes=self._nodes)

    def get_tree(self, novel_id):
        return SimpleNamespace(novel_id=novel_id, nodes=self._nodes)


def _act(number, *, novel_id="n-1", title=None, content=None, key_events=(),
         narrative_arc=None, conflicts=(), metadata=None):
    return StoryNode(
        id=f"act-{number}",
        novel_id=novel_id,
        parent_id="vol-1",
        node_type=NodeType.ACT,
        number=number,
        title=title if title is not None else f"第{number}幕标题",
        order_index=number,
        content=content,
        key_events=list(key_events),
        narrative_arc=narrative_arc,
        conflicts=list(conflicts),
        metadata=dict(metadata or {}),
    )


def _chapter(number, *, novel_id="n-1", act_number=1, title=None, word_count=0):
    return StoryNode(
        id=f"ch-{act_number}-{number}",
        novel_id=novel_id,
        parent_id=f"act-{act_number}",
        node_type=NodeType.CHAPTER,
        number=number,
        title=title if title is not None else f"第{number}章标题",
        order_index=number,
        word_count=word_count,
    )


def _svc(nodes):
    return ContinuousPlanningService(
        story_node_repo=_TreeRepo(nodes),
        chapter_element_repo=Mock(),
        llm_service=Mock(),
    )


# ======================================================================
#  C-05：有前序幕就必须汇集出真实事实
# ======================================================================


async def test_previous_acts_digest_reports_real_prior_act_facts():
    """前情提要必须带上真实存在的前序幕信息，而不是空字符串。"""
    nodes = [
        _act(1, title="山门试炼", content="少年第一次离开山村",
             key_events=["被退婚", "拜入青玄宗"]),
        _act(2, title="血战同门", content="外门大比露头角",
             key_events=["越阶挑战"],
             narrative_arc="压抑到爆发",
             conflicts=["与外门首席的旧怨"],
             # VolumeSummaryService 写入的 LLM 幕摘要，属于「已经存在的事实」
             metadata={"summary": "主角在外门大比中击败首席，被内门长老看中。"}),
        _chapter(1, act_number=1, title="离村", word_count=2100),
        _chapter(2, act_number=1, title="入宗", word_count=2300),
        _chapter(3, act_number=2, title="大比", word_count=2500),
        _act(3, title="内门试炼"),
    ]
    svc = _svc(nodes)

    text = await svc._get_previous_acts_summary(_act(3))

    assert text is not None, "有前序幕却返回 None —— 这就是 C-05 本身"
    # 两幕的真实标题都要出现
    assert "山门试炼" in text
    assert "血战同门" in text
    # 结构字段：叙事目标 / 情节点 / 情绪弧 / 核心冲突
    assert "少年第一次离开山村" in text
    assert "被退婚" in text
    assert "压抑到爆发" in text
    assert "与外门首席的旧怨" in text
    # LLM 写的幕摘要是最高价值的事实，必须被带进来
    assert "主角在外门大比中击败首席" in text
    # 该幕下的章节标题
    assert "大比" in text


async def test_previous_acts_digest_is_none_for_first_act():
    """第 1 幕没有前情，返回 None 是正确行为 —— 锁住它，防止「修复」变成永远返回点什么。"""
    svc = _svc([_act(1, title="山门试炼"), _chapter(1, word_count=2000)])

    assert await svc._get_previous_acts_summary(_act(1)) is None


async def test_previous_acts_digest_omits_absent_fields_without_placeholders():
    """缺字段就不写那一行，绝不写「（未提取）」「暂无」这类占位词。"""
    svc = _svc([_act(1, title="只有标题的那一幕"), _act(2, title="当前幕")])

    text = await svc._get_previous_acts_summary(_act(2))

    assert text is not None
    # 幕本身要在（标题确实存在）
    assert "只有标题的那一幕" in text
    # 缺失的字段整行都不该出现
    assert "叙事目标" not in text
    assert "关键事件" not in text
    assert "核心冲突" not in text
    assert "情绪弧" not in text
    assert "章节" not in text
    for token in ("（未提取）", "未提取", "暂无", "（暂无）", "（无）", "None"):
        assert token not in text, f"出现了占位词: {token}"


async def test_previous_acts_digest_stays_bounded_for_long_books():
    """几十上百幕时整段必须有上限，且最近一幕永远保留 —— 守住提示词预算。"""
    nodes = []
    for number in range(1, 41):
        nodes.append(_act(number, content="很长的一段叙事目标" * 40))
    nodes.append(_act(41, title="当前幕"))
    svc = _svc(nodes)

    text = await svc._get_previous_acts_summary(_act(41))

    assert text is not None
    assert len(text) <= ContinuousPlanningService._PREVIOUS_ACTS_MAX_CHARS
    # 最近的幕是规划当下最该看到的，不能被截断掉
    assert "第40幕标题" in text


# ======================================================================
#  C-04：收束幕判定必须重算，不得读 metadata
# ======================================================================


def test_act_budget_context_ignores_stale_metadata_final_flag():
    """节点 metadata 里的 is_final_act 与事实相反时，判定仍以「幕号 vs 幕数」为准。

    这是对「顺手优化成读 metadata」的直接封锁：动态补出来的幕根本没有这个
    字段，两条来源必须走同一套重算口径。
    """
    last_act = _act(3, title="终局", metadata={"is_final_act": False})
    svc = _svc([_act(1), _act(2), last_act])

    budget = svc._act_budget_context("n-1", 3)
    assert budget["is_final_act"] is True
    assert budget["total_acts"] == 3
    assert "【全书收束】" in budget["brief"]

    # 中间幕即便带着 is_final_act=True 也不能被当成终局
    middle = _act(2, title="中段", metadata={"is_final_act": True})
    svc_mid = _svc([_act(1), middle, _act(3)])
    mid_budget = svc_mid._act_budget_context("n-1", 2)
    assert mid_budget["is_final_act"] is False
    assert "【全书进度】" in mid_budget["brief"]


def test_macro_plan_stops_writing_unconsumed_metadata_fields():
    """宏观规划不再写入没有任何读取方的 metadata 字段。"""
    svc = _svc([])
    nodes = svc._flatten_structure_to_nodes(
        "n-1",
        [
            {
                "title": "第一部",
                "volumes": [
                    {
                        "title": "第一卷",
                        "acts": [
                            {"title": "第一幕", "estimated_chapters": 6},
                            {"title": "第二幕", "estimated_chapters": 4},
                        ],
                    }
                ],
            }
        ],
    )
    acts = [n for n in nodes if n["node_type"] == "act"]

    assert [a["metadata"].get("planned_act_index") for a in acts] == [None, None]
    assert [a["metadata"].get("planned_total_acts") for a in acts] == [None, None]
    # 收束幕判定走 _act_budget_context 重算，这里不再写一个没人读的标记。
    assert [a["metadata"].get("is_final_act") for a in acts] == [None, None]
