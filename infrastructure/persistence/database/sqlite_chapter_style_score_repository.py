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
        style_vector_json: Optional[str] = None,  # 第 86 批 · N-18：LLM 风格向量
    ) -> str:
        """写入（或更新）一章的文风评分。

        ⚠ 第 86 批（N-18）：``style_vector_json`` 存的是**同量纲**的 LLM 风格向量
          ⇒ 它是「LLM 模式基准」的唯一来源（基准必须与当前值同一把尺子）。
          ⇒ 为 ``None`` 时**不覆盖**已有值（统计模式评分不该把 LLM 向量擦掉）。
        """
        existing = self.get_by_chapter(novel_id, chapter_number)
        if existing:
            self.db.execute(
                """
                UPDATE chapter_style_scores
                SET adjective_density = ?,
                    avg_sentence_length = ?,
                    sentence_count = ?,
                    similarity_score = ?,
                    style_vector_json = COALESCE(?, style_vector_json),
                    computed_at = CURRENT_TIMESTAMP
                WHERE novel_id = ? AND chapter_number = ?
                """,
                (
                    adjective_density,
                    avg_sentence_length,
                    sentence_count,
                    similarity_score,
                    style_vector_json,
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
             avg_sentence_length, sentence_count, similarity_score, style_vector_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                score_id,
                novel_id,
                chapter_number,
                adjective_density,
                avg_sentence_length,
                sentence_count,
                similarity_score,
                style_vector_json,
            ),
        )
        return score_id

    def upsert_style_vector(
        self, novel_id: str, chapter_number: int, style_vector_json: str
    ) -> None:
        """**只**写风格向量，不碰该行的其它列（第 86 批 · N-18 的基准回填用）。

        ⚠ 为什么要单独一个方法：回填时我们只想补齐「同量纲基准」的原料，
          绝不能顺手改写那一章的相似度/统计指标（那是「重新评分」的语义）。
        """
        self.db.execute(
            """
            INSERT INTO chapter_style_scores
            (score_id, novel_id, chapter_number, adjective_density,
             avg_sentence_length, sentence_count, similarity_score, style_vector_json)
            VALUES (?, ?, ?, 0.0, 0.0, 0, NULL, ?)
            ON CONFLICT(novel_id, chapter_number)
            DO UPDATE SET style_vector_json = excluded.style_vector_json
            """,
            (str(uuid4()), novel_id, chapter_number, style_vector_json),
        )

    def get_by_chapter(
        self, novel_id: str, chapter_number: int
    ) -> Optional[dict]:
        row = self.db.fetch_one(
            """
            SELECT score_id, novel_id, chapter_number, adjective_density,
                   avg_sentence_length, sentence_count, similarity_score,
                   style_vector_json, computed_at
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
                   avg_sentence_length, sentence_count, similarity_score,
                   style_vector_json, computed_at
            FROM chapter_style_scores
            WHERE novel_id = ? AND similarity_score IS NOT NULL
            ORDER BY chapter_number DESC
            LIMIT 1
            """,
            (novel_id,),
        )
        return dict(row) if row else None

    def list_by_novel(
        self, novel_id: str, limit: Optional[int] = 50
    ) -> List[dict]:
        """取该书的评分行（**按章号升序**）。

        ⚠ 第 90 批（N-22）：``limit=None`` ⇒ **不限**（返回全部）。
          此前默认 50 ⇒ 漂移报告（承诺「全量章节评分序列」）
          对长书只会返回**最早** 50 行 ⇒ 面板看到的是开篇数据，不是现状。

        ⚠ 注意方向：这是 **ASC + LIMIT** ⇒ 拿到的总是**最早**的 N 章。
          「最近 N 章」必须用 ``list_recent_by_novel``。
        """
        sql = """
            SELECT score_id, novel_id, chapter_number, adjective_density,
                   avg_sentence_length, sentence_count, similarity_score,
                   style_vector_json, computed_at
            FROM chapter_style_scores
            WHERE novel_id = ?
            ORDER BY chapter_number ASC
        """
        params: tuple = (novel_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (novel_id, int(limit))
        rows = self.db.fetch_all(sql, params)
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
                   avg_sentence_length, sentence_count, similarity_score,
                   style_vector_json, computed_at
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

