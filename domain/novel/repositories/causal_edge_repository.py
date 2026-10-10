"""因果边仓储抽象接口"""
from abc import ABC, abstractmethod
from typing import List, Optional
from domain.novel.value_objects.causal_edge import CausalEdge


class CausalEdgeRepository(ABC):
    """因果边仓储抽象接口"""

    @abstractmethod
    def save(self, edge: CausalEdge) -> None:
        """保存因果边"""
        pass

    @abstractmethod
    def save_batch(self, edges: List[CausalEdge]) -> None:
        """批量保存因果边"""
        pass

    @abstractmethod
    def get_by_novel(self, novel_id: str) -> List[CausalEdge]:
        """获取小说的所有因果边"""
        pass

    @abstractmethod
    def get_unresolved(
        self, novel_id: str, limit: Optional[int] = None
    ) -> List[CausalEdge]:
        """获取未闭环的因果边

        Args:
            novel_id: 小说 ID
            limit: 最多返回多少条；None 表示不限制。调用方若只消费前 N 条，
                应把 N 交给实现下推到 SQL，而不是取回全集再切片。

        实现约定：返回顺序必须稳定（同一 novel_id 重复调用得到同样顺序），
        顺序本身是选择策略的一部分——未闭环边可能上千条，排序决定哪些边
        能进入每章的评估窗口。
        """
        pass

    @abstractmethod
    def get_by_character(self, novel_id: str, character_id: str) -> List[CausalEdge]:
        """获取涉及指定角色的因果边"""
        pass

    @abstractmethod
    def get_chains_involving(self, novel_id: str, entity_name: str) -> List[CausalEdge]:
        """获取涉及指定实体名称的因果链

        用于 ACTIVE_ENTITY_MEMORY 槽位：
        当大纲提到某角色/势力时，查出所有相关因果边，
        注入到 T0 层，确保 AI 知道此实体的因果历史。
        """
        pass

    @abstractmethod
    def resolve(self, edge_id: str, chapter: int) -> bool:
        """标记因果边已闭环。

        Returns:
            True 表示该因果边存在并已被标记闭环；False 表示 edge_id 不存在。
            调用方依赖这个返回值区分"真的闭环了"与"传入了一个不存在的 id"，
            实现不得对未知 id 静默成功。
        """
        pass

    @abstractmethod
    def get_high_strength_unresolved(self, novel_id: str, min_strength: float = 0.7) -> List[CausalEdge]:
        """获取高强度的未闭环因果边（用于叙事债务检测）"""
        pass

    @abstractmethod
    def delete(self, edge_id: str) -> bool:
        """删除因果边"""
        pass
