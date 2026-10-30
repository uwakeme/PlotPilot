"""
宏观规划合并引擎（Macro Merge Engine）

外科手术级的结构覆写机制：
- 保留有肉的骨头（已写正文的节点）
- 切除多余的空骨头（僵尸节点）
- 嫁接新的骨头（新增节点）
"""

from typing import List, Dict
from dataclasses import dataclass


# H-05：冲突风险分级。
#
#   fatal   —— 节点下**已有正文**（不可逆）⇒ 重构必须阻断
#   warning —— 节点下**只有尚未动笔的章节** ⇒ 不阻断，但必须显式提示，
#               且旧节点**保留**（不删、不替作者搬家）
#
# 分级只依据「这一章到底动笔没有」；依据不明时一律按 fatal 处理：
# 宁可阻断，不可丢稿。
CONFLICT_SEVERITY_FATAL = "fatal"
CONFLICT_SEVERITY_WARNING = "warning"


def _normalize_node_type(value) -> str:
    """node_type 归一为大写，**仅用于比较**。

    ⚠ 这里绝不能改写节点字典本身 ——
    ``apply_merge_plan`` 会把 ``node_type`` 原样写进数据库，
    而数据库口径是小写（``domain/structure/story_node.py`` 的 NodeType 值）。

    为什么要归一：引擎原先写死 ``'CHAPTER'``（大写），
    而生产侧传进来的是 ``node.node_type.value``（小写）
    ⇒ carrier 一个都标不出来 ⇒ 冲突检测恒不触发（RED 永不亮）
    ⇒ 章节节点反而被当成空壳删掉。
    """
    return str(value or "").strip().upper()


@dataclass
class MergePlan:
    """合并计划"""
    creates: List[Dict]  # 需要创建的节点
    updates: List[Dict]  # 需要更新的节点
    deletes: List[str]   # 需要删除的节点 ID
    conflicts: List[Dict]  # 冲突列表（每项带 severity）
    summary: Dict  # 合并摘要（用于前端渐进式提示）

    @property
    def fatal_conflicts(self) -> List[Dict]:
        """致命冲突：只有这些会阻断重构。

        ⚠ 缺 `severity` 的冲突按 **fatal** 处理 ——
        「分级依据不明」不等于「可以放行」。
        """
        return [
            c for c in self.conflicts
            if c.get("severity", CONFLICT_SEVERITY_FATAL) == CONFLICT_SEVERITY_FATAL
        ]

    @property
    def warnings(self) -> List[Dict]:
        """非致命冲突：旧节点已保留，等待作者自己处理。"""
        return [
            c for c in self.conflicts
            if c.get("severity") == CONFLICT_SEVERITY_WARNING
        ]

    @property
    def has_fatal_conflict(self) -> bool:
        """是否有致命冲突（由 conflicts 派生，不另存一份真相）。"""
        return bool(self.fatal_conflicts)


class MergeConflictException(Exception):
    """合并冲突异常"""
    def __init__(self, message: str, conflicts: List[Dict]):
        super().__init__(message)
        self.conflicts = conflicts


class MacroMergeEngine:
    """宏观规划合并引擎

    核心算法：
    1. 自底向上标记承载者（Bottom-Up Contagion）
    2. 三路比对（create/update/delete）
    3. 冲突检测（H-05 分级：fatal 阻断 / warning 提示）
    4. 生成渐进式提示（绿/黄/红）
    """

    def __init__(self, old_nodes: List[Dict], new_nodes: List[Dict]):
        """初始化合并引擎

        Args:
            old_nodes: 旧结构节点列表（从数据库读取，已标准化为 Dict）
            new_nodes: 新结构节点列表（从前端传入，已标准化为 Dict）
        """
        # 将列表转化为哈希表，实现 O(1) 匹配
        self.old_map = {node['id']: node for node in old_nodes}
        self.new_map = {node['id']: node for node in new_nodes}

        # node_type 只在**比较**时归一（见 _normalize_node_type 的注释：
        # 大小写口径不一致会让整个冲突检测静默失效）。
        self._old_types = {
            node_id: _normalize_node_type(node.get('node_type'))
            for node_id, node in self.old_map.items()
        }

        self.to_create: List[Dict] = []
        self.to_update: List[Dict] = []
        self.to_delete: List[str] = []
        self.conflicts: List[Dict] = []

    def _mark_carriers_bottom_up(self) -> Dict[str, Dict[str, int]]:
        """自底向上标记承载者（Bottom-Up Contagion）

        核心逻辑：
        - 找到所有 CHAPTER 节点（正文节点）
        - 回溯其所有祖先（Act → Volume → Part）
        - 将祖先节点标记为 carrier（承载者）

        H-05：carrier 不再只是「是/否」两态，而是带上**证据** ——
        它承载了几章、其中几章已有正文。风险分级（fatal / warning）就靠这个。

        Returns:
            carrier_id -> {"chapters": 承载的章节数, "written": 其中已有正文的章数}
        """
        carriers: Dict[str, Dict[str, int]] = {}

        # 找到所有正文节点（Chapter）—— 按归一口径判定，不靠字面量大小写
        chapter_nodes = [
            n for node_id, n in self.old_map.items()
            if self._old_types.get(node_id) == 'CHAPTER'
        ]

        for chapter in chapter_nodes:
            written = self._chapter_has_prose(chapter)
            # Chapter 本身有内容，回溯其所有祖先
            current_id = chapter.get('parent_id')
            while current_id and current_id in self.old_map:
                info = carriers.setdefault(current_id, {"chapters": 0, "written": 0})
                info["chapters"] += 1
                if written:
                    info["written"] += 1
                current_id = self.old_map[current_id].get('parent_id')

        return carriers

    @staticmethod
    def _chapter_has_prose(chapter: Dict) -> bool:
        """该章节是否已有正文。

        判据由调用方（服务层）以 `has_prose` 布尔位给出 ——
        **缺失即视为有正文**：只有「确实没有正文」被证明过时才允许降级成 warning，
        判据不明时一律按最坏情况处理（宁可阻断，不可丢稿）。
        """
        flag = chapter.get('has_prose')
        if flag is None:
            return True
        return bool(flag)

    def execute_diff(self) -> MergePlan:
        """执行比对逻辑（三路比对）

        Returns:
            合并计划（MergePlan）
        """
        carriers = self._mark_carriers_bottom_up()

        # 1. 遍历旧结构（寻找需要更新、删除或引发冲突的节点）
        for old_id, old_node in self.old_map.items():
            # 跳过 Chapter 节点（我们只 merge 宏观框架）—— 按归一口径判定
            if self._old_types.get(old_id) == 'CHAPTER':
                continue

            if old_id in self.new_map:
                # 场景 C：新旧都有 -> 安全合并（换皮不换骨）
                new_node = self.new_map[old_id]
                updated_node = {
                    'id': old_id,
                    'title': new_node['title'],
                    'description': new_node.get('description', ''),
                    'order_index': new_node['order_index']
                }
                self.to_update.append(updated_node)
            else:
                # 场景 A/B：旧的有，新的没有 -> 判断是否可以删除
                if old_id in carriers:
                    # H-05 风险分级：
                    #   下面有**已写正文**的章节 ⇒ fatal（不可逆，必须阻断）
                    #   下面只有**未动笔**的章节 ⇒ warning（不阻断，但保留待人工处理）
                    # 无论哪一档，这个节点都**不会进 deletes** ——
                    # 删掉它，它的章节就成了孤儿（parent_id 指向不存在的节点）。
                    info = carriers[old_id]
                    written = int(info.get("written", 0))
                    carried = int(info.get("chapters", 0))
                    if written > 0:
                        severity = CONFLICT_SEVERITY_FATAL
                        reason = f"新结构删除了包含已有正文章节的节点 [{old_node['title']}]"
                    else:
                        severity = CONFLICT_SEVERITY_WARNING
                        reason = (
                            f"新结构未覆盖仍承载 {carried} 章（尚无正文）的节点 "
                            f"[{old_node['title']}]，已保留待人工处理"
                        )
                    self.conflicts.append({
                        "node_id": old_id,
                        "title": old_node['title'],
                        "node_type": old_node['node_type'],
                        "reason": reason,
                        "severity": severity,
                        "carried_chapters": carried,
                        "written_chapters": written,
                    })
                else:
                    # 纯空壳，安全删除
                    self.to_delete.append(old_id)

        # 2. 遍历新结构（寻找需要新建的节点）
        for new_id, new_node in self.new_map.items():
            if new_id not in self.old_map:
                self.to_create.append(new_node)

        # 3. 构造合并计划返回
        return MergePlan(
            creates=self.to_create,
            updates=self.to_update,
            deletes=self.to_delete,
            conflicts=self.conflicts,
            summary=self._generate_summary()
        )

    def _generate_summary(self) -> Dict:
        """生成给前端的渐进式提示状态

        Returns:
            summary 字典，包含 status（GREEN/YELLOW/RED）和 message
        """
        # 检查是否是纯空结构（没有任何 Chapter 节点）—— 按归一口径判定
        is_pure_empty = all(t != 'CHAPTER' for t in self._old_types.values())

        # H-05：分级 —— fatal 阻断，warning 只提示
        fatal = [
            c for c in self.conflicts
            if c.get("severity", CONFLICT_SEVERITY_FATAL) == CONFLICT_SEVERITY_FATAL
        ]
        warnings = [
            c for c in self.conflicts
            if c.get("severity") == CONFLICT_SEVERITY_WARNING
        ]

        if fatal:
            # 红色阻断：发现数据冲突
            return {
                "status": "RED",
                "message": "发现数据冲突，重构被阻断。",
                "conflicts": fatal,
                "warnings": warnings,
            }
        elif warnings:
            # 黄色提示：没有丢稿，但有旧节点仍承载未动笔的章节 ——
            # 已保留，不替作者搬家，等作者自己处理。
            return {
                "status": "YELLOW",
                "message": (
                    f"合并完成，但有 {len(warnings)} 个旧节点仍承载未动笔的章节，"
                    f"已保留待人工处理（新增 {len(self.to_create)} 个节点，"
                    f"更新 {len(self.to_update)} 个节点信息，"
                    f"修剪 {len(self.to_delete)} 个多余空节点）。"
                ),
                "warnings": warnings,
                "creates": len(self.to_create),
                "updates": len(self.to_update),
                "deletes": len(self.to_delete),
            }
        elif is_pure_empty:
            # 绿色通路：覆盖空框架
            return {
                "status": "GREEN",
                "message": f"覆盖空框架。新增 {len(self.to_create)} 个节点，删除 {len(self.to_delete)} 个空节点。",
                "creates": len(self.to_create),
                "updates": len(self.to_update),
                "deletes": len(self.to_delete)
            }
        else:
            # 黄色通路：安全合并
            return {
                "status": "YELLOW",
                "message": f"安全合并。保留已有正文，更新 {len(self.to_update)} 个节点信息，修剪 {len(self.to_delete)} 个多余空节点。",
                "creates": len(self.to_create),
                "updates": len(self.to_update),
                "deletes": len(self.to_delete)
            }
