"""设定漂移审计（A1）：章后校验正文是否违反 Bible / FactLock 锁定事实。

背景：FactLock 只在生成前把 Bible 事实锁进 T0（prompt 侧约束），生成后
没有「正文是否违反设定」的审计器——设定漂移（角色能力、地名、规则随
章节膨胀）是 AI 长文的头号顽疾。本服务补上验收侧：

- 基准事实：Bible world_settings/locations 聚合 + MemoryEngine FactLock 清单
- 比对：CPMS 节点 setting-drift-audit（LLM 逐条比对，输出漂移分 + 分级 issue）
- 降级：基准为空 → 跳过（audited=False，drift_score=None）；LLM 失败 → 同上（不假装通过）

F-13（长章漂移漏审 / 假通过）：旧实现只送前 12000 字（``_CONTENT_LIMIT``），
长章后半段的漂移永远审不到，却照样返回 ``audited=True`` —— **假通过**。

现在改为**分段送审**：正文按段落边界切成若干段逐段送审，合并各段结论
（漂移分取各段最大，issues 合并）。段数超过 token 护栏时只审前 N 段，并
**显式报告覆盖不全**（``coverage="partial"`` / ``audited=False`` /
``drift_score=None``），不把「没审完」粉饰成「审过了」。
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
# 单段送审的正文长度上限（字符）——长章按此切段，不再截断丢弃后半段
_SEGMENT_CHARS = 6000
# 单章最多送审段数（token 上限护栏）——超出部分不审，并显式报告「未审完」
_MAX_SEGMENTS = 4


def _split_into_segments(text: str, limit: int) -> List[str]:
    """把正文切成不超过 ``limit`` 字的连续片段。

    不变量（守卫测试钉住，缺一不可）：

    - ``"".join(segments) == text`` —— 无缝无丢字；否则「整章都审过了」
      这句话就是假的。
    - 每段长度 ≤ ``limit`` —— 否则单次请求会撑爆 token 预算。
    - 优先在段落空行处切；找不到空行退到单换行；仍不行才按字符硬切。

    切点只落在换行上，避免把一个词劈成两半送审。
    """
    if not text:
        return []
    if limit <= 0 or len(text) <= limit:
        return [text]

    segments: List[str] = []
    start = 0
    total = len(text)
    while start < total:
        if total - start <= limit:
            segments.append(text[start:])
            break
        hard_end = start + limit
        # 先找段落空行，再退到单换行；都找不到就按字符硬切
        cut = text.rfind("\n\n", start, hard_end)
        width = 2
        if cut == -1:
            cut = text.rfind("\n", start, hard_end)
            width = 1
        if cut <= start:
            cut = hard_end
        else:
            cut += width
        segments.append(text[start:cut])
        start = cut
    return segments


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

    def _collect_setting_facts(self, novel_id: str, chapter_number: int = 0) -> List[str]:
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

        fact_locks = self._load_fact_locks(novel_id, chapter_number)
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

    def _load_fact_locks(self, novel_id: str, chapter_number: int = 0) -> List[str]:
        """取 FactLock 基准事实 —— 与生成侧 T0 FACT_LOCK **同源**。

        旧实现用 ``MemoryEngine.__new__`` 绕过 ``__init__`` 去翻会话缓存：
        ``_cache`` 从未初始化（它在 ``__init__`` 里建）⇒ ``getattr(...)`` 得
        None ⇒ **恒返回 []**；而且 ``MemoryState`` 压根没有 ``fact_locks``
        属性（全仓只有这段代码写过这个名字）⇒ 两处都落空。

        于是审计侧的 FactLock 基准**恒空**。更要紧的是口径：生成侧锁进 T0 的
        是 FactLockBuilder 从 Bible 推出的**角色白名单 / 死亡锁 / 关系 /
        身份锁 / 时间线**，而审计侧拿 world_settings / locations 比对 ——
        **两侧基准不同源**，「正文是否违反生成时被锁住的事实」这个问题就问错了。

        现在直接用同一个构造器出清单，不读任何会话缓存。
        """
        if self._bible_repository is None:
            return []
        try:
            from application.engine.services.memory_engine import FactLockBuilder

            builder = FactLockBuilder(self._bible_repository)
            return list(builder.build_facts(novel_id, current_chapter=chapter_number))
        except Exception as e:
            logger.debug("设定漂移审计加载 FactLock 基准失败 novel=%s: %s", novel_id, e)
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

        正文过长时**分段送审**（不再截断丢弃后半段），各段结论合并。

        Returns:
            {audited, drift_score, issues: [...], checked_facts,
             coverage: "full"|"partial"|"none",
             segments_audited, segments_total}

            audited=False 表示未执行**或未审完**（无基准 / LLM 失败 / 覆盖不全），
            调用方不得当作通过。覆盖不全时 drift_score 为 None，已审段落
            查到的问题仍放在 issues 里，供人工复核。
        """
        content = (content or "").strip()
        if not content:
            return self._unaudited("正文为空")

        segments = _split_into_segments(content, _SEGMENT_CHARS)
        total_segments = len(segments)

        facts = self._collect_setting_facts(novel_id, chapter_number)
        if not facts:
            return self._unaudited(
                "无基准事实（Bible/FactLock 为空）", total_segments=total_segments
            )

        setting_facts_text = "\n".join(f"- {f}" for f in facts)
        targets = segments[:_MAX_SEGMENTS]

        results: List[Dict[str, Any]] = []
        for index, segment in enumerate(targets, start=1):
            variables = {
                "novel_id": novel_id,
                "novel_title": novel_title or novel_id,
                "chapter_number": int(chapter_number),
                "setting_facts": setting_facts_text,
                "chapter_content": segment,
                # 让模型知道自己看的是第几段、整章共几段，
                # 否则它会把节选当成整章，替没看到的后半段下结论
                "segment_index": index,
                "segment_total": total_segments,
            }
            try:
                raw = await self._invoke(variables)
            except Exception as e:
                logger.warning(
                    "设定漂移审计调用失败 novel=%s ch=%s seg=%s/%s: %s",
                    novel_id, chapter_number, index, total_segments, e,
                )
                results.append(self._unaudited(f"LLM 调用失败（{type(e).__name__}）"))
                continue
            results.append(self._parse_response(raw, total_facts=len(facts)))

        return self._merge_segments(results, total_segments=total_segments)

    def _merge_segments(
        self,
        results: List[Dict[str, Any]],
        *,
        total_segments: int,
    ) -> Dict[str, Any]:
        """合并各段审计结论。

        - drift_score：取各段**最大**（任一段漂移即本章漂移）
        - issues：各段合并
        - coverage：全部段落都审完才是 "full"；
          段数超上限被跳过、或某段失败 ⇒ "partial"
        - 覆盖不全 ⇒ audited=False + drift_score=None（**不报假通过**），
          但已审段落查到的问题仍然返回，供人工复核
        """
        audited = [r for r in results if r.get("audited")]
        failed = [r for r in results if not r.get("audited")]

        issues: List[Dict[str, Any]] = []
        for r in audited:
            issues.extend(r.get("issues") or [])

        score = 0
        checked = 0
        for r in audited:
            try:
                score = max(score, int(r.get("drift_score") or 0))
            except (TypeError, ValueError):
                pass
            try:
                checked = max(checked, int(r.get("checked_facts") or 0))
            except (TypeError, ValueError):
                pass

        skipped = max(0, total_segments - len(results))
        segments_audited = len(audited)
        full = (not failed) and skipped == 0 and segments_audited == total_segments
        if full:
            coverage = "full"
        elif segments_audited:
            coverage = "partial"
        else:
            # 一段都没审成：不是「审了一部分」，是「没审」
            coverage = "none"

        payload: Dict[str, Any] = {
            "issues": issues,
            "checked_facts": checked,
            "coverage": coverage,
            "segments_audited": segments_audited,
            "segments_total": total_segments,
        }
        if full:
            payload["audited"] = True
            payload["drift_score"] = score
            return payload

        reasons: List[str] = []
        if skipped:
            reasons.append(f"章节过长，仅送审前 {len(results)}/{total_segments} 段")
        for r in failed:
            reasons.append(str(r.get("reason") or "段落审计失败"))
        return {
            "audited": False,
            "reason": "；".join(reasons) or "审计覆盖不全",
            "drift_score": None,
            **payload,
        }

    async def _invoke(self, variables: Dict[str, Any]) -> str:
        from application.ai_invocation.autopilot.factory import get_or_create_autopilot_helper_invoker
        from application.ai_invocation.autopilot.helper_invoker import AutopilotHelperRequest
        from infrastructure.ai.prompt_keys import SETTING_DRIFT_AUDIT

        owner = type("SettingDriftAuditOwner", (), {"llm_service": self._llm_service})()
        return await get_or_create_autopilot_helper_invoker(owner).invoke_text(
            AutopilotHelperRequest(
                novel_id=str(variables.get("novel_id") or variables.get("novel_title") or "global"),
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
    def _unaudited(reason: str, total_segments: int = 0) -> Dict[str, Any]:
        return {
            "audited": False,
            "reason": reason,
            "drift_score": None,
            "issues": [],
            "checked_facts": 0,
            "coverage": "none",
            "segments_audited": 0,
            "segments_total": total_segments,
        }
