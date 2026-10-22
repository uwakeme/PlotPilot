"""宏观诊断服务 - 自动扫描 + 结果存储"""
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from uuid import uuid4

from domain.novel.value_objects.novel_id import NovelId
from application.audit.dtos.macro_refactor_dto import (
    SOURCE_AI,
    LogicBreakpoint,
)
from application.audit.services.macro_breakpoint_schema import (
    VERDICT_TO_BREAKPOINT_TYPE,
)
from application.audit.services.macro_refactor_scanner import MacroRefactorScanner
from infrastructure.persistence.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

# 约 6 万字触发一次（5~10 万字取中值，与全托管锚点配合）
MACRO_DIAGNOSIS_WORD_INTERVAL = 60_000


# H-06：这里曾有一个 ``build_silent_context_patch``，用**代码写死的三套模板句**
# 生成「系统叙事校准」短指令并静默注入生成 Context 头部 ——
# 「后续生成须强化「X」所要求的行为倾向」「保持情绪张力，不要强行拉回原人设」
# 「应延续情绪强度，不要立即恢复冷静」。
# 那三句是**代码替编辑决定接下来怎么写**。现已改为由 LLM 写
# （``application/audit/services/macro_calibration.py``）：
# 校准指令只装 LLM 的产出；LLM 没给 / 调用失败 ⇒ 不注入任何东西。


class MacroDiagnosisResult:
    """宏观诊断结果"""
    
    def __init__(
        self,
        id: str,
        novel_id: str,
        trigger_reason: str,
        trait: str,
        conflict_tags: Optional[List[str]],
        breakpoints: List[LogicBreakpoint],
        status: str = "completed",
        error_message: Optional[str] = None,
        created_at: Optional[datetime] = None
    ):
        self.id = id
        self.novel_id = novel_id
        self.trigger_reason = trigger_reason
        self.trait = trait
        self.conflict_tags = conflict_tags or []
        self.breakpoints = breakpoints
        self.status = status
        self.error_message = error_message
        self.created_at = created_at or datetime.now(timezone.utc)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "novel_id": self.novel_id,
            "trigger_reason": self.trigger_reason,
            "trait": self.trait,
            "conflict_tags": self.conflict_tags,
            "breakpoints": [
                {
                    "event_id": bp.event_id,
                    "chapter": bp.chapter,
                    "reason": bp.reason,
                    "tags": bp.tags,
                    # H-06：断点类型与**它的来源**（ai / rule / 未评估）
                    "breakpoint_type": bp.breakpoint_type.value,
                    "breakpoint_source": bp.breakpoint_source,
                }
                for bp in self.breakpoints
            ],
            "breakpoint_count": len(self.breakpoints),
            "status": self.status,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


class MacroDiagnosisService:
    """宏观诊断服务 - 自动扫描 + 结果存储
    
    职责：
    1. 执行全人设扫描（使用内置规则）
    2. 存储诊断结果到数据库
    3. 提供查询接口获取最新结果
    """
    
    # 默认扫描的人设标签（从内置规则中选取）
    DEFAULT_SCAN_TRAITS = ["冷酷", "理性", "谨慎", "温和"]
    
    def __init__(
        self,
        db: DatabaseConnection,
        scanner: MacroRefactorScanner,
        llm_service: Any = None,
        bible_repository: Any = None,
    ):
        self.db = db
        self.scanner = scanner
        # H-06：叙事校准指令由 LLM 写（None ⇒ 不生成校准 ⇒ 不注入）。
        self.llm_service = llm_service
        # H-06 子项 A：AI 语义诊断需要**本书角色**（人设描述）。
        # 只有 character_states 是不够的 —— 那张表只有带伤疤/执念的角色才有行，
        # 角色表为空 ⇒ 分类通道等于白做（X-03 的教训）。
        self.bible_repository = bible_repository
    
    def run_full_diagnosis(
        self,
        novel_id: str,
        trigger_reason: str,
        traits: Optional[List[str]] = None,
        total_words_at_run: int = 0,
    ) -> MacroDiagnosisResult:
        """扫描 + 落库。**不含 AI 校准** ⇒ context_patch 为空（不注入任何东西）。

        生产路径请用 :meth:`run_full_diagnosis_with_calibration`（async）——
        那里才有 LLM 写的叙事校准指令。
        """
        result = self._scan_full(novel_id, trigger_reason, traits)
        self._save_result(result, total_words_at_run=total_words_at_run)
        return result

    async def run_full_diagnosis_with_calibration(
        self,
        novel_id: str,
        trigger_reason: str,
        traits: Optional[List[str]] = None,
        total_words_at_run: int = 0,
    ) -> MacroDiagnosisResult:
        """扫描 → AI 语义分类 → AI 写叙事校准指令 → 落库（生产路径，H-06）。

        断点分类由 **AI** 给（``macro_breakpoint_classifier``）：

        - AI 判过了 ⇒ 断点类型用 AI 的判定（真崩塌 / 人物突破 / 应激反应）；
          AI 说「不偏离」或「判不出来」的事件**不是**断点；
        - AI 不可用 ⇒ 回落**代码规则判定**（改造前的行为），
          且来源可分辨（``breakpoint_source``）；
        - 规则**判不出**的（缺仓储 / 提不出角色 / 没找到触发原因）
          一律落 ``UNASSESSED`` —— 「判不出」不说成「崩塌」。

        校准指令**只**来自 LLM：没给 / 调用失败 / 输出不合契约 ⇒ 落空
        ⇒ 生成上下文不注入任何东西。**代码不代写校准句。**
        """
        result = self._scan_full(novel_id, trigger_reason, traits)
        await self._classify_breakpoints_with_ai(result, novel_id)
        calibration = ""
        if result.breakpoints:
            from application.audit.services.macro_calibration import (
                build_macro_calibration,
            )

            calibration = await build_macro_calibration(
                self.llm_service,
                trait=result.trait,
                breakpoints=result.breakpoints,
            )
        self._save_result(
            result, total_words_at_run=total_words_at_run, calibration=calibration
        )
        return result

    async def _classify_breakpoints_with_ai(
        self, result: MacroDiagnosisResult, novel_id: str
    ) -> None:
        """用 AI 的语义判定**覆盖**断点分类（就地改写 ``result.breakpoints``）。

        AI 不可用 / 输出不合契约 ⇒ 什么都不做 ⇒ 沿用规则判定（来源可分辨）。
        """
        if self.llm_service is None:
            return
        try:
            characters = self._character_profiles_for_ai(novel_id)
            events = self.scanner.list_candidate_events(novel_id)
        except Exception as exc:
            logger.warning("准备宏观断点分类输入失败，沿用规则判定: %s", exc)
            return
        if not events:
            return
        if not characters:
            # H-06 子项 A：没有角色档案 ⇒ 这个判断做不了
            # （问题本身就是「是不是偏离了**角色的人设**」），
            # 而且角色白名单为空会让 AI 的归属信息**静默**丢失
            # ⇒ 判为未评估，回落规则判定（来源可分辨）。
            logger.info(
                "宏观断点分类未评估：没有角色档案，沿用规则判定"
            )
            return

        from application.audit.services.macro_breakpoint_classifier import (
            MacroBreakpointClassifier,
        )

        classification = await MacroBreakpointClassifier(
            self.llm_service
        ).classify(characters=characters, events=events)
        if not classification.is_usable:
            logger.info(
                "宏观断点分类未评估（来源：%s），沿用规则判定",
                classification.source or "unassessed",
            )
            return
        self._apply_ai_classification(result, classification, events)

    def _character_profiles_for_ai(self, novel_id: str) -> List[Dict]:
        """角色档案 = Bible 角色（人设）+ 人物状态（伤疤 / 执念 / 当前状态）。

        缺 Bible 仓储时**只**给人物状态（可能为空）⇒ AI 看不到角色，
        这时要如实反映：分类通道拿不到角色就让 AI 判不了（⇒ 未评估），
        而不是拿空角色表去凑一个「看起来判过了」的结论。
        """
        profiles: Dict[str, Dict[str, Any]] = {}

        if self.bible_repository is not None:
            try:
                bible = self.bible_repository.get_by_novel_id(NovelId(novel_id))
            except Exception as exc:
                logger.warning("读取 Bible 失败，AI 诊断将缺少角色人设: %s", exc)
                bible = None
            for char in (getattr(bible, "characters", None) or []):
                raw_id = getattr(char, "character_id", None)
                cid = str(getattr(raw_id, "value", raw_id) or "")
                if not cid:
                    continue
                profiles[cid] = {
                    "character_id": cid,
                    "name": str(getattr(char, "name", "") or ""),
                    "personality": str(getattr(char, "personality", "") or ""),
                    "description": str(getattr(char, "description", "") or ""),
                    "core_belief": str(getattr(char, "core_belief", "") or ""),
                    "traits": [],
                    "scars": [],
                    "motivations": [],
                    "state_summary": "",
                }

        for state_profile in self.scanner.list_character_profiles(novel_id):
            cid = state_profile["character_id"]
            target = profiles.setdefault(
                cid,
                {
                    "character_id": cid,
                    "name": "",
                    "personality": "",
                    "description": "",
                    "core_belief": "",
                    "traits": [],
                    "scars": [],
                    "motivations": [],
                    "state_summary": "",
                },
            )
            target["traits"] = state_profile["traits"]
            target["scars"] = state_profile["scars"]
            target["motivations"] = state_profile["motivations"]
            target["state_summary"] = state_profile["state_summary"]

        return list(profiles.values())

    @staticmethod
    def _apply_ai_classification(
        result: MacroDiagnosisResult,
        classification,
        events: List[Dict],
    ) -> None:
        """把 AI 的判定写进断点。

        合并规则：

        1. AI **判过**的事件 ⇒ 以 AI 为准（判成偏离就改类型；
           判成「不偏离 / 判不出来」就**不是**断点，丢掉）；
        2. AI **没看到**的事件（候选窗口之外）⇒ 保留规则判定；
        3. AI 判出来、规则没找到的偏离 ⇒ 补一条新断点。
        """
        judged = classification.verdicts
        by_event = {bp.event_id: bp for bp in result.breakpoints}
        events_by_id = {str(e.get("event_id") or ""): e for e in events}
        merged: List[LogicBreakpoint] = []

        for bp in result.breakpoints:
            verdict = judged.get(bp.event_id)
            if verdict is None:
                # AI 没看到这条（候选窗口之外）⇒ 保留规则判定
                merged.append(bp)
                continue
            if not verdict.is_deviation:
                # AI 判过了，且说不是偏离 ⇒ 不是断点
                continue
            bp.breakpoint_type = VERDICT_TO_BREAKPOINT_TYPE[verdict.verdict]
            bp.breakpoint_source = SOURCE_AI
            merged.append(bp)

        for event_id, verdict in judged.items():
            if not verdict.is_deviation or event_id in by_event:
                continue
            event = events_by_id.get(event_id) or {}
            merged.append(
                LogicBreakpoint(
                    event_id=event_id,
                    chapter=int(event.get("chapter") or 0),
                    reason=verdict.reason,
                    tags=list(event.get("tags") or []),
                    breakpoint_type=VERDICT_TO_BREAKPOINT_TYPE[verdict.verdict],
                    breakout_reason=verdict.reason,
                    character_id=verdict.character_id or None,
                    event_summary=str(event.get("summary") or ""),
                    breakpoint_source=SOURCE_AI,
                )
            )

        result.breakpoints = merged
        logger.info(
            "宏观断点已由 AI 分类：判定 %s 条，保留断点 %s 条",
            len(judged),
            len(merged),
        )

    def _scan_full(
        self,
        novel_id: str,
        trigger_reason: str,
        traits: Optional[List[str]] = None,
    ) -> MacroDiagnosisResult:
        """扫描（**不落库**）。失败时返回 status="failed" 的结果。"""
        diagnosis_id = str(uuid4())
        scan_traits = traits or self.DEFAULT_SCAN_TRAITS
        all_breakpoints: List[LogicBreakpoint] = []

        try:
            # 对每个人设标签执行扫描
            # H-06 子项 A：同一条事件可能同时撞上**多个人设标签**
            # （「行为:鲁莽」既在「冷酷」表里也在「谨慎」表里；
            #  「情绪:激动」同时在「冷酷」「理性」「温和」三张表里）
            # ⇒ 旧实现会把同一事件重复报 2~3 次，breakpoint_count 虚高，
            # 而且 AI 的逐事件判定对不上。断点是**按事件**的 ⇒ 只留第一条。
            seen_event_ids: set = set()
            for trait in scan_traits:
                breakpoints = self.scanner.scan_breakpoints(
                    novel_id=novel_id,
                    trait=trait,
                    conflict_tags=None  # 使用内置规则
                )
                for bp in breakpoints:
                    if bp.event_id in seen_event_ids:
                        continue
                    seen_event_ids.add(bp.event_id)
                    all_breakpoints.append(bp)

            # 创建结果对象
            result = MacroDiagnosisResult(
                id=diagnosis_id,
                novel_id=novel_id,
                trigger_reason=trigger_reason,
                trait=",".join(scan_traits),  # 多标签合并
                conflict_tags=[],
                breakpoints=all_breakpoints,
                status="completed"
            )

            logger.info(
                f"[MacroDiagnosis] 完成诊断 novel={novel_id} "
                f"trigger={trigger_reason} traits={scan_traits} "
                f"breakpoints={len(all_breakpoints)}"
            )

            return result

        except Exception as e:
            logger.error(f"[MacroDiagnosis] 诊断失败 novel={novel_id}: {e}", exc_info=True)

            # 失败结果（不落库，由调用方决定）
            return MacroDiagnosisResult(
                id=diagnosis_id,
                novel_id=novel_id,
                trigger_reason=trigger_reason,
                trait=",".join(scan_traits),
                conflict_tags=[],
                breakpoints=[],
                status="failed",
                error_message=str(e)
            )
    def run_single_trait_diagnosis(
        self,
        novel_id: str,
        trait: str,
        conflict_tags: Optional[List[str]] = None,
        trigger_reason: str = "manual"
    ) -> MacroDiagnosisResult:
        """执行单人设诊断
        
        Args:
            novel_id: 小说 ID
            trait: 目标人设标签
            conflict_tags: 自定义冲突标签（可选）
            trigger_reason: 触发原因
        
        Returns:
            MacroDiagnosisResult: 诊断结果
        """
        diagnosis_id = str(uuid4())
        
        try:
            breakpoints = self.scanner.scan_breakpoints(
                novel_id=novel_id,
                trait=trait,
                conflict_tags=conflict_tags
            )
            
            result = MacroDiagnosisResult(
                id=diagnosis_id,
                novel_id=novel_id,
                trigger_reason=trigger_reason,
                trait=trait,
                conflict_tags=conflict_tags or [],
                breakpoints=breakpoints,
                status="completed"
            )
            
            self._save_result(result, total_words_at_run=0)
            
            logger.info(
                f"[MacroDiagnosis] 完成单人设诊断 novel={novel_id} "
                f"trait={trait} breakpoints={len(breakpoints)}"
            )
            
            return result
            
        except Exception as e:
            logger.error(f"[MacroDiagnosis] 单人设诊断失败 novel={novel_id}: {e}", exc_info=True)
            
            result = MacroDiagnosisResult(
                id=diagnosis_id,
                novel_id=novel_id,
                trigger_reason=trigger_reason,
                trait=trait,
                conflict_tags=conflict_tags or [],
                breakpoints=[],
                status="failed",
                error_message=str(e)
            )
            self._save_result(result, total_words_at_run=0)
            
            return result
    
    def get_latest_result(self, novel_id: str) -> Optional[Dict[str, Any]]:
        """获取最新的诊断结果
        
        Args:
            novel_id: 小说 ID
        
        Returns:
            最新诊断结果字典，无结果返回 None
        """
        sql = """
            SELECT id, novel_id, trigger_reason, trait, conflict_tags,
                   breakpoints, breakpoint_count, status, resolved, resolved_at, resolved_by,
                   injection_decision, decided_at, decided_by,
                   context_patch, error_message, created_at
            FROM macro_diagnosis_results
            WHERE novel_id = ?
            ORDER BY created_at DESC
            LIMIT 1
        """
        # ⚠ **老库可能没有新列**（第 3 批复审 S14）：
        #   迁移 runner 只 log error、**不阻断启动** ⇒ 迁移失败时服务照样起来，
        #   而这条 SELECT 会直接炸（no such column）⇒ 诊断 API 500。
        #   注入侧有「降级返回空串」的兜底，读取侧此前**没有** ⇒ 这里补上。
        #   降级语义：新列当「未决定（pending）」，绝不假装已批准。
        legacy_sql = """
            SELECT id, novel_id, trigger_reason, trait, conflict_tags,
                   breakpoints, breakpoint_count, status, resolved, resolved_at, resolved_by,
                   context_patch, error_message, created_at
            FROM macro_diagnosis_results
            WHERE novel_id = ?
            ORDER BY created_at DESC
            LIMIT 1
        """
        try:
            row = self.db.fetch_one(sql, (novel_id,))
            missing_columns = False
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            if "no such column" not in message:
                raise
            logger.warning(
                "macro_diagnosis_results 缺少新列（迁移未生效？），按未决定降级: %s", exc
            )
            row = self.db.fetch_one(legacy_sql, (novel_id,))
            missing_columns = True

        if not row:
            return None

        return {
            "id": row["id"],
            "novel_id": row["novel_id"],
            "trigger_reason": row["trigger_reason"],
            "trait": row["trait"],
            "conflict_tags": json.loads(row["conflict_tags"]) if row["conflict_tags"] else [],
            "breakpoints": json.loads(row["breakpoints"]) if row["breakpoints"] else [],
            "breakpoint_count": row["breakpoint_count"],
            "status": row["status"],
            "resolved": bool(row["resolved"]),
            "resolved_at": row["resolved_at"],
            "resolved_by": row["resolved_by"],
            # H-06 子项 B：注入决定（pending / approved / rejected）
            # ⚠ 缺列时一律「未决定」—— 绝不默认成「已批准」。
            "injection_decision": (
                "pending" if missing_columns else (row.get("injection_decision") or "pending")
            ),
            "decided_at": None if missing_columns else row.get("decided_at"),
            "decided_by": None if missing_columns else row.get("decided_by"),
            # 校准文本本身要露给作者（他得先看见才谈得上批准）
            "context_patch": row["context_patch"] or "",
            "error_message": row["error_message"],
            "created_at": row["created_at"]
        }
    
    def list_results(
        self,
        novel_id: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """获取诊断历史列表
        
        Args:
            novel_id: 小说 ID
            limit: 最大返回数量
        
        Returns:
            诊断结果列表
        """
        sql = """
            SELECT id, novel_id, trigger_reason, trait, conflict_tags,
                   breakpoints, breakpoint_count, status, resolved, resolved_at, resolved_by,
                   injection_decision, decided_at, decided_by,
                   context_patch, error_message, created_at
            FROM macro_diagnosis_results
            WHERE novel_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """
        rows = self.db.fetch_all(sql, (novel_id, limit))
        
        return [
            {
                "id": row["id"],
                "novel_id": row["novel_id"],
                "trigger_reason": row["trigger_reason"],
                "trait": row["trait"],
                "conflict_tags": json.loads(row["conflict_tags"]) if row["conflict_tags"] else [],
                "breakpoints": json.loads(row["breakpoints"]) if row["breakpoints"] else [],
                "breakpoint_count": row["breakpoint_count"],
                "status": row["status"],
                "resolved": bool(row["resolved"]),
                "resolved_at": row["resolved_at"],
                "resolved_by": row["resolved_by"],
                # H-06 子项 B：注入决定（pending / approved / rejected）
                "injection_decision": row["injection_decision"],
                "decided_at": row["decided_at"],
                "decided_by": row["decided_by"],
                # 校准文本本身要露给作者（他得先看见才谈得上批准）
                "context_patch": row["context_patch"] or "",
                "error_message": row["error_message"],
                "created_at": row["created_at"]
            }
            for row in rows
        ]
    
    def mark_resolved(
        self,
        novel_id: str,
        diagnosis_id: str,
        resolved_by: str = "manual"
    ) -> bool:
        """标记诊断结果为已解决
        
        已解决的诊断结果不会再注入到提示词中。
        
        Args:
            novel_id: 小说 ID
            diagnosis_id: 诊断结果 ID
            resolved_by: 解决方式（'manual' 或 'auto'）
        
        Returns:
            是否成功标记
        """
        try:
            now = datetime.now(timezone.utc).isoformat()
            sql = """
                UPDATE macro_diagnosis_results
                SET resolved = 1, resolved_at = ?, resolved_by = ?
                WHERE id = ? AND novel_id = ?
            """
            self.db.execute(sql, (now, resolved_by, diagnosis_id, novel_id))
            self.db.get_connection().commit()
            
            logger.info(f"[MacroDiagnosis] 标记已解决: diagnosis_id={diagnosis_id}")
            return True
            
        except Exception as e:
            logger.error(f"[MacroDiagnosis] 标记已解决失败: {e}")
            return False
    
    def record_injection_decision(
        self,
        novel_id: str,
        diagnosis_id: str,
        decision: str,
        decided_by: str = "manual"
    ) -> bool:
        """记录一次「注入决定」（H-06 子项 B）。

        合法决定只有两个：``approved``（批准 ⇒ 校准指令进生成上下文）与
        ``rejected``（否决 ⇒ 不注入）。其它值一律**拒收**（不改库）。

        ⚠ 与 ``mark_resolved`` 的方向**相反**，两者不可互相替代：
        ``resolved=1`` = 「作者已处理完这条 ⇒ 停注入」；
        ``approved`` = 「作者认可这条校准 ⇒ 注入」。
        """
        from application.audit.services.macro_diagnosis_injection import (
            is_valid_decision,
        )

        if not is_valid_decision(decision):
            logger.warning(
                "[MacroDiagnosis] 非法注入决定被拒收: diagnosis_id=%s decision=%r",
                diagnosis_id,
                decision,
            )
            return False

        try:
            now = datetime.now(timezone.utc).isoformat()
            sql = """
                UPDATE macro_diagnosis_results
                SET injection_decision = ?, decided_at = ?, decided_by = ?
                WHERE id = ? AND novel_id = ?
            """
            self.db.execute(
                sql,
                (decision, now, decided_by, diagnosis_id, novel_id),
            )
            self.db.get_connection().commit()

            logger.info(
                "[MacroDiagnosis] 记录注入决定: diagnosis_id=%s decision=%s by=%s",
                diagnosis_id,
                decision,
                decided_by,
            )
            return True

        except Exception as e:
            logger.error(f"[MacroDiagnosis] 记录注入决定失败: {e}")
            return False

    def list_pending_decisions(self, novel_id: str) -> List[Dict[str, Any]]:
        """列出**待确认**的诊断结果（注入决定 = pending 且未解决）。

        这是「注入经提案确认」的用户入口：作者在这里看到诊断结论与
        校准文本，然后批准或否决。
        """
        sql = """
            SELECT id, novel_id, trigger_reason, trait, conflict_tags,
                   breakpoints, breakpoint_count, status, resolved, resolved_at, resolved_by,
                   injection_decision, decided_at, decided_by,
                   context_patch, error_message, created_at
            FROM macro_diagnosis_results
            WHERE novel_id = ? AND status = 'completed' AND resolved = 0
              AND injection_decision = 'pending'
            ORDER BY created_at DESC
        """
        rows = self.db.fetch_all(sql, (novel_id,))
        return [
            {
                "id": row["id"],
                "novel_id": row["novel_id"],
                "trigger_reason": row["trigger_reason"],
                "trait": row["trait"],
                "conflict_tags": json.loads(row["conflict_tags"]) if row["conflict_tags"] else [],
                "breakpoints": json.loads(row["breakpoints"]) if row["breakpoints"] else [],
                "breakpoint_count": row["breakpoint_count"],
                "status": row["status"],
                "resolved": bool(row["resolved"]),
                "resolved_at": row["resolved_at"],
                "resolved_by": row["resolved_by"],
                "injection_decision": row["injection_decision"],
                "decided_at": row["decided_at"],
                "decided_by": row["decided_by"],
                "context_patch": row["context_patch"] or "",
                "error_message": row["error_message"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    def get_latest_unresolved_result(self, novel_id: str) -> Optional[Dict[str, Any]]:
        """获取最新的未解决诊断结果
        
        用于提示词注入，只返回未解决的诊断结果。
        
        Args:
            novel_id: 小说 ID
        
        Returns:
            最新未解决诊断结果，无结果返回 None
        """
        sql = """
            SELECT id, novel_id, trigger_reason, trait, conflict_tags,
                   breakpoints, breakpoint_count, status, resolved, resolved_at, resolved_by,
                   error_message, created_at
            FROM macro_diagnosis_results
            WHERE novel_id = ? AND resolved = 0 AND status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
        """
        row = self.db.fetch_one(sql, (novel_id,))
        
        if not row:
            return None
        
        return {
            "id": row["id"],
            "novel_id": row["novel_id"],
            "trigger_reason": row["trigger_reason"],
            "trait": row["trait"],
            "conflict_tags": json.loads(row["conflict_tags"]) if row["conflict_tags"] else [],
            "breakpoints": json.loads(row["breakpoints"]) if row["breakpoints"] else [],
            "breakpoint_count": row["breakpoint_count"],
            "status": row["status"],
            "resolved": bool(row["resolved"]),
            "resolved_at": row["resolved_at"],
            "resolved_by": row["resolved_by"],
            "error_message": row["error_message"],
            "created_at": row["created_at"]
        }
    
    def _save_result(
        self,
        result: MacroDiagnosisResult,
        total_words_at_run: int = 0,
        calibration: str = "",
    ) -> None:
        """保存诊断结果到数据库（含静默注入用 context_patch）。

        context_patch **只装 LLM 写的校准指令**（H-06）。调用方没给
        （没跑 LLM / LLM 没答 / 调用失败）⇒ 存空 ⇒ 生成上下文不注入任何东西。
        """
        # ★ V2: 保存完整的断点信息（含 breakpoint_type, scar_id, breakout_reason）
        breakpoints_json = json.dumps(
            [bp.to_dict() for bp in result.breakpoints],
            ensure_ascii=False
        )
        conflict_tags_json = json.dumps(result.conflict_tags, ensure_ascii=False)
        context_patch = str(calibration or "").strip()
        
        sql = """
            INSERT INTO macro_diagnosis_results
            (id, novel_id, trigger_reason, trait, conflict_tags, breakpoints, breakpoint_count,
             status, error_message, context_patch, total_words_at_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.db.execute(sql, (
            result.id,
            result.novel_id,
            result.trigger_reason,
            result.trait,
            conflict_tags_json,
            breakpoints_json,
            len(result.breakpoints),
            result.status,
            result.error_message,
            context_patch or None,
            int(max(0, total_words_at_run)),
        ))
        self.db.get_connection().commit()
