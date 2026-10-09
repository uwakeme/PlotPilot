"""设定漂移审计（A1）：章后校验正文是否违反 Bible / FactLock 锁定事实。

背景：FactLock 只在生成前把 Bible 事实锁进 T0（prompt 侧约束），生成后
没有「正文是否违反设定」的审计器——设定漂移（角色能力、地名、规则随
章节膨胀）是 AI 长文的头号顽疾。本服务补上验收侧：

- 基准事实：Bible world_settings/locations 聚合 + MemoryEngine FactLock 清单
- 比对：CPMS 节点 setting-drift-audit（LLM 逐条比对，输出漂移分 + 分级 issue）
- 降级：基准为空 → 跳过（drift_score 0）；LLM 失败 → audited=False（不假装通过）
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 送入 LLM 的基准事实规模上限（条）——防长篇 Bible 全量撑爆 prompt
_MAX_FACTS = 120
# 单条事实文本截断（字符）
_FACT_TEXT_LIMIT = 200
# 送审正文截断（字符）——漂移高发区在正文细节，取前 12K 足够覆盖单章
_CONTENT_LIMIT = 12_000


class SettingDriftAuditService:
    """设定漂移审计服务（章后调用，结果并入审计快照）。"""

    def __init__(
        self,
        llm_service: Optional[Any] = None,
        bible_repository: Optional[Any] = None,
    ) -> None:
        self._llm_service = llm_service
        self._bible_repository = bible_repository

    # ── 基准事实收集 ──────────────────────────────────────────────

    def _collect_setting_facts(self, novel_id: str) -> List[str]:
        """收集基准事实清单（Bible 聚合 + FactLock）。"""
        facts: List[str] = []

        bible = self._load_bible(novel_id)
        if bible is not None:
            for setting in getattr(bible, "world_settings", []) or []:
                text = f"[世界观/{getattr(setting, 'category', '通用')}] {getattr(setting, 'content', '') or ''}"
                facts.append(text[:_FACT_TEXT_LIMIT])
            for loc in getattr(bible, "locations", []) or []:
                name = getattr(loc, "name", "")
                desc = getattr(loc, "description", "")
                loc_type = getattr(loc, "location_type", "") or getattr(loc, "type", "")
                facts.append(f"[地点/{loc_type}] {name}：{desc}"[:_FACT_TEXT_LIMIT])

        fact_locks = self._load_fact_locks(novel_id)
        for lock in fact_locks:
            facts.append(f"[FactLock] {str(lock)}"[:_FACT_TEXT_LIMIT])

        # 去重保序 + 上限
        seen = set()
        unique: List[str] = []
        for f in facts:
            key = f.strip()
            if key and key not in seen:
                seen.add(key)
                unique.append(f)
        return unique[:_MAX_FACTS]

    def _load_bible(self, novel_id: str) -> Optional[Any]:
        if self._bible_repository is None:
            return None
        try:
            from domain.novel.value_objects.novel_id import NovelId

            return self._bible_repository.get_by_novel_id(NovelId(novel_id))
        except Exception as e:
            logger.debug("设定漂移审计加载 Bible 失败 novel=%s: %s", novel_id, e)
            return None

    def _load_fact_locks(self, novel_id: str) -> List[Any]:
        """从 MemoryEngine 的会话缓存取 FactLock 清单（只读，不初始化引擎）。"""
        try:
            from application.engine.services.memory_engine import MemoryEngine

            engine = MemoryEngine.__new__(MemoryEngine)
            cache = getattr(engine, "_cache", None)
            if not isinstance(cache, dict):
                return []
            state = cache.get(novel_id)
            locks = getattr(state, "fact_locks", None)
            return list(locks or [])
        except Exception:
            return []

    # ── 审计主流程 ────────────────────────────────────────────────

    async def audit_chapter(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        novel_title: str = "",
    ) -> Dict[str, Any]:
        """审计单章设定漂移。

        Returns:
            {audited, drift_score, issues: [...], checked_facts}
            audited=False 表示未执行（无基准/LLM 失败），调用方不得当作通过。
        """
        content = (content or "").strip()
        if not content:
            return self._unaudited("正文为空")

        facts = self._collect_setting_facts(novel_id)
        if not facts:
            return self._unaudited("无基准事实（Bible/FactLock 为空）")

        setting_facts_text = "\n".join(f"- {f}" for f in facts)
        variables = {
            "novel_title": novel_title or novel_id,
            "chapter_number": int(chapter_number),
            "setting_facts": setting_facts_text,
            "chapter_content": content[:_CONTENT_LIMIT],
        }

        try:
            raw = await self._invoke(variables)
        except Exception as e:
            logger.warning("设定漂移审计调用失败 novel=%s ch=%s: %s", novel_id, chapter_number, e)
            return self._unaudited(f"LLM 调用失败（{type(e).__name__}）")

        return self._parse_response(raw, total_facts=len(facts))

    async def _invoke(self, variables: Dict[str, Any]) -> str:
        from application.ai_invocation.autopilot.factory import get_or_create_autopilot_helper_invoker
        from application.ai_invocation.autopilot.helper_invoker import AutopilotHelperRequest
        from infrastructure.ai.prompt_keys import SETTING_DRIFT_AUDIT

        owner = type("SettingDriftAuditOwner", (), {"llm_service": self._llm_service})()
        return await get_or_create_autopilot_helper_invoker(owner).invoke_text(
            AutopilotHelperRequest(
                novel_id=str(variables.get("novel_title") or "global"),
                stage="audit",
                operation="audit.setting.drift",
                node_key=SETTING_DRIFT_AUDIT,
                explicit_variables=variables,
                context={
                    "novel_title": variables.get("novel_title"),
                    "chapter_number": variables.get("chapter_number"),
                },
                metadata={"source": "setting_drift_audit"},
                config={"max_tokens": 2048, "temperature": 0.2},
            )
        )

    def _parse_response(self, raw: str, total_facts: int) -> Dict[str, Any]:
        import re

        text = (raw or "").strip()
        # 剥 markdown 代码围栏
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fence:
            text = fence.group(1)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("设定漂移审计输出无法解析: %r", text[:120])
            return self._unaudited("LLM 输出无法解析")

        issues_raw = data.get("issues") or []
        issues = []
        for item in issues_raw:
            if not isinstance(item, dict):
                continue
            issues.append({
                "severity": str(item.get("severity") or "info").lower(),
                "category": str(item.get("category") or "fact"),
                "description": str(item.get("description") or "")[:400],
                "suggestion": str(item.get("suggestion") or "")[:200],
            })

        try:
            score = max(0, min(100, int(data.get("drift_score") or 0)))
        except (TypeError, ValueError):
            score = 0

        return {
            "audited": True,
            "drift_score": score,
            "issues": issues,
            "checked_facts": int(data.get("checked_facts") or total_facts),
        }

    @staticmethod
    def _unaudited(reason: str) -> Dict[str, Any]:
        return {
            "audited": False,
            "reason": reason,
            "drift_score": None,
            "issues": [],
            "checked_facts": 0,
        }
