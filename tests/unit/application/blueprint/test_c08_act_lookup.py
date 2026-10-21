"""C-08：章节 → 幕的查找语义（与 docstring 一致），以及续规划那条死分支。

登记表的 C-08 写的是「`_find_act_for_chapter` 语义与 docstring 反了
（`chapter_number` 形参从未使用）；连带 `has_next_act` 恒 False」。
核实后病因**三处**，第三处比登记表写的更严重：

1. `_find_act_for_chapter(novel_id, chapter_number)` **完全忽略** `chapter_number`，
   直接返回幕号最大的那一幕（= 全书最后一幕）。
2. `_get_next_act(current_act)` 找的是 `number == current_act.number + 1` ——
   配合上一条（当前幕恒为最后一幕）⇒ `has_next_act` **恒为 False**，
   「这一幕完成了、可以开始下一幕」那条分支是**死代码**。
3. **（登记表没写）** `_count_written_chapters_in_act` / `_count_planned_chapters_in_act`
   调的是 `get_children(act_id, None)` **且没有 await** ——
   而 `StoryNodeRepository.get_children(parent_id)` 只收**一个**参数、是 async。
   ⇒ 生产里这一步必抛 TypeError ⇒ `continue_planning` 整个 500。
   ⚠ 用 Mock 写的守卫**抓不到**这个 bug（Mock 收下任意参数、也不提醒 await）
   ⇒ 本文件用**真实签名**的替身仓储。

全部是行为断言：不使用 `inspect.getsource` 之类的源码文本断言。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
)
from domain.structure.story_node import NodeType, StoryNode


# ======================================================================
#  替身：按**真实仓储签名**实现（get_children(parent_id) 是 async、只收一个参数）
# ======================================================================


class _RealShapeRepo:
    """刻意不用 Mock：Mock 会收下错误签名、也不会提醒 await ⇒ 假绿。

    签名照抄 `StoryNodeRepository`：
    `get_tree` 是 async（不 await 就是 coroutine ⇒ `tree.nodes` 抛 AttributeError）、
    `get_tree_sync` 是同步的、`get_children(parent_id)` 只收**一个**参数且是 async。
    """

    def __init__(self, nodes):
        self._nodes = list(nodes)

    async def get_tree(self, novel_id):
        return SimpleNamespace(novel_id=novel_id, nodes=self._nodes)

    def get_tree_sync(self, novel_id):
        return SimpleNamespace(novel_id=novel_id, nodes=self._nodes)

    async def get_children(self, parent_id):
        return [n for n in self._nodes if getattr(n, "parent_id", None) == parent_id]


def _act(number: int, *, chapter_start=None, chapter_end=None):
    return StoryNode(
        id=f"act-{number}",
        novel_id="n-1",
        node_type=NodeType.ACT,
        number=number,
        title=f"第{number}幕",
        order_index=number,
        chapter_start=chapter_start,
        chapter_end=chapter_end,
    )


def _chapter(number: int, act_id: str, *, written: bool = True):
    return StoryNode(
        id=f"ch-{number}",
        novel_id="n-1",
        node_type=NodeType.CHAPTER,
        number=number,
        title=f"第{number}章",
        order_index=number,
        parent_id=act_id,
        word_count=100 if written else 0,
    )


def _structure():
    """三幕：act-1(1-3) / act-2(4-6) / act-3(7-9)。"""
    acts = [
        _act(1, chapter_start=1, chapter_end=3),
        _act(2, chapter_start=4, chapter_end=6),
        _act(3, chapter_start=7, chapter_end=9),
    ]
    chapters = []
    for act_no, (start, end) in enumerate(((1, 3), (4, 6), (7, 9)), start=1):
        for number in range(start, end + 1):
            chapters.append(_chapter(number, f"act-{act_no}"))
    return acts + chapters


def _svc(nodes=None):
    return ContinuousPlanningService(
        story_node_repo=_RealShapeRepo(_structure() if nodes is None else nodes),
        chapter_element_repo=Mock(),
        llm_service=Mock(),
    )


# ======================================================================
#  1. _find_act_for_chapter：真的去找「章节所属的幕」
# ======================================================================


@pytest.mark.asyncio
async def test_find_act_for_chapter_returns_the_owning_act():
    """章节 5 属于第 2 幕 —— 不是「幕号最大的那一幕」。"""
    svc = _svc()

    act = await svc._find_act_for_chapter("n-1", 5)

    assert act is not None
    assert act.id == "act-2"
    assert act.number == 2


@pytest.mark.asyncio
async def test_find_act_for_chapter_returns_none_for_unknown_chapter():
    """结构里没有这一章 ⇒ 返回 None（**不拿最后一幕冒充**）。"""
    svc = _svc()

    act = await svc._find_act_for_chapter("n-1", 99)

    assert act is None


@pytest.mark.asyncio
async def test_find_act_for_chapter_falls_back_to_act_chapter_range():
    """章节节点还没落库 ⇒ 用幕的章节区间判定。"""
    acts = [
        _act(1, chapter_start=1, chapter_end=3),
        _act(2, chapter_start=4, chapter_end=6),
    ]
    svc = _svc(acts)

    act = await svc._find_act_for_chapter("n-1", 5)

    assert act is not None
    assert act.id == "act-2"


@pytest.mark.asyncio
async def test_find_act_for_chapter_uses_the_chapter_node_when_no_range_is_backfilled():
    """幕的章节区间**没回填**时，章节节点的父幕仍是答案。

    这一条专门用来给「按章节节点找父幕」这条规则**独立牙齿**：
    区间没回填 ⇒ 区间规则答不出来 ⇒ 只有章节节点这条规则能答。
    （否则「有区间也有章节节点」的场景里，两条规则给出同一个答案，
    守卫就分不清是哪条规则在起作用 —— 变异时会是假绿。）
    """
    acts = [_act(1), _act(2), _act(3)]  # 没有 chapter_start / chapter_end
    chapters = [_chapter(5, "act-2")]
    svc = _svc(acts + chapters)

    act = await svc._find_act_for_chapter("n-1", 5)

    assert act is not None
    assert act.id == "act-2"


# ======================================================================
#  2. _get_next_act：下一幕 = 幕号更大的最小那一幕
# ======================================================================


@pytest.mark.asyncio
async def test_get_next_act_finds_the_following_act():
    svc = _svc()

    nxt = await svc._get_next_act(_act(2))

    assert nxt is not None
    assert nxt.id == "act-3"


@pytest.mark.asyncio
async def test_get_next_act_is_none_for_the_last_act():
    svc = _svc()

    assert await svc._get_next_act(_act(3)) is None


@pytest.mark.asyncio
async def test_get_next_act_tolerates_duplicate_act_numbers():
    """重号幕（fallback 造过）时，「下一幕」= 幕号**大于**当前的最小那一幕。"""
    acts = [_act(1), _act(2), _act(2), _act(3)]
    svc = _svc(acts)

    nxt = await svc._get_next_act(_act(2))

    assert nxt is not None
    assert nxt.number == 3


@pytest.mark.asyncio
async def test_get_next_act_skips_number_gaps():
    """幕号有断层（幕被删过）时，下一幕是**幕号更大的最小那一幕**，不是「number+1」。"""
    acts = [_act(1), _act(2), _act(5)]
    svc = _svc(acts)

    nxt = await svc._get_next_act(_act(2))

    assert nxt is not None
    assert nxt.number == 5


# ======================================================================
#  3. 计数：签名 + await（登记表没写的那条）
# ======================================================================


@pytest.mark.asyncio
async def test_count_chapters_in_act_uses_real_repository_signature():
    """`get_children(parent_id)` 只收一个参数且是 async —— 调用点必须对得上。"""
    svc = _svc()

    assert await svc._count_planned_chapters_in_act("act-2") == 3
    assert await svc._count_written_chapters_in_act("act-2") == 3


@pytest.mark.asyncio
async def test_count_written_chapters_counts_only_written_ones():
    svc = _svc()
    # 把 act-2 的三章都改成未写
    nodes = [
        n
        for n in _structure()
        if getattr(n, "parent_id", None) != "act-2"
    ] + [
        _chapter(number, "act-2", written=False) for number in (4, 5, 6)
    ]
    svc = _svc(nodes)

    assert await svc._count_planned_chapters_in_act("act-2") == 3
    assert await svc._count_written_chapters_in_act("act-2") == 0


# ======================================================================
#  4. continue_planning：has_next_act 不再恒为 False
# ======================================================================


@pytest.mark.asyncio
async def test_continue_planning_reports_next_act_when_current_act_completed():
    """当前幕写完了且**后面还有幕** ⇒ has_next_act=True（这条分支此前是死代码）。"""
    svc = _svc()

    result = await svc.continue_planning("n-1", 5)  # 第 5 章属于 act-2

    assert result["success"] is True
    assert result["act_completed"] is True
    assert result["has_next_act"] is True
    assert result["next_act"]["number"] == 3
    assert "suggest_create_next" not in result


@pytest.mark.asyncio
async def test_continue_planning_counts_the_owning_act_progress():
    """进度必须是**当前章所属那一幕**的进度（不是最后一幕的）。"""
    nodes = [
        n
        for n in _structure()
        if getattr(n, "parent_id", None) != "act-2"
    ] + [
        _chapter(number, "act-2", written=False) for number in (4, 5, 6)
    ]
    svc = _svc(nodes)

    result = await svc.continue_planning("n-1", 5)

    assert result["act_completed"] is False
    assert result["current_act"]["number"] == 2
    assert result["progress"] == "0/3"
    assert result["message"] == "继续第 2 幕"


@pytest.mark.asyncio
async def test_continue_planning_says_not_found_for_unknown_chapter():
    """结构里没有这一章 ⇒ 如实回「未找到」（不拿最后一幕冒充）。"""
    svc = _svc()

    result = await svc.continue_planning("n-1", 99)

    assert result["success"] is False
    assert "未找到" in result["message"]
