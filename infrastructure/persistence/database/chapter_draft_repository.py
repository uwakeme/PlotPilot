"""章节历史草稿仓储（章节正文的版本快照与回退）"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import List, Optional

from infrastructure.persistence.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

_MAX_DRAFTS_PER_CHAPTER = 30  # 每章最多保留的历史版本数

# 快照来源语义（chapter_drafts.source）：
#   manual_save  手动保存前
#   pre_regen    重新生成前
#   pre_prose    AI 正文生成写回前
#   pre_revise   AI 交互式优化写回前
#   pre_restore  恢复旧版本前（保存当前内容）
#   pre_autopilot 自动驾驶收稿覆写前
#   auto_gen     首次生成


class ChapterDraftRecord:
    """章节历史草稿数据对象"""

    def __init__(
        self,
        id: str,
        novel_id: str,
        chapter_id: str,
        chapter_number: int,
        content: str,
        outline: str,
        source: str,
        word_count: int,
        created_at: str,
    ) -> None:
        self.id = id
        self.novel_id = novel_id
        self.chapter_id = chapter_id
        self.chapter_number = chapter_number
        self.content = content
        self.outline = outline
        self.source = source
        self.word_count = word_count
        self.created_at = created_at


class ChapterDraftRepository:
    """章节历史草稿仓储：追加写入、按时间倒序列出、自动修剪旧版本。"""

    def __init__(self, db: DatabaseConnection) -> None:
        self.db = db

    def save_draft(
        self,
        novel_id: str,
        chapter_id: str,
        chapter_number: int,
        content: str,
        outline: str = "",
        source: str = "manual_save",
    ) -> ChapterDraftRecord:
        """保存一个历史草稿快照，并自动修剪超出上限的旧版本。

        Args:
            source: 快照来源，见模块头注释。
        """
        if not content or not content.strip():
            raise ValueError("不能保存空内容的草稿")

        draft_id = str(uuid.uuid4())
        word_count = len(content.replace(" ", ""))
        now = datetime.utcnow().isoformat()

        sql = """
            INSERT INTO chapter_drafts
                (id, novel_id, chapter_id, chapter_number, content, outline, source, word_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.db.execute(sql, (
            draft_id, novel_id, chapter_id, chapter_number,
            content, outline, source, word_count, now
        ))
        logger.info(
            "Saved chapter draft: novel=%s chapter=%s source=%s words=%d",
            novel_id, chapter_number, source, word_count
        )

        self._trim_old_drafts(novel_id, chapter_number)

        return ChapterDraftRecord(
            id=draft_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            chapter_number=chapter_number,
            content=content,
            outline=outline,
            source=source,
            word_count=word_count,
            created_at=now,
        )

    def list_drafts(
        self,
        novel_id: str,
        chapter_number: int,
        limit: int = _MAX_DRAFTS_PER_CHAPTER,
    ) -> List[ChapterDraftRecord]:
        """列出指定章节的历史草稿，按时间倒序排列（最新在前）。"""
        sql = """
            SELECT id, novel_id, chapter_id, chapter_number, content, outline,
                   source, word_count, created_at
            FROM chapter_drafts
            WHERE novel_id = ? AND chapter_number = ?
            ORDER BY created_at DESC
            LIMIT ?
        """
        rows = self.db.fetch_all(sql, (novel_id, chapter_number, limit))
        return [self._row_to_record(r) for r in rows]

    def get_draft(self, draft_id: str) -> Optional[ChapterDraftRecord]:
        """按 ID 获取单条草稿。"""
        sql = """
            SELECT id, novel_id, chapter_id, chapter_number, content, outline,
                   source, word_count, created_at
            FROM chapter_drafts
            WHERE id = ?
        """
        row = self.db.fetch_one(sql, (draft_id,))
        return self._row_to_record(row) if row else None

    def restore_draft(self, novel_id: str, chapter_number: int, draft_id: str) -> dict:
        """把章节正文恢复为指定历史版本。

        恢复前把当前内容自动存为 ``pre_restore`` 快照（可再回退到恢复前）；
        只更新正文与字数，章节 status 保持不变（与 AI 优化写回同语义）。
        """
        draft = self.get_draft(draft_id)
        if draft is None or draft.novel_id != novel_id or draft.chapter_number != chapter_number:
            raise ValueError(f"草稿不存在或不属于该章节: {draft_id}")

        existing = self.db.fetch_one(
            "SELECT id, content FROM chapters WHERE novel_id = ? AND number = ?",
            (novel_id, chapter_number),
        )
        if existing is None:
            raise ValueError(f"章节不存在: novel={novel_id} chapter={chapter_number}")

        current_content = existing["content"] or ""
        if current_content.strip():
            self.save_draft(
                novel_id, existing["id"], chapter_number,
                current_content, source="pre_restore",
            )

        self.db.execute(
            """
            UPDATE chapters
            SET content = ?, word_count = ?, updated_at = CURRENT_TIMESTAMP
            WHERE novel_id = ? AND number = ?
            """,
            (draft.content, draft.word_count, novel_id, chapter_number),
        )
        logger.info(
            "Restored chapter draft: novel=%s chapter=%s draft=%s source=%s",
            novel_id, chapter_number, draft_id, draft.source,
        )
        return {
            "restored": True,
            "draft_id": draft_id,
            "chapter_id": existing["id"],
            "chapter_number": chapter_number,
            "word_count": draft.word_count,
            "previous_content_saved_as": "pre_restore",
        }

    def _trim_old_drafts(self, novel_id: str, chapter_number: int) -> None:
        """删除超出上限的最旧草稿。"""
        delete_sql = """
            DELETE FROM chapter_drafts
            WHERE id IN (
                SELECT id FROM chapter_drafts
                WHERE novel_id = ? AND chapter_number = ?
                ORDER BY created_at DESC
                LIMIT -1 OFFSET ?
            )
        """
        self.db.execute(delete_sql, (novel_id, chapter_number, _MAX_DRAFTS_PER_CHAPTER))

    @staticmethod
    def _row_to_record(row: dict) -> ChapterDraftRecord:
        return ChapterDraftRecord(
            id=row["id"],
            novel_id=row["novel_id"],
            chapter_id=row["chapter_id"],
            chapter_number=row["chapter_number"],
            content=row["content"],
            outline=row["outline"],
            source=row["source"],
            word_count=row["word_count"],
            created_at=row["created_at"],
        )


def snapshot_chapter_before_content_change(
    db: DatabaseConnection,
    novel_id: str,
    chapter_number: int,
    source: str,
    *,
    skip_extension: bool = False,
) -> Optional[ChapterDraftRecord]:
    """在覆写章节正文前打快照；内容为空或与最近一次快照相同则跳过。

    供所有正文写路径调用（手动保存 / AI 投影 / 自动驾驶收稿），幂等且零重复：
    高频路径内容未变化时只有一次 SELECT 成本。
    """
    existing = db.fetch_one(
        "SELECT id, content FROM chapters WHERE novel_id = ? AND number = ?",
        (novel_id, chapter_number),
    )
    if existing is None:
        return None
    content = existing["content"] or ""
    if not content.strip():
        return None

    repo = ChapterDraftRepository(db)
    latest = repo.list_drafts(novel_id, chapter_number, limit=1)
    if latest:
        latest_content = latest[0].content
        if latest_content == content:
            return None  # 最近快照即当前内容，无需重复留痕
        # 流式/增量写作的纯延伸（或收缩）不单独留痕——仅自动驾驶高频路径启用，
        # 手动保存 / AI 修改是用户的完整编辑动作，必须每次留痕。
        if skip_extension and latest_content and (
            content.startswith(latest_content) or latest_content.startswith(content)
        ):
            return None
    try:
        return repo.save_draft(
            novel_id, existing["id"], chapter_number, content, source=source
        )
    except ValueError:
        return None
