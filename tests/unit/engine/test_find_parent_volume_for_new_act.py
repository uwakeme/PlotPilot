"""_find_parent_volume_for_new_act「只向前换卷」选卷策略测试。

背景回归：历史上选卷结果与实际挂载脱钩（create_next_act_auto 硬编码
parent_id=上一幕的父卷），导致全部新幕堆积在同一卷、其余卷永久空壳。
策略要求：锚定最新幕所在卷，填满后只向后换卷，绝不回填锚点之前的空卷
（partial 宏观规划会留下大量空壳卷，回填会让后期章节倒挂到早期剧情卷下）。
"""
from types import SimpleNamespace

from engine.runtime.daemon_host import DaemonHostMixin

CAP = 5


def _make_host() -> DaemonHostMixin:
    # 选卷是纯内存计算，不依赖 self 状态；绕过 __init__ 直接取实例
    return object.__new__(DaemonHostMixin)


def _vol(num: int) -> SimpleNamespace:
    return SimpleNamespace(id=f"vol-{num}", number=num, title=f"第{num}卷")


def _act(num: int, parent_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"act-{num}",
        number=num,
        parent_id=parent_id,
        node_type=SimpleNamespace(value="act"),
    )


def _acts_in_volume(volume_id: str, start: int, count: int):
    return [_act(start + i, volume_id) for i in range(count)]


def _select(host, volumes, acts):
    return host._find_parent_volume_for_new_act(
        volume_nodes=volumes,
        act_nodes=acts,
        rec_acts_per_volume=CAP,
        novel_id="n-1",
    )


def test_anchor_volume_below_cap_stays():
    vols = [_vol(1), _vol(2)]
    acts = _acts_in_volume("vol-1", 1, 3)

    assert _select(_make_host(), vols, acts) is vols[0]


def test_anchor_full_moves_forward_to_next_volume():
    vols = [_vol(1), _vol(2)]
    acts = _acts_in_volume("vol-1", 1, CAP)

    assert _select(_make_host(), vols, acts) is vols[1]


def test_forward_scan_skips_full_volumes():
    vols = [_vol(1), _vol(2), _vol(3), _vol(4)]
    acts = (
        _acts_in_volume("vol-1", 1, CAP)
        + _acts_in_volume("vol-2", 10, CAP)
        + _acts_in_volume("vol-3", 20, 2)
    )

    assert _select(_make_host(), vols, acts) is vols[2]


def test_never_backfills_empty_volumes_before_anchor():
    """核心回归：锚点之前的空壳卷绝不回填（本缺陷在真实书目堆积了 76 幕）。"""
    vols = [_vol(1), _vol(2), _vol(3), _vol(4)]
    acts = _acts_in_volume("vol-3", 1, CAP)  # 最新幕在卷3且已满；卷1/卷2为空壳

    assert _select(_make_host(), vols, acts) is vols[3]


def test_all_full_overruns_onto_last_volume():
    vols = [_vol(1), _vol(2)]
    acts = _acts_in_volume("vol-1", 1, CAP) + _acts_in_volume("vol-2", 10, CAP)

    assert _select(_make_host(), vols, acts) is vols[-1]


def test_anchor_last_volume_full_overruns_in_place():
    """锚点即最后一卷且已满：无处可换，超发挂回锚点（也不回填前面的空卷）。"""
    vols = [_vol(1), _vol(2)]
    acts = _acts_in_volume("vol-2", 10, CAP)

    assert _select(_make_host(), vols, acts) is vols[1]


def test_dangling_anchor_falls_back_to_first_unfilled():
    vols = [_vol(1), _vol(2)]
    acts = [_act(9, "vol-ghost")]  # 最新幕的父卷不在卷列表中

    assert _select(_make_host(), vols, acts) is vols[0]


def test_dangling_anchor_falls_back_then_scans_forward():
    vols = [_vol(1), _vol(2)]
    acts = [_act(9, "vol-ghost")] + _acts_in_volume("vol-1", 1, CAP)

    assert _select(_make_host(), vols, acts) is vols[1]


def test_no_acts_anchors_first_volume():
    vols = [_vol(1), _vol(2)]

    assert _select(_make_host(), vols, []) is vols[0]


def test_no_volumes_returns_none():
    assert _select(_make_host(), [], []) is None


def test_volume_order_is_normalized_by_number():
    """卷节点乱序传入时，向前扫描仍按 number 顺序进行。"""
    vol_two = _vol(2)
    vol_one = _vol(1)
    acts = _acts_in_volume("vol-1", 1, CAP)  # 锚点卷1已满

    result = _make_host()._find_parent_volume_for_new_act(
        volume_nodes=[vol_two, vol_one],
        act_nodes=acts,
        rec_acts_per_volume=CAP,
        novel_id="n-1",
    )

    assert result is vol_two
