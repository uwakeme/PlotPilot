"""Anti-AI 已学模式（规则库）仓储。

F-06：以前「学到的」模式只在内存 dict 里（全局单例）⇒ **重启即失**，
而且从没有回灌进扫描器。现在规则库落库 + **版本化**（登记表 F-06 的不变量）。

写入走 ``DatabaseConnection``（项目写库铁律），不裸开 ``sqlite3``。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 扫描器格式：(pattern, name, severity, category, hint)
ScannerPattern = Tuple[str, str, str, str, str]


class AntiAiPatternRepository:
    """已学模式的持久化。"""

    def __init__(self, db: Any = None) -> None:
        self._db = db

    def _get_db(self):
        if self._db is not None:
            return self._db
        from infrastructure.persistence.database.connection import get_database

        return get_database()

    # ── 读 ────────────────────────────────────────────────────────

    def list_approved(self, novel_id: str) -> List[ScannerPattern]:
        """该小说已批准的规则（可直接喂给扫描器）。"""
        if not novel_id:
            return []
        try:
            rows = self._get_db().fetch_all(
                """
                SELECT keyword, name, severity, category, hint
                FROM anti_ai_learned_patterns
                WHERE novel_id = ? AND status = 'approved'
                ORDER BY rule_version ASC, created_at ASC
                """,
                (str(novel_id),),
            )
        except Exception as exc:
            logger.warning("读取已学模式失败 novel=%s: %s", novel_id, exc)
            return []
        return [
            (
                str(r.get("keyword") or ""),
                str(r.get("name") or ""),
                str(r.get("severity") or "info"),
                str(r.get("category") or "本书特有"),
                str(r.get("hint") or ""),
            )
            for r in rows
            if str(r.get("keyword") or "").strip()
        ]

    def list_rejected(self, novel_id: str) -> List[str]:
        """该小说**被人工驳回**的规则关键词。

        为什么需要它：驳回必须**永久**。若只把「已批准」的喂给 AI 当「别再提」，
        AI 下一章会重新提同一条 ⇒ 规则被复活 ⇒ 人工驳回形同虚设。
        ⇒ 驳回过的关键词也要进 AI 的「别再提」清单。
        """
        if not novel_id:
            return []
        try:
            rows = self._get_db().fetch_all(
                """
                SELECT keyword FROM anti_ai_learned_patterns
                WHERE novel_id = ? AND status = 'rejected'
                ORDER BY rule_version ASC
                """,
                (str(novel_id),),
            )
        except Exception as exc:
            logger.warning("读取已驳回模式失败 novel=%s: %s", novel_id, exc)
            return []
        return [str(r.get("keyword") or "") for r in rows if str(r.get("keyword") or "").strip()]

    def current_version(self, novel_id: str) -> int:
        """规则库当前版本（该小说所有规则里的最大版本号；驳回不回退）。"""
        if not novel_id:
            return 0
        try:
            row = self._get_db().fetch_one(
                """
                SELECT MAX(rule_version) AS v FROM anti_ai_learned_patterns WHERE novel_id = ?
                """,
                (str(novel_id),),
            )
        except Exception as exc:
            logger.warning("读取规则库版本失败 novel=%s: %s", novel_id, exc)
            return 0
        if not row or row.get("v") is None:
            return 0
        try:
            return int(row["v"])
        except (TypeError, ValueError):
            return 0

    def exists(self, novel_id: str, keyword: str) -> bool:
        """该关键词是否已在规则库里（去重用）。"""
        if not novel_id or not keyword:
            return False
        try:
            row = self._get_db().fetch_one(
                """
                SELECT id FROM anti_ai_learned_patterns
                WHERE novel_id = ? AND keyword = ?
                LIMIT 1
                """,
                (str(novel_id), str(keyword)),
            )
        except Exception as exc:
            logger.warning("查重失败 novel=%s: %s", novel_id, exc)
            return False
        return row is not None

    # ── 写 ────────────────────────────────────────────────────────

    def upsert_approved(
        self,
        *,
        novel_id: str,
        keyword: str,
        name: str = "",
        severity: str = "info",
        category: str = "本书特有",
        reason: str = "",
        hint: str = "",
        chapter_number: int = 0,
        source: str = "ai",
        revive_rejected: bool = False,
    ) -> Optional[int]:
        """写入一条已批准规则；**已存在则原地更新元信息**（不产生重复行）。

        返回该规则的版本号；写失败返回 ``None``。

        ``revive_rejected``：
          - 默认 ``False``（AI 路径）—— 若这行**被人工驳回过**，**不复活**，
            返回 ``None``。人工驳回必须永久，否则「驳回」这个唯一的
            人工控制手段形同虚设（AI 下一章重新提一次就回来了）。
          - ``True``（人工路径 ``approve_pattern``）—— 允许把驳回过的规则
            恢复为已批准（人工显式操作）。
        """
        keyword = str(keyword or "").strip()
        if not novel_id or not keyword:
            return None
        now = datetime.now().isoformat()
        try:
            existing = self._get_db().fetch_one(
                """
                SELECT id, status FROM anti_ai_learned_patterns
                WHERE novel_id = ? AND keyword = ?
                LIMIT 1
                """,
                (str(novel_id), keyword),
            )
            if existing:
                if str(existing.get("status") or "") == "rejected" and not revive_rejected:
                    # 人工驳回过 ⇒ **不复活**（AI 路径）。
                    logger.info(
                        "[%s] 已驳回的模式不再自动复活 keyword=%r", novel_id, keyword
                    )
                    return None
                version = self.current_version(novel_id) + 1
                # 同一个关键词 ⇒ 更新，不新增行。
                # （不能用 ``ON CONFLICT(id)``：id 每次都是新 uuid，冲突永不触发；
                #   表上现在有 ``UNIQUE(novel_id, keyword)`` 唯一索引兜底。）
                self._get_db().execute(
                    """
                    UPDATE anti_ai_learned_patterns
                    SET name = ?, severity = ?, category = ?, reason = ?, hint = ?,
                        status = 'approved', source = ?,
                        rule_version = ?, first_seen_chapter = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(name or ""),
                        str(severity or "info"),
                        str(category or "本书特有"),
                        str(reason or ""),
                        str(hint or ""),
                        str(source or "ai"),
                        version,
                        int(chapter_number or 0),
                        now,
                        existing["id"],
                    ),
                )
                return version

            version = self.current_version(novel_id) + 1
            self._get_db().execute(
                """
                INSERT INTO anti_ai_learned_patterns
                    (id, novel_id, keyword, name, severity, category, reason, hint,
                     status, source, rule_version, first_seen_chapter, hit_count,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'approved', ?, ?, ?, 0, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    str(novel_id),
                    keyword,
                    str(name or ""),
                    str(severity or "info"),
                    str(category or "本书特有"),
                    str(reason or ""),
                    str(hint or ""),
                    str(source or "ai"),
                    version,
                    int(chapter_number or 0),
                    now,
                    now,
                ),
            )
        except Exception as exc:
            logger.warning("写入已学模式失败 novel=%s keyword=%s: %s", novel_id, keyword, exc)
            return None
        return version

    def reject(self, novel_id: str, keyword: str) -> bool:
        """人工驳回一条规则（保留行 ⇒ 版本不回退）。"""
        try:
            self._get_db().execute(
                """
                UPDATE anti_ai_learned_patterns
                SET status = 'rejected', updated_at = ?
                WHERE keyword = ? AND novel_id = ?
                """,
                (datetime.now().isoformat(), str(keyword), str(novel_id)),
            )
        except Exception as exc:
            logger.warning("驳回已学模式失败 novel=%s: %s", novel_id, exc)
            return False
        return True
