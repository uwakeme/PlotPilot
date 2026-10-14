"""书库读模型：只读的书籍浏览数据。

与 ``NovelService`` 的分工：那条路服务「编辑」，会把每本书的全部章节正文
hydrate 进 DTO（一本 300 万字的书可达数十 MB）。书库只需要卡片和目录，
所以这里直接走 SQL 聚合，章节正文仅在 ``get_chapter`` 里按需取一次。

字数口径统一用 ``LENGTH(content)``：``chapters.word_count`` 列是写入期
陈旧产物，读它会让书库与侧边栏统计对不上
（依据 sqlite_stats_repository_adapter.get_book_word_totals 的说明）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from application.core.dtos.novel_dto import public_stage_name
from application.core.premise_genre_world import parse_genre_world_from_premise
from domain.novel.value_objects.generation_preferences import GenerationPreferences
from infrastructure.persistence.database.connection import (
    DatabaseConnection,
    get_database,
)
from infrastructure.persistence.database.reading_progress_repository import (
    ReadingProgressRepository,
)

_BOOKS_SQL = """
SELECT n.id AS novel_id,
       n.slug,
       n.title,
       n.author,
       n.target_chapters,
       n.current_stage,
       n.generation_prefs_json,
       n.premise,
       COUNT(c.id)                                 AS chapter_count,
       COALESCE(SUM(LENGTH(c.content)), 0)         AS total_word_count,
       rp.last_chapter_number,
       rp.last_read_at,
       (SELECT t.title FROM chapters t
         WHERE t.novel_id = n.id AND t.number = rp.last_chapter_number) AS last_read_title
FROM novels n
LEFT JOIN chapters c
       ON c.novel_id = n.id AND COALESCE(LENGTH(c.content), 0) > 0
LEFT JOIN reading_progress rp
       ON rp.novel_id = n.id
GROUP BY n.id
"""

# 阅读过的书在前按阅读时间倒序；未读过的按小说更新时间倒序。
_SORT_ORDERS = {
    "recent": "ORDER BY (rp.novel_id IS NULL) ASC, COALESCE(rp.last_read_at, n.updated_at) DESC",
    "updated": "ORDER BY n.updated_at DESC",
}

# 翻页按章号取相邻章，不依赖行序——章节被删后仍能正确翻页。
# 两条 SQL 各自写全，不做字符串插值：方向是封闭的字面量集合，
# 写成常量后这段代码里就不存在「拼接出来的 SQL」，无需再论证注入面。
_NEIGHBOUR_SQL = {
    -1: "SELECT number FROM chapters WHERE novel_id = ? AND number < ? "
        "ORDER BY number DESC LIMIT 1",
    1: "SELECT number FROM chapters WHERE novel_id = ? AND number > ? "
       "ORDER BY number ASC LIMIT 1",
}


def _chapter_display_title(title: Optional[str], number: int) -> str:
    """章节标题兜底，与 ExportService 同口径。"""
    cleaned = (title or "").strip()
    return cleaned if cleaned else f"第 {number} 章"


class ProgressWriteNotFlushedError(RuntimeError):
    """读进度写队列超时，未能确认落盘。

    与「书不存在」（None）区分开：前者是 404，后者是 503。
    """


class LibraryService:
    """书库只读查询。"""

    def __init__(self, db: DatabaseConnection | None = None) -> None:
        self.db = db if db is not None else get_database()
        self.progress_repo = ReadingProgressRepository(self.db)

    # ── 列表 ───────────────────────────────────────────────

    def list_books(self, sort: str = "recent") -> List[Dict[str, Any]]:
        order = _SORT_ORDERS.get(sort, _SORT_ORDERS["recent"])
        rows = self.db.fetch_all(f"{_BOOKS_SQL} {order}")
        return [self._to_book_item(row) for row in rows]

    def _to_book_item(self, row: Dict[str, Any]) -> Dict[str, Any]:
        prefs = GenerationPreferences.from_json(row.get("generation_prefs_json"))
        parsed_genre, parsed_world = parse_genre_world_from_premise(row.get("premise") or "")

        last_read = None
        last_number = row.get("last_chapter_number")
        if last_number is not None:
            last_read = {
                "chapter_number": int(last_number),
                "chapter_title": _chapter_display_title(row.get("last_read_title"), int(last_number)),
                "read_at": row.get("last_read_at"),
            }

        return {
            "slug": row["slug"],
            "title": row["title"],
            "author": row.get("author") or "",
            "genre": prefs.locked_genre or parsed_genre,
            "world_preset": prefs.locked_world_preset or parsed_world,
            "stage": public_stage_name(row.get("current_stage")),
            "chapter_count": int(row["chapter_count"] or 0),
            "target_chapters": int(row["target_chapters"] or 0),
            "total_word_count": int(row["total_word_count"] or 0),
            "last_read": last_read,
        }

    # ── 目录 ───────────────────────────────────────────────

    def get_toc(self, novel_id: str) -> Optional[Dict[str, Any]]:
        book = self.db.fetch_one(
            "SELECT id, slug, title FROM novels WHERE id = ?", (novel_id,)
        )
        if book is None:
            return None

        rows = self.db.fetch_all(
            "SELECT number, title, COALESCE(LENGTH(content), 0) AS word_count, status "
            "FROM chapters WHERE novel_id = ? ORDER BY number ASC",
            (book["id"],),
        )
        return {
            "slug": book["slug"],
            "title": book["title"],
            "chapters": [
                {
                    "number": int(r["number"]),
                    "title": _chapter_display_title(r.get("title"), int(r["number"])),
                    "word_count": int(r["word_count"] or 0),
                    "status": r.get("status") or "draft",
                }
                for r in rows
            ],
        }

    # ── 单章 ───────────────────────────────────────────────

    def get_chapter(self, novel_id: str, number: int) -> Optional[Dict[str, Any]]:
        book = self.db.fetch_one(
            "SELECT id, slug FROM novels WHERE id = ?", (novel_id,)
        )
        if book is None:
            return None

        row = self.db.fetch_one(
            "SELECT number, title, content, status "
            "FROM chapters WHERE novel_id = ? AND number = ?",
            (book["id"], number),
        )
        if row is None:
            return None

        return {
            "slug": book["slug"],
            "number": int(row["number"]),
            "title": _chapter_display_title(row.get("title"), int(row["number"])),
            # 原样返回：换行归一化与 markdown 渲染都交给前端
            "content": row.get("content") or "",
            "word_count": len(row.get("content") or ""),
            "prev_number": self._neighbour_number(book["id"], number, direction=-1),
            "next_number": self._neighbour_number(book["id"], number, direction=1),
        }

    def _neighbour_number(self, novel_id: str, number: int, direction: int) -> Optional[int]:
        """按章号取相邻章；``direction`` 只能是 -1（上一章）或 1（下一章）。"""
        sql = _NEIGHBOUR_SQL.get(direction)
        if sql is None:
            raise ValueError(f"direction 只能是 -1 或 1，收到 {direction!r}")
        row = self.db.fetch_one(sql, (novel_id, number))
        return int(row["number"]) if row else None

    # ── 进度 ───────────────────────────────────────────────

    def record_progress(self, novel_id: str, chapter_number: int) -> Optional[Dict[str, Any]]:
        book = self.db.fetch_one("SELECT id FROM novels WHERE id = ?", (novel_id,))
        if book is None:
            return None

        # record() 返回 None = 写队列超时，回读可能是上一章的旧行。
        # 那时绝不能把旧章号当新章号返回，必须让调用方知道写没确认落盘。
        row = self.progress_repo.record(novel_id, chapter_number)
        if row is None:
            raise ProgressWriteNotFlushedError(
                f"读进度写队列超时，未确认落盘: novel={novel_id} chapter={chapter_number}"
            )
        # 走到这里说明写已确认落盘，last_chapter_number 必然存在且非空
        # （列 NOT NULL）。这里不做「取不到就拿入参顶上」的兜底：真出现缺列
        # 就该报错，而不是返回一个从没被写进去过的章号。
        return {
            "slug": novel_id,
            "last_chapter_number": int(row["last_chapter_number"]),
            "read_at": row.get("last_read_at"),
        }