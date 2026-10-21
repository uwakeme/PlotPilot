"""Anti-AI 自适应学习服务（F-06：归纳交给 AI，规则库落库并版本化）。

改造前这个模块叫「学习」，做的事却是**代码用固定模板 + 固定触发词做统计**：
6 个结构模板正则（X地Y / 不禁X / 缓缓X / 微微X / 暗自X / 一X就Y）+ 7 个触发词
（仿佛/宛如/不禁/缓缓/微微/暗自/分明）—— 那不是归纳，而且「什么算 AI 味」本身
就是**代码代做的语义判断**。

更糟的是它学到的模式只放在**内存 dict**里（挂在全局单例上），
``get_approved_patterns()`` 又**全仓无调用者** ⇒ 从来没有回灌进扫描器。
结果是：**学了没人看，看了没人用，用了活不过重启。**

现在：

- **归纳由 AI 做**（新 CPMS 节点 ``anti-ai-pattern-induction``）：
  AI 看本章正文与已知命中，指出这本书特有的 AI 味表达；
- **代码只做不越界的校验**：关键词必须是纯字面（无正则元字符 ⇒ 无注入/回溯风险）、
  **必须在本章正文里确实出现**（AI 编的丢弃）、严重度在白名单内；
- **规则库落库 + 版本化**（登记表 F-06 的不变量）⇒ 重启不失；
- **回灌**：已批准的规则由扫描器加载（``AntiAIAuditor.scan_chapter(..., novel_id=...)``）
  ⇒ 归纳出来的模式**真的会被检测到**（下一章起）。

**降级纪律**：AI 不可用 / 输出不合契约 ⇒ **不产生任何模式**（不编），
也不回落成「代码统计出来的模式」—— 那正是要消灭的东西。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: 扫描器格式：(pattern, name, severity, category, hint)
ScannerPattern = Tuple[str, str, str, str, str]

#: 已学模式的替换建议（进扫描器的 hint 位）
_DEFAULT_HINT = "改写为具体动作或对白"


class AntiAILearningService:
    """Anti-AI 自适应学习服务（归纳交 AI，规则库落库）。"""

    def __init__(self, repository: Any = None) -> None:
        self._repository = repository

    # ── 内部：仓储 ────────────────────────────────────────────────

    def _repo(self):
        if self._repository is not None:
            return self._repository
        from infrastructure.persistence.database.sqlite_anti_ai_pattern_repository import (
            AntiAiPatternRepository,
        )

        return AntiAiPatternRepository()

    # ── 归纳 → 校验 → 落库 ───────────────────────────────────────

    async def induce_and_store_patterns(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content: str,
        hits: Optional[Iterable[Any]] = None,
        llm_service: Any = None,
        novel_title: str = "",
    ) -> List[Any]:
        """让 AI 归纳本书特有的 AI 味表达，通过校验的**落库**（approved）。

        返回落库成功的归纳结果；**未评估时返回空列表**（不编模式）。
        """
        from application.audit.services.anti_ai_pattern_induction import (
            AntiAiPatternInducer,
        )
        from application.audit.services.anti_ai_pattern_induction_schema import SOURCE_AI

        if llm_service is None or not novel_id or not content:
            return []

        repo = self._repo()
        try:
            known_keywords = [p[0] for p in repo.list_approved(novel_id)]
        except Exception as exc:
            logger.warning("读取已批准模式失败 novel=%s: %s", novel_id, exc)
            known_keywords = []

        patterns, source = await AntiAiPatternInducer(llm_service).induce(
            chapter_number=chapter_number,
            content=content,
            known_hits=list(hits or []),
            known_keywords=known_keywords,
            novel_title=novel_title,
        )
        if source != SOURCE_AI:
            # 未评估 ⇒ **不产生任何模式**（不编，也不回落成代码统计的模式）
            logger.info(
                "[%s] AI 味模式归纳未评估（ch=%s），本轮不产生新模式", novel_id, chapter_number
            )
            return []

        stored: List[Any] = []
        for pattern in patterns:
            version = repo.upsert_approved(
                novel_id=novel_id,
                keyword=pattern.keyword,
                name=pattern.name or pattern.keyword,
                severity=pattern.severity,
                category=pattern.category,
                reason=pattern.reason,
                hint=_DEFAULT_HINT,
                chapter_number=chapter_number,
                source="ai",
            )
            if version:
                logger.info(
                    "[%s] 归纳出本书特有 AI 味模式 keyword=%r severity=%s 规则库版本=%s",
                    novel_id, pattern.keyword, pattern.severity, version,
                )
                stored.append(pattern)
        return stored

    # ── 读 ────────────────────────────────────────────────────────

    def get_approved_patterns(self, novel_id: str) -> List[ScannerPattern]:
        """该小说已批准的规则（可直接喂给扫描器）。

        旧实现从**内存 dict**读（且全仓无调用者 ⇒ 从不回灌）；现在从**库里**读，
        重启不失。
        """
        if not novel_id:
            return []
        try:
            return self._repo().list_approved(novel_id)
        except Exception as exc:
            logger.warning("读取已批准模式失败 novel=%s: %s", novel_id, exc)
            return []

    def current_rule_version(self, novel_id: str) -> int:
        """规则库当前版本（每次新增 +1；驳回不回退）。"""
        if not novel_id:
            return 0
        try:
            return int(self._repo().current_version(novel_id))
        except Exception as exc:
            logger.warning("读取规则库版本失败 novel=%s: %s", novel_id, exc)
            return 0

    # ── 人工增删 ──────────────────────────────────────────────────

    def approve_pattern(
        self,
        *,
        novel_id: str,
        keyword: str,
        name: str = "",
        severity: str = "info",
        category: str = "本书特有",
        reason: str = "",
    ) -> bool:
        """人工添加/恢复一条规则（落库）。"""
        if not novel_id or not str(keyword or "").strip():
            return False
        version = self._repo().upsert_approved(
            novel_id=novel_id,
            keyword=str(keyword).strip(),
            name=name or str(keyword).strip(),
            severity=severity or "info",
            category=category or "本书特有",
            reason=reason or "人工添加",
            hint=_DEFAULT_HINT,
            source="manual",
        )
        return bool(version)

    def reject_pattern(self, novel_id: str, keyword: str) -> bool:
        """人工驳回一条规则（保留行 ⇒ 版本不回退）。"""
        if not novel_id or not str(keyword or "").strip():
            return False
        try:
            return bool(self._repo().reject(novel_id, str(keyword).strip()))
        except Exception as exc:
            logger.warning("驳回模式失败 novel=%s: %s", novel_id, exc)
            return False


# ── 全局单例 ────────────────────────────────────────────────────────

_service: Optional[AntiAILearningService] = None


def get_anti_ai_learning_service() -> AntiAILearningService:
    """获取全局 Anti-AI 学习服务。"""
    global _service
    if _service is None:
        _service = AntiAILearningService()
    return _service
