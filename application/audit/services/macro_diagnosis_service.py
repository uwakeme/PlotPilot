"""宏观诊断服务 - 自动扫描 + 结果存储"""
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from uuid import uuid4

from application.audit.dtos.macro_refactor_dto import LogicBreakpoint
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
                    "tags": bp.tags
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
    ):
        self.db = db
        self.scanner = scanner
        # H-06：叙事校准指令由 LLM 写（None ⇒ 不生成校准 ⇒ 不注入）。
        self.llm_service = llm_service
    
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
        """扫描 → AI 写叙事校准指令 → 落库（生产路径，H-06）。

        校准指令**只**来自 LLM：没给 / 调用失败 / 输出不合契约 ⇒ 落空
        ⇒ 生成上下文不注入任何东西。**代码不代写校准句。**
        """
        result = self._scan_full(novel_id, trigger_reason, traits)
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
            for trait in scan_traits:
                breakpoints = self.scanner.scan_breakpoints(
                    novel_id=novel_id,
                    trait=trait,
                    conflict_tags=None  # 使用内置规则
                )
                all_breakpoints.extend(breakpoints)

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
                   error_message, created_at
            FROM macro_diagnosis_results
            WHERE novel_id = ?
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
                   error_message, created_at
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
