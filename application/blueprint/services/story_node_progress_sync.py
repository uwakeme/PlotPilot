"""章节写作进度回写：chapters 表 → story_nodes 章节节点。

为什么需要这个模块
------------------
``story_nodes`` 里的 chapter 节点带 ``word_count`` / ``status`` 字段，
设计意图是让结构树镜像章节的真实进度（见
``application/core/services/novel_service.py::add_chapter``，手动建章路径
一直会写这两个字段）。

但引擎写作路径（``engine/pipeline/base.py::_step_save_chapter`` 与
``engine/runtime/daemon_host.py::_save_chapter_ephemeral``）只写 ``chapters``
表，**从不回写 story_nodes**。实测全库 587 个章节节点，
``word_count > 0`` 的数量为 0——这个字段对引擎写出来的书永远是幽灵字段。

而有三处控制流判定依赖它来判断「这一章动笔没有」：

- ``engine/runtime/act_planning_delegate.py`` 闸门 0（``prev_act_unwritten``）
- 同文件预算闸门的 ``pending`` 列表
- ``application/blueprint/services/continuous_planning_service.py``
  的 ``_count_written_chapters_in_act``

字段恒为 0 意味着这三处恒判「全部未写」。后果是上一幕永远有「未动笔章节」，
新幕开不出来；写作找不到下一章后又被 ``_current_act_fully_written`` 推进
``current_act``，两者互相打架形成死循环。

真相源始终是 ``chapters.status``。本模块只做一件事：在章节落库后把进度
镜像回结构树节点，让上述判定恢复视力。

``story_structure_service`` 里早就有一层展示层的覆盖补丁
（``_enrich_chapter_nodes_from_chapters_table``），但那只是让前端好看，
控制流依然在读未同步的字段——病根在源头，不在展示层。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = ["sync_chapter_node_progress"]


async def sync_chapter_node_progress(
    story_node_repo: Any,
    novel_id: str,
    chapter_number: int,
    *,
    word_count: Optional[int] = None,
    status: Optional[str] = None,
) -> bool:
    """把章节的真实写作进度镜像到对应的 story_nodes 章节节点。

    Args:
        story_node_repo: ``StoryNodeRepository`` 实例
        novel_id: 小说 id
        chapter_number: 全书章号（1-based）
        word_count: 真实字数；``None`` 表示不改写
        status: 章节状态字符串（如 ``"completed"``）；``None`` 表示不改写

    Returns:
        是否发生了实际写入。找不到章节节点时返回 ``False``——正文可能先于
        结构落库（此时没有节点可写，不应视为错误）。
    """
    if story_node_repo is None or novel_id is None or chapter_number is None:
        return False

    try:
        nodes = await story_node_repo.get_by_novel(novel_id)
    except Exception:
        logger.debug("[%s] 读取结构节点失败，跳过章节进度同步", novel_id, exc_info=True)
        return False

    node = next(
        (
            n
            for n in nodes
            if getattr(getattr(n, "node_type", None), "value", None) == "chapter"
            and int(getattr(n, "number", 0) or 0) == int(chapter_number)
        ),
        None,
    )
    if node is None:
        logger.debug(
            "[%s] 第%s章没有对应的结构节点，跳过进度同步", novel_id, chapter_number
        )
        return False

    changed = False
    if word_count is not None and int(node.word_count or 0) != int(word_count):
        node.word_count = int(word_count)
        changed = True
    if status is not None and str(node.status or "") != str(status):
        node.status = str(status)
        changed = True

    if not changed:
        return False

    try:
        await story_node_repo.update(node)
    except Exception:
        # 结构树是展示与结构编排用的，章节正文才是事实源。同步失败不应阻断写作。
        logger.warning(
            "[%s] 第%s章进度回写结构节点失败（不阻断写作）",
            novel_id,
            chapter_number,
            exc_info=True,
        )
        return False
    return True
