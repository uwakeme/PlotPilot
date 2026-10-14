"""阅读进度仓储：记录每本书读到哪一章。

写入用 UPSERT 而非先查后写：阅读器会在「打开章节」和「切换章节」两个时机
各上报一次，同一章号重复上报不应产生第二行。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from infrastructure.persistence.database.connection import (
    DatabaseConnection,
    get_database,
)

logger = logging.getLogger(__name__)


class ReadingProgressRepository:
    """reading_progress 表读写。"""

    def __init__(self, db: DatabaseConnection | None = None) -> None:
        self.db = db if db is not None else get_database()

    def get(self, novel_id: str) -> Optional[Dict[str, Any]]:
        """返回进度行；从未读过则 None。"""
        return self.db.fetch_one(
            "SELECT novel_id, last_chapter_number, last_read_at "
            "FROM reading_progress WHERE novel_id = ?",
            (novel_id,),
        )

    def record(self, novel_id: str, chapter_number: int) -> Optional[Dict[str, Any]]:
        """记录读到第几章并返回该行。

        Returns:
            进度行；入队写未能确认落盘时返回 None——此时回读可能是旧行，
            返回它等同于撒谎，调用方需要能区分。
        """
        self.db.execute(
            "INSERT INTO reading_progress (novel_id, last_chapter_number, last_read_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(novel_id) DO UPDATE SET "
            "  last_chapter_number = excluded.last_chapter_number, "
            "  last_read_at = CURRENT_TIMESTAMP",
            (novel_id, chapter_number),
        )
        # 入队写是最终一致语义：execute() 返回时消费者线程可能还没执行，
        # 直接回读会拿到旧行。先等队列排空；超时则拒绝返回可能过期的数据。
        if not self.db.flush_pending_writes():
            logger.warning(
                "读进度写队列超时，未确认落盘 novel=%s chapter=%s", novel_id, chapter_number
            )
            return None
        return self.get(novel_id)

    def delete_by_novel(self, novel_id: str) -> None:
        """显式清理。正常删除小说走外键级联，此方法供数据修复与测试使用。"""
        self.db.execute(
            "DELETE FROM reading_progress WHERE novel_id = ?",
            (novel_id,),
        )