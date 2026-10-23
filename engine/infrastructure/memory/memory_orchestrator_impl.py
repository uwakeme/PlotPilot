"""记忆编排器实现 — MemoryOrchestrator

G-04-b：原先这里还有一整套「三层记忆模型」的**读回装配**
（``assemble_context`` + ``_assemble_t0..t3`` + ``_apply_budget_pressure``），
但它**零生产调用方**（生产上下文的装配早已由
``application/engine/services/context_budget_allocator.py`` 承担，
两套并存、语义不同）⇒ 已删除，避免「两套都留着」。

本模块现在只负责**写路径**：
- ``update_emotion_ledger``：每章抽取情绪账本写进 checkpoints；
- ``restore_state`` / ``manage_foreshadow``：引擎状态恢复与伏笔生命周期。
"""
from __future__ import annotations

import json
import logging
from typing import Dict, Any, List, Optional

from engine.core.services.memory_orchestrator import (
    MemoryOrchestrator,
)
from engine.core.entities.story import StoryId
from engine.core.value_objects.emotion_ledger import EmotionLedger
from engine.infrastructure.memory.echo_recall import EchoRecall
from engine.infrastructure.memory.character_psyche import CharacterPsycheEngine
from engine.infrastructure.memory.emotion_ledger_extractor import EmotionLedgerExtractor

logger = logging.getLogger(__name__)


class MemoryOrchestratorImpl(MemoryOrchestrator):
    """记忆编排器实现（**写路径**：情绪账本抽取 / 状态恢复 / 伏笔生命周期）"""

    def __init__(
        self,
        db_pool=None,
        character_psyche: Optional[CharacterPsycheEngine] = None,
        echo_recall: Optional[EchoRecall] = None,
        llm_service=None,
    ):
        self._db_pool = db_pool
        self._character_psyche = character_psyche or CharacterPsycheEngine(db_pool)
        self._echo_recall = echo_recall or EchoRecall(db_pool)
        self._ledger_extractor = EmotionLedgerExtractor(llm_service=llm_service)

    def _load_emotion_ledger_json(self, story_id: str) -> Optional[Dict[str, Any]]:
        """从 checkpoint 读取当前情绪账本"""
        if not self._db_pool:
            return None
        with self._db_pool.get_connection() as conn:
            row = conn.execute(
                """SELECT c.emotion_ledger
                   FROM checkpoint_heads h
                   JOIN checkpoints c ON c.id = h.checkpoint_id
                   WHERE h.story_id = ? AND c.is_active = 1""",
                (story_id,),
            ).fetchone()
            if row and row["emotion_ledger"]:
                return json.loads(row["emotion_ledger"])

            row = conn.execute(
                """SELECT emotion_ledger FROM checkpoints
                   WHERE story_id = ? AND is_active = 1
                   ORDER BY created_at DESC LIMIT 1""",
                (story_id,),
            ).fetchone()
            if row and row["emotion_ledger"]:
                return json.loads(row["emotion_ledger"])
        return None

    async def update_emotion_ledger(
        self,
        story_id: StoryId,
        chapter_number: int,
        chapter_content: str,
    ) -> EmotionLedger:
        """更新情绪账本（从章节内容中提取情感变化）"""
        current_ledger = EmotionLedger.create_empty()

        try:
            ledger_data = self._load_emotion_ledger_json(story_id.value)
            if ledger_data:
                current_ledger = EmotionLedger.from_dict(ledger_data)
        except Exception as e:
            logger.debug(f"读取EmotionLedger失败: {e}")

        content = (chapter_content or "").strip()
        if len(content) < 100:
            return current_ledger

        deltas = await self._ledger_extractor.extract_deltas(
            chapter_content=content,
            chapter_number=chapter_number,
            current_ledger=current_ledger,
        )
        updated_ledger = self._ledger_extractor.merge_ledger(
            current_ledger, deltas, chapter_number
        )

        if updated_ledger != current_ledger:
            await self._persist_emotion_ledger(story_id, updated_ledger)

        return updated_ledger

    async def _persist_emotion_ledger(self, story_id: StoryId, ledger: EmotionLedger) -> None:
        """将情绪账本写入 checkpoint"""
        if not self._db_pool:
            return
        payload = json.dumps(ledger.to_dict(), ensure_ascii=False)
        try:
            with self._db_pool.get_connection() as conn:
                row = conn.execute(
                    "SELECT checkpoint_id FROM checkpoint_heads WHERE story_id = ?",
                    (story_id.value,),
                ).fetchone()
                checkpoint_id = row["checkpoint_id"] if row else None

                if not checkpoint_id:
                    row = conn.execute(
                        """SELECT id FROM checkpoints
                           WHERE story_id = ? AND is_active = 1
                           ORDER BY created_at DESC LIMIT 1""",
                        (story_id.value,),
                    ).fetchone()
                    checkpoint_id = row["id"] if row else None

                if not checkpoint_id:
                    logger.debug(
                        "[EmotionLedger] 无 checkpoint，跳过持久化 story=%s",
                        story_id.value,
                    )
                    return

                conn.execute(
                    "UPDATE checkpoints SET emotion_ledger = ? WHERE id = ?",
                    (payload, checkpoint_id),
                )
                conn.commit()
            logger.info(
                "[EmotionLedger] 已更新 story=%s checkpoint=%s wounds=%d boons=%d loops=%d",
                story_id.value,
                checkpoint_id,
                len(ledger.wounds),
                len(ledger.boons),
                len(ledger.open_loops),
            )
        except Exception as e:
            logger.warning("[EmotionLedger] 持久化失败: %s", e)

    async def restore_state(
        self,
        story_id: StoryId,
        character_masks: Dict[str, Any],
        emotion_ledger: Dict[str, Any],
        active_foreshadows: List[str],
        outline: str = "",
        recent_summary: str = "",
    ) -> None:
        """从 Checkpoint 数据恢复内存状态。"""
        if not self._db_pool:
            return
        try:
            payload = json.dumps(emotion_ledger, ensure_ascii=False)
            with self._db_pool.get_connection() as conn:
                row = conn.execute(
                    "SELECT checkpoint_id FROM checkpoint_heads WHERE story_id = ?",
                    (story_id.value,),
                ).fetchone()
                checkpoint_id = row["checkpoint_id"] if row else None
                if checkpoint_id:
                    conn.execute(
                        "UPDATE checkpoints SET emotion_ledger = ? WHERE id = ?",
                        (payload, checkpoint_id),
                    )
                    conn.commit()
            logger.info("[MemoryRestore] 情绪账本已恢复 story=%s", story_id.value)
        except Exception as e:
            logger.warning("[MemoryRestore] 状态恢复失败（非致命）: %s", e)

    async def manage_foreshadow(
        self,
        story_id: StoryId,
        foreshadow_id: str,
        action: str,
        chapter_number: int = 0,
    ) -> None:
        """管理伏笔生命周期"""
        if not self._db_pool:
            return

        try:
            with self._db_pool.get_connection() as conn:
                if action == "plant":
                    # 新伏笔在Foreshadow实体创建时已处理
                    pass
                elif action == "reference":
                    conn.execute(
                        """UPDATE foreshadows
                           SET reference_count = reference_count + 1
                           WHERE id = ?""",
                        (foreshadow_id,)
                    )
                elif action == "awaken":
                    conn.execute(
                        """UPDATE foreshadows SET status = 'awakening'
                           WHERE id = ? AND status IN ('planted', 'referenced')""",
                        (foreshadow_id,)
                    )
                elif action == "resolve":
                    conn.execute(
                        """UPDATE foreshadows
                           SET status = 'resolved', resolved_in_chapter = ?
                           WHERE id = ?""",
                        (chapter_number, foreshadow_id)
                    )
                elif action == "abandon":
                    conn.execute(
                        "UPDATE foreshadows SET status = 'abandoned' WHERE id = ?",
                        (foreshadow_id,)
                    )

                conn.commit()
        except Exception as e:
            logger.error(f"管理伏笔失败: {e}")
