# -*- coding: utf-8 -*-
"""第 76 批：合并引擎的 ``node_type`` 契约（**比较时归一、落库不许变大写**）。

⚠ 第 59 批的病根：引擎写死 ``'CHAPTER'``（大写），而生产侧传的是
  ``node.node_type.value``（**小写**）⇒ carrier 一个都标不出来
  ⇒ 冲突检测恒不触发（RED 永不亮）⇒ **章节节点反而被当成空壳删掉**。
⇒ 修法：比较时归一（``_normalize_node_type``）。

⚠ 但那批只钉住了**比较侧** ⇒ **落库侧没有守卫**：
  ``node_type`` 会被原样写进数据库（数据库口径是**小写**）
  ⇒ 哪天有人把归一结果写回节点字典，落库就变成大写
  ⇒ 生产侧读回来又对不上 ⇒ **同一个 bug 换个地方复发**。

本文件钉住
----------
1. **比较侧**：小写 ``chapter`` 必须被认成章节节点
   （章节节点不许进 ``deletes``；承载它的卷必须报**致命**冲突）；
2. **落库侧**：合并过程**不许**改写节点字典里的 ``node_type``
   （原样保持小写；冲突记录里带出来的也是原始小写值）。
"""
from __future__ import annotations

from application.audit.services.macro_merge_engine import (
    CONFLICT_SEVERITY_FATAL,
    MacroMergeEngine,
)


def _old_nodes() -> list:
    """旧结构：一个卷 + 一个**已有正文**的章节（口径全是**小写**）。"""
    return [
        {
            "id": "vol-1",
            "title": "第一卷",
            "node_type": "volume",
            "order_index": 0,
            "parent_id": None,
        },
        {
            "id": "ch-1",
            "title": "第一章",
            "node_type": "chapter",
            "order_index": 1,
            "parent_id": "vol-1",
            "has_prose": True,
        },
    ]


def _new_nodes() -> list:
    """新结构：把卷删了（⇒ 卷承载的正文章节成了冲突）。"""
    return [
        {
            "id": "vol-2",
            "title": "新卷",
            "node_type": "volume",
            "order_index": 0,
            "parent_id": None,
        }
    ]


def test_the_lowercase_chapter_node_is_recognized_as_a_chapter():
    """⚠ 比较侧：``chapter``（小写）必须被认成章节节点。

    ⚠ 认不出来 ⇒ 它会被当成**空壳**删掉（章节正文变孤儿）⇒ 不可逆。
    """
    old = _old_nodes()
    plan = MacroMergeEngine(old, _new_nodes()).execute_diff()

    assert "ch-1" not in plan.deletes, (
        "小写 chapter 节点被当成空壳删了 ⇒ 它的正文章节成了孤儿"
    )


def test_the_carrier_with_written_chapters_raises_a_fatal_conflict():
    """⚠ 章节被认出来 ⇒ 卷才是 carrier ⇒ 删卷必须**阻断**（fatal）。"""
    old = _old_nodes()
    plan = MacroMergeEngine(old, _new_nodes()).execute_diff()

    conflicts = {c["node_id"]: c for c in plan.conflicts}
    assert "vol-1" in conflicts, "承载正文章节的卷没有被报冲突 ⇒ 会被直接删掉"
    assert conflicts["vol-1"]["severity"] == CONFLICT_SEVERITY_FATAL
    assert "vol-1" not in plan.deletes, "有致命冲突的节点还进了 deletes ⇒ 阻断形同虚设"


def test_the_merge_never_rewrites_the_node_type_in_the_nodes():
    """⚠ **落库侧**：合并过程不许把 ``node_type`` 变大写。

    ⚠ 数据库口径是小写（``domain/structure/story_node.py`` 的 NodeType 值）
    ⇒ 落库变大写 ⇒ 生产侧读回来对不上 ⇒ 第 59 批那个 bug 换个地方复发。
    """
    old = _old_nodes()
    new = _new_nodes()
    plan = MacroMergeEngine(old, new).execute_diff()

    assert [n["node_type"] for n in old] == ["volume", "chapter"], (
        "合并过程改写了旧节点字典里的 node_type ⇒ 落库口径会漂"
    )
    assert [n["node_type"] for n in new] == ["volume"]
    # 冲突记录里带出来的也是**原始**小写值（前端要按它显示）
    for conflict in plan.conflicts:
        assert conflict["node_type"] == conflict["node_type"].lower(), conflict
    # 计划里要写库的节点也不许带大写口径
    for node in list(plan.creates) + list(plan.updates):
        if "node_type" in node:
            assert node["node_type"] == node["node_type"].lower(), node
