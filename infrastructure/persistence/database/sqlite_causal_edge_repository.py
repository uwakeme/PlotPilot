"""SQLite 因果边仓储实现"""
import json
import logging
from typing import List, Optional
from domain.novel.value_objects.causal_edge import CausalEdge, CausalType
from domain.novel.repositories.causal_edge_repository import CausalEdgeRepository
from infrastructure.persistence.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)


class SqliteCausalEdgeRepository(CausalEdgeRepository):
    """SQLite 因果边仓储实现"""

    def __init__(self, db: DatabaseConnection):
        self.db = db

    def save(self, edge: CausalEdge) -> None:
        """保存因果边"""
        self.db.execute("""
            INSERT OR REPLACE INTO causal_edges
            (id, novel_id, source_event_summary, source_chapter, causal_type,
             target_event_summary, target_chapter, strength, confidence,
             state_change, involved_characters, is_resolved, resolved_chapter,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM causal_edges WHERE id = ?), datetime('now')),
                    datetime('now'))
        """, (
            edge.id, edge.novel_id,
            edge.source_event_summary, edge.source_chapter,
            edge.causal_type.value,
            edge.target_event_summary, edge.target_chapter,
            edge.strength, edge.confidence,
            edge.state_change,
            json.dumps(edge.involved_characters, ensure_ascii=False),
            int(edge.is_resolved), edge.resolved_chapter,
            edge.id,
        ))
        self.db.get_connection().commit()

    def save_batch(self, edges: List[CausalEdge]) -> None:
        """批量保存因果边"""
        for edge in edges:
            self.save(edge)

    def get_by_novel(self, novel_id: str) -> List[CausalEdge]:
        """获取小说的所有因果边"""
        rows = self.db.fetch_all(
            "SELECT * FROM causal_edges WHERE novel_id = ? ORDER BY source_chapter ASC",
            (novel_id,),
        )
        return [self._row_to_edge(row) for row in rows]

    def get_unresolved(
        self, novel_id: str, limit: Optional[int] = None
    ) -> List[CausalEdge]:
        """获取未闭环的因果边，默认按 source_chapter DESC（近因优先）。

        排序理由：调用方每章只把固定的少数条（MAX_PENDING_CAUSAL_EDGES_FOR_LLM）
        交给 LLM 判定"本章正文是否兑现了这条边"。边离当前写作位置越近，与本章
        语境重合的概率越高，闭环机会越大——所以窗口应该落在最近的边上。
        反过来按 ASC（最旧优先）时，最老的一批边会永久霸占前 N 条名额，
        第 N 名之后的边再也得不到评估，等于给闭环判定设了功能天花板。

        代价（已知且有意接受）：长期挂着的旧边会滑出窗口。这正是我们要的——
        它们要么早已闭环（is_resolved=1 已不在集合里），要么本来就不会被兑现，
        继续占用名额只会挤掉真正有机会闭环的边。若将来需要"老边兜底翻牌"，
        应另开轮转策略（游标/分段窗口），而不是把排序改回 ASC。

        limit 在 SQL 层生效：未闭环边可达上千条，每章物化全部再在 Python 侧截断
        是纯浪费。None 表示不限制（保留完整集合语义，供不关心窗口的调用方使用）。
        """
        sql = (
            "SELECT * FROM causal_edges WHERE novel_id = ? AND is_resolved = 0 "
            "ORDER BY source_chapter DESC, id DESC"
        )
        params: List[object] = [novel_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self.db.fetch_all(sql, tuple(params))
        return [self._row_to_edge(row) for row in rows]

    def get_by_character(self, novel_id: str, character_id: str) -> List[CausalEdge]:
        """获取涉及指定角色的因果边"""
        rows = self.db.fetch_all(
            "SELECT * FROM causal_edges WHERE novel_id = ? AND involved_characters LIKE ? ORDER BY source_chapter ASC",
            (novel_id, f'%"{character_id}"%'),
        )
        return [self._row_to_edge(row) for row in rows]

    def get_chains_involving(self, novel_id: str, entity_name: str) -> List[CausalEdge]:
        """获取涉及指定实体名称的因果链

        搜索范围：source_event_summary, target_event_summary, involved_characters
        """
        pattern = f'%{entity_name}%'
        rows = self.db.fetch_all(
            """SELECT * FROM causal_edges WHERE novel_id = ?
               AND (source_event_summary LIKE ? OR target_event_summary LIKE ?
                    OR involved_characters LIKE ?)
               ORDER BY source_chapter ASC""",
            (novel_id, pattern, pattern, pattern),
        )
        return [self._row_to_edge(row) for row in rows]

    def resolve(self, edge_id: str, chapter: int) -> bool:
        """标记因果边已闭环。

        Returns:
            True 表示确实更新了一行；False 表示 edge_id 不存在。
        """
        self.db.execute(
            """UPDATE causal_edges SET is_resolved = 1, resolved_chapter = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (chapter, edge_id),
        )
        self.db.get_connection().commit()
        return self.db.execute(
            "SELECT 1 FROM causal_edges WHERE id = ?", (edge_id,)
        ).fetchone() is not None

    def get_high_strength_unresolved(self, novel_id: str, min_strength: float = 0.7) -> List[CausalEdge]:
        """获取高强度的未闭环因果边"""
        rows = self.db.fetch_all(
            "SELECT * FROM causal_edges WHERE novel_id = ? AND is_resolved = 0 AND strength >= ? ORDER BY strength DESC",
            (novel_id, min_strength),
        )
        return [self._row_to_edge(row) for row in rows]

    def delete(self, edge_id: str) -> bool:
        cur = self.db.execute("DELETE FROM causal_edges WHERE id = ?", (edge_id,))
        self.db.get_connection().commit()
        return cur.rowcount > 0

    def _row_to_edge(self, row) -> CausalEdge:
        """将数据库行转换为 CausalEdge 对象"""
        d = dict(row)
        return CausalEdge(
            id=d["id"],
            novel_id=d["novel_id"],
            source_event_summary=d["source_event_summary"],
            source_chapter=d["source_chapter"],
            causal_type=CausalType(d.get("causal_type", "causes")),
            target_event_summary=d["target_event_summary"],
            target_chapter=d.get("target_chapter"),
            strength=d.get("strength", 0.8),
            confidence=d.get("confidence", 0.7),
            state_change=d.get("state_change", ""),
            involved_characters=json.loads(d.get("involved_characters", "[]")),
            is_resolved=bool(d.get("is_resolved", 0)),
            resolved_chapter=d.get("resolved_chapter"),
        )
