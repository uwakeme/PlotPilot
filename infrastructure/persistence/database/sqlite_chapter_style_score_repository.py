"""SQLite 章节文风评分仓储"""
from typing import List, Optional
from uuid import uuid4


class SqliteChapterStyleScoreRepository:
    """读写 chapter_style_scores 表。"""

    def __init__(self, db_connection):
        self.db = db_connection

    def upsert(
        self,
        novel_id: str,
        chapter_number: int,
        adjective_density: float,
        avg_sentence_length: float,
        sentence_count: int,
        similarity_score: Optional[float],  # None 表示无指纹基准
    ) -> str:
        existing = self.get_by_chapter(novel_id, chapter_number)
        if existing:
            self.db.execute(
                """
                UPDATE chapter_style_scores
                SET adjective_density = ?,
                    avg_sentence_length = ?,
                    sentence_count = ?,
                    similarity_score = ?,
                    computed_at = CURRENT_TIMESTAMP
                WHERE novel_id = ? AND chapter_number = ?
                """,
                (
                    adjective_density,
                    avg_sentence_length,
                    sentence_count,
                    similarity_score,
                    novel_id,
                    chapter_number,
                ),
            )
            return existing["score_id"]

        score_id = str(uuid4())
        self.db.execute(
            """
            INSERT INTO chapter_style_scores
            (score_id, novel_id, chapter_number, adjective_density,
             avg_sentence_length, sentence_count, similarity_score)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                score_id,
                novel_id,
                chapter_number,
                adjective_density,
                avg_sentence_length,
                sentence_count,
                similarity_score,
            ),
        )
        return score_id

    def get_by_chapter(
        self, novel_id: str, chapter_number: int
    ) -> Optional[dict]:
        row = self.db.fetch_one(
            """
            SELECT score_id, novel_id, chapter_number, adjective_density,
                   avg_sentence_length, sentence_count, similarity_score, computed_at
            FROM chapter_style_scores
            WHERE novel_id = ? AND chapter_number = ?
            """,
            (novel_id, chapter_number),
        )
        return dict(row) if row else None

    def get_latest_by_novel(self, novel_id: str) -> Optional[dict]:
        """取最近一次有记录的章节评分（按章号倒序取第一条）。

        ``list_by_novel`` 是 ASC + LIMIT，拿到的总是**最早**的 N 章，
        无法回答「当前漂移是多少」，所以这里单开一个倒序的只读方法。
        """
        row = self.db.fetch_one(
            """
            SELECT score_id, novel_id, chapter_number, adjective_density,
                   avg_sentence_length, sentence_count, similarity_score, computed_at
            FROM chapter_style_scores
            WHERE novel_id = ? AND similarity_score IS NOT NULL
            ORDER BY chapter_number DESC
            LIMIT 1
            """,
            (novel_id,),
        )
        return dict(row) if row else None

    def list_by_novel(
        self, novel_id: str, limit: int = 50
    ) -> List[dict]:
        rows = self.db.fetch_all(
            """
            SELECT score_id, novel_id, chapter_number, adjective_density,
                   avg_sentence_length, sentence_count, similarity_score, computed_at
            FROM chapter_style_scores
            WHERE novel_id = ?
            ORDER BY chapter_number ASC
            LIMIT ?
            """,
            (novel_id, limit),
        )
        return [dict(r) for r in rows]

    def list_recent_by_novel(
        self, novel_id: str, limit: int = 50
    ) -> List[dict]:
        """取**最近** ``limit`` 章的评分（**按章号正序返回**）。

        ⚠ 第 82 批（N-04）：``list_by_novel`` 是 ``ORDER BY chapter_number ASC LIMIT ?``
          ⇒ 拿到的是**最早**的 N 章。
          ⇒ 「最近 N 章连续低分」的告警窗口必须用这个方法，
          否则超过 N 章的书，告警永远反映第 1~N 章。

        ⚠ 返回顺序仍是**正序**（升序）：调用方取尾部 ⇒ 拿到的就是**最新**那几章。
        """
        rows = self.db.fetch_all(
            """
            SELECT score_id, novel_id, chapter_number, adjective_density,
                   avg_sentence_length, sentence_count, similarity_score, computed_at
            FROM chapter_style_scores
            WHERE novel_id = ?
              AND chapter_number IN (
                    SELECT chapter_number
                      FROM chapter_style_scores
                     WHERE novel_id = ?
                     ORDER BY chapter_number DESC
                     LIMIT ?
              )
            ORDER BY chapter_number ASC
            """,
            (novel_id, novel_id, limit),
        )
        return [dict(r) for r in rows]
