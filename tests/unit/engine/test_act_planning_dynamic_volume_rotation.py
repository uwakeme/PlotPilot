"""守护进程动态建幕必须把选出的父卷真正传给 create_next_act_auto。

历史回归：act_planning_delegate 选了父卷却只用来打日志，建幕走
create_next_act_auto(current_act_id=last_act.id) 后硬编码挂在上一幕的
父卷下，导致全部新幕堆积进同一卷、其余卷永久空壳（且日志显示的是
被丢弃的选卷结果，长期失真）。
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from domain.novel.entities.novel import NovelStage
from engine.runtime import act_planning_delegate
from engine.runtime.daemon_host import DaemonHostMixin


def _node(node_id, number, node_type, parent_id, **extra):
    return SimpleNamespace(
        id=node_id,
        number=number,
        node_type=SimpleNamespace(value=node_type),
        parent_id=parent_id,
        **extra,
    )


@pytest.mark.asyncio
async def test_run_act_planning_passes_selected_volume_to_create_next_act(monkeypatch):
    """target_act 缺失触发动态建幕：真实选卷结果必须作为 parent_volume_id 传入建幕。"""
    volumes = [
        _node("vol-1", 1, "volume", "part-1", title="卷一"),
        _node("vol-2", 2, "volume", "part-1", title="卷二"),
    ]
    # 卷1已有 5 幕（= calculate_structure_params(300) 的每卷幕数上限），最新幕在卷1
    acts = [
        _node(f"act-{n}", n, "act", "vol-1", suggested_chapter_count=8)
        for n in range(48, 53)
    ]
    nodes = volumes + acts

    host = Mock()
    host._is_still_running.return_value = True
    host.story_node_repo.get_by_novel = AsyncMock(return_value=nodes)
    host.story_node_repo.get_children_sync.return_value = []
    # 真实选卷策略 + Mock 落库：验证选卷 → 建幕的接线（历史上断掉的一环）
    host._find_parent_volume_for_new_act = Mock(
        side_effect=lambda **kw: DaemonHostMixin._find_parent_volume_for_new_act(host, **kw)
    )
    create_next_act = AsyncMock(
        return_value={"success": True, "next_act": {"parent_id": "vol-2", "number": 53}}
    )
    host.planning_service.create_next_act_auto = create_next_act
    host.planning_service.confirm_act_planning = AsyncMock()

    novel = Mock()
    novel.novel_id.value = "n-1"
    novel.current_act = 52
    novel.target_chapters = 300
    novel.current_stage = NovelStage.ACT_PLANNING

    monkeypatch.setattr(act_planning_delegate, "_read_shared_state", lambda _nid: {})

    await act_planning_delegate.run_act_planning(host, novel)

    create_next_act.assert_awaited_once()
    kwargs = create_next_act.await_args.kwargs
    assert kwargs["novel_id"] == "n-1"
    assert kwargs["current_act_id"] == "act-52"  # 最新幕作为叙事衔接上下文
    assert kwargs["parent_volume_id"] == "vol-2"  # 卷1已满 → 只向前换卷到卷2


@pytest.mark.asyncio
async def test_run_act_planning_keeps_anchor_volume_when_below_cap(monkeypatch):
    """锚点卷未满时留守，不向后跳卷。"""
    volumes = [
        _node("vol-1", 1, "volume", "part-1", title="卷一"),
        _node("vol-2", 2, "volume", "part-1", title="卷二"),
    ]
    acts = [
        _node(f"act-{n}", n, "act", "vol-1", suggested_chapter_count=8)
        for n in range(48, 51)  # 3 幕 < 上限 5
    ]
    nodes = volumes + acts

    host = Mock()
    host._is_still_running.return_value = True
    host.story_node_repo.get_by_novel = AsyncMock(return_value=nodes)
    host.story_node_repo.get_children_sync.return_value = []
    host._find_parent_volume_for_new_act = Mock(
        side_effect=lambda **kw: DaemonHostMixin._find_parent_volume_for_new_act(host, **kw)
    )
    create_next_act = AsyncMock(
        return_value={"success": True, "next_act": {"parent_id": "vol-1", "number": 51}}
    )
    host.planning_service.create_next_act_auto = create_next_act
    host.planning_service.confirm_act_planning = AsyncMock()

    novel = Mock()
    novel.novel_id.value = "n-1"
    novel.current_act = 50
    novel.target_chapters = 300
    novel.current_stage = NovelStage.ACT_PLANNING

    monkeypatch.setattr(act_planning_delegate, "_read_shared_state", lambda _nid: {})

    await act_planning_delegate.run_act_planning(host, novel)

    create_next_act.assert_awaited_once()
    assert create_next_act.await_args.kwargs["parent_volume_id"] == "vol-1"
