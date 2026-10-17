"""部 / 卷章数必须落到节点上，且总章数按「部的最大值」估（B-04）。

`estimate_total_chapters` 估出来的数字直接喂给 `StoryPhase` 的阶段阈值
（opening=0.25 / development=0.75 / convergence=0.90 / finale=1.01）。
估错了，阶段划分跟着全错，而这个函数只是「返回一个看起来正常的整数」——
没有异常、没有日志，下游无从分辨。

两个缺陷：

1. 取**第一个**有 chapter_end 的部就返回。生产库 `novel-1790226677602`
   第 1 部 chapter_end=56、第 2 部=524、第 3 部为空，返回 56，低估 89%。
   写到第 57 章时进度 57/56=1.02 已越过 finale 阈值 1.01，
   故事在第 57 章就被判定为进入结局阶段。
2. `_flatten_structure_to_nodes` 只给幕节点写 `suggested_chapter_count`，
   部 / 卷节点丢掉同一个语义（LLM 的 `estimated_chapters`）。
   提示词 schema 明确要求部、卷、幕都返回 `estimated_chapters`
   （`planning-quick-macro/user.md`），所以丢的是真实数据，不是没人给的字段：
   生产库实测 part 3 个节点 suggested>0 的有 0 个、volume 9 个同样 0 个，
   于是「按部章数求和」那条兜底分支恒为 0，是条死分支。

替身一律用真实 `StoryNode` VO（Task 8 / Task 9 的先例），避免
「替身字段名和真实 VO 对不上」这类假红；断言全部是行为断言，
不使用 `inspect.getsource` 之类的源码文本断言。
"""
from unittest.mock import Mock

from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
)
from application.engine.services.context_lifecycle import estimate_total_chapters
from domain.structure.story_node import NodeType, StoryNode


# ======================================================================
#  替身
# ======================================================================


class _NodeRepo:
    """只提供 `estimate_total_chapters` 用到的那一个方法。"""

    def __init__(self, nodes):
        self._nodes = list(nodes)

    def get_by_novel_sync(self, _novel_id):
        return self._nodes


def _part(number, *, novel_id="n-1", chapter_end=None, suggested=None):
    return StoryNode(
        id=f"part-{novel_id}-{number}",
        novel_id=novel_id,
        node_type=NodeType.PART,
        number=number,
        title=f"第{number}部",
        order_index=number,
        chapter_end=chapter_end,
        suggested_chapter_count=suggested,
    )


def _volume(number, *, novel_id="n-1", chapter_end=None, suggested=None):
    return StoryNode(
        id=f"volume-{novel_id}-{number}",
        novel_id=novel_id,
        parent_id=f"part-{novel_id}-1",
        node_type=NodeType.VOLUME,
        number=number,
        title=f"第{number}卷",
        order_index=number,
        chapter_end=chapter_end,
        suggested_chapter_count=suggested,
    )


def _act(number, *, novel_id="n-1", chapter_end=None, suggested=None):
    return StoryNode(
        id=f"act-{novel_id}-{number}",
        novel_id=novel_id,
        parent_id=f"volume-{novel_id}-1",
        node_type=NodeType.ACT,
        number=number,
        title=f"第{number}幕",
        order_index=number,
        chapter_end=chapter_end,
        suggested_chapter_count=suggested,
    )


def _chapter_node(number, *, novel_id="n-1"):
    return StoryNode(
        id=f"chapter-{novel_id}-{number}",
        novel_id=novel_id,
        parent_id=f"act-{novel_id}-1",
        node_type=NodeType.CHAPTER,
        number=number,
        title=f"第{number}章",
        order_index=number,
    )


def _svc():
    return ContinuousPlanningService(
        story_node_repo=Mock(),
        chapter_element_repo=Mock(),
        llm_service=Mock(),
    )


def _flat_of_type(flat_nodes, node_type):
    return [n for n in flat_nodes if n["node_type"] == node_type]


def _persisted(flat_nodes):
    """模拟「扁平化结果落库、再读回成 VO」。

    `get_by_novel_sync` 的返回就是 `StoryNode` 列表，这里直接用 VO 自己的
    反序列化工厂构造，避免手搓字段名。
    """
    return [StoryNode.from_dict(n) for n in flat_nodes]


def _budget_of(flat_node, label):
    """取扁平化节点上的 suggested_chapter_count，缺失时给出业务原因。"""
    assert "suggested_chapter_count" in flat_node, (
        f"{label} 节点没有落 suggested_chapter_count —— "
        f"LLM 已经规划好的章数在这一层被丢掉了：{flat_node}"
    )
    return flat_node["suggested_chapter_count"]


# ======================================================================
#  缺陷一：多部书按最大值估，不按第一个部
# ======================================================================


def test_multi_part_book_is_estimated_from_the_largest_part_end():
    """3 个部（56 / 524 / 未规划）时必须按 524 估，而不是取第一个部的 56。

    数字取自生产库 novel-1790226677602。取 56 的话，写到第 57 章进度就
    越过 finale 阈值，故事在第二部开头就被判定为已在写结局。
    """
    total = estimate_total_chapters(
        _NodeRepo([
            _part(1, novel_id="novel-1790226677602", chapter_end=56),
            _part(2, novel_id="novel-1790226677602", chapter_end=524),
            _part(3, novel_id="novel-1790226677602"),  # 还没规划
        ]),
        "novel-1790226677602",
    )

    assert total == 524, (
        f"总章数取了第一个部的 56 而不是最大部的 524，低估 468 章（89%）：{total}"
    )


def test_part_end_max_ignores_unset_and_non_positive_values():
    """未规划的部（None）与非法值（0 / 负数）不参与最大值计算。"""
    total = estimate_total_chapters(
        _NodeRepo([
            _part(1, chapter_end=None),
            _part(2, chapter_end=0),
            _part(3, chapter_end=-5),
            _part(4, chapter_end=300),
        ]),
        "n-1",
    )

    assert total == 300, f"无效 chapter_end 混进了最大值：{total}"


# ======================================================================
#  缺陷二：部 / 卷节点的章数必须落到节点上
# ======================================================================


def test_part_and_volume_nodes_carry_chapter_budget_from_both_source_names():
    """部 / 卷节点都要带 suggested_chapter_count，两个源名都认。

    `estimated_chapters` 是提示词 schema 对部、卷、幕统一要求的字段名，
    `suggested_chapter_count` 是兜底骨架的同义名 —— 与幕节点同一套归一口径。
    """
    flat = _svc()._flatten_structure_to_nodes(
        "n-1",
        [
            {
                "title": "第一部",
                "estimated_chapters": 120,
                "volumes": [
                    {"title": "第一卷", "estimated_chapters": 60},
                    {"title": "第二卷", "suggested_chapter_count": 60},
                ],
            },
            {
                "title": "第二部",
                "estimated_chapters": 80,
                "volumes": [{"title": "第三卷", "estimated_chapters": 80}],
            },
        ],
    )

    parts = _flat_of_type(flat, "part")
    volumes = _flat_of_type(flat, "volume")

    assert _budget_of(parts[0], "部") == 120, "部的 estimated_chapters 没落上"
    assert _budget_of(parts[1], "部") == 80, "第二个部的章数没落上"

    assert [_budget_of(v, "卷") for v in volumes] == [60, 60, 80], (
        "卷的章数没落上，或 suggested_chapter_count 这个源名没被认"
    )


def test_string_chapter_budgets_are_normalized_to_int():
    """LLM 常把章数返回成字符串，必须规整成正整数（复用 _as_positive_int）。"""
    flat = _svc()._flatten_structure_to_nodes(
        "n-1",
        [
            {
                "title": "第一部",
                "estimated_chapters": "60",
                "volumes": [{"title": "第一卷", "estimated_chapters": "60章"}],
            }
        ],
    )

    part = _flat_of_type(flat, "part")[0]
    volume = _flat_of_type(flat, "volume")[0]

    assert _budget_of(part, "部") == 60, f'"60" 没被规整成 60：{part}'
    assert _budget_of(volume, "卷") == 60, f'"60章" 没被规整成 60：{volume}'


def test_missing_or_invalid_chapter_budget_stays_none():
    """LLM 没给或给了非法值时保持 None，**不伪造 0**。

    这条是红线：0 会被「有值就累加」的分支当成真实章数，
    伪造出来的默认值正是估算失真、却无人知晓的来源之一。
    """
    flat = _svc()._flatten_structure_to_nodes(
        "n-1",
        [
            {
                "title": "第一部",
                "estimated_chapters": 0,
                "volumes": [
                    {"title": "第一卷", "estimated_chapters": "abc"},
                    {"title": "第二卷"},  # 整个字段缺失
                ],
            }
        ],
    )

    part = _flat_of_type(flat, "part")[0]
    volumes = _flat_of_type(flat, "volume")

    assert _budget_of(part, "部") is None, "0 是非法值，不该被规整成任何数字"
    assert _budget_of(volumes[0], "卷") is None, "'abc' 是非法值，不该被规整成任何数字"
    assert _budget_of(volumes[1], "卷") is None, "字段缺失就该是 None，不该是 0"


def test_summed_part_budget_feeds_the_total_estimate():
    """章数落到部节点上之后，「按部求和」这条兜底分支才真正有数据可用。

    这条同时是缺陷二的回归守卫：部节点不带章数时，求和恒为 0，
    函数会静默落到 100 章这个兜底值 —— 一本规划了 200 章的书被当成 100 章。
    """
    flat = _svc()._flatten_structure_to_nodes(
        "n-1",
        [
            {
                "title": "第一部",
                "estimated_chapters": 120,
                "volumes": [
                    {"title": "第一卷", "estimated_chapters": 60, "acts": [{"title": "第一幕"}]},
                    {"title": "第二卷", "estimated_chapters": 60, "acts": [{"title": "第二幕"}]},
                ],
            },
            {
                "title": "第二部",
                "estimated_chapters": 80,
                "volumes": [
                    {"title": "第三卷", "estimated_chapters": 80, "acts": [{"title": "第三幕"}]}
                ],
            },
        ],
    )
    nodes = _persisted(flat)
    # 扁平化阶段不写章节范围，全部 chapter_end 为空 → 只能走求和分支
    assert all(n.chapter_end is None for n in nodes)

    total = estimate_total_chapters(_NodeRepo(nodes), "n-1")

    assert total == 200, (
        f"部章数求和分支没生效，总章数落到了兜底值：{total}（期望 120+80=200）"
    )


# ======================================================================
#  反向守卫：不该被改坏的行为
# ======================================================================


def test_parts_without_chapter_end_still_fall_through_to_chapter_nodes():
    """全部部的 chapter_end 为空时，必须继续走后续兜底分支，不能返回一个错值。"""
    total = estimate_total_chapters(
        _NodeRepo([_part(1), _part(2), _chapter_node(50)]),
        "n-1",
    )

    assert total == 60, f"没有部 chapter_end 时没有回落到已写章节：{total}"


def test_act_chapter_end_does_not_leak_into_the_total():
    """幕的 chapter_end 不参与取值。

    生产库那本书的幕级最大 chapter_end 是 529，比部的最大值 524 还大 5；
    部才是「全书目标章数」这个语义的持有者，这条守住取值范围不被放大。
    """
    total = estimate_total_chapters(
        _NodeRepo([
            _part(1, chapter_end=56),
            _part(2, chapter_end=524),
            _act(1, chapter_end=529),
        ]),
        "novel-1790226677602",
    )

    assert total == 524, f"幕的 chapter_end 混进了总章数：{total}"
