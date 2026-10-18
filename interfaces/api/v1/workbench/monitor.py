"""监控大盘 API endpoints - 提供张力曲线、人声漂移、伏笔统计等监控数据

重要：SQLite 访问必须在 asyncio.to_thread 中执行，避免阻塞 Starlette 事件循环。
若在 async def 里直接调 chapter_repo / novel_repo，一次慢查询会让全进程所有 API「一起卡」，与「失败隔离」设计理念相悖。
"""

import asyncio
import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.connection import get_database
from infrastructure.persistence.database.sqlite_chapter_style_score_repository import (
    SqliteChapterStyleScoreRepository,
)
from interfaces.api.dependencies import (
    get_novel_repository,
    get_chapter_repository,
    get_foreshadowing_repository,
    get_voice_fingerprint_repository,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/novels", tags=["monitor"])

# 未评估哨兵值（与 TensionDimensions.UNEVALUATED 保持一致）
UNEVALUATED = -1.0


class TensionPoint(BaseModel):
    chapter: int
    tension: float
    title: str
    evaluated: bool = True  # 是否已完成真实评估


class TensionCurveResponse(BaseModel):
    novel_id: str
    points: List[TensionPoint]
    stats: Optional["TensionCurveStats"] = None


class TensionCurveStats(BaseModel):
    """张力曲线统计信息"""
    avg_tension: float = 0.0         # 平均张力（排除未评估）
    max_tension: float = 0.0         # 最高张力
    min_tension: float = 0.0         # 最低张力
    variance: float = 0.0            # 张力方差（衡量起伏程度）
    is_flat: bool = False            # 曲线是否过于平缓（方差<4.0）
    evaluated_count: int = 0         # 已评估章节数
    unevaluated_count: int = 0       # 未评估章节数
    consecutive_low: int = 0         # 连续低张力章节数（<40分即<4.0）


def _get_tension_curve_sync(novel_id: str) -> TensionCurveResponse:
    chapter_repo = get_chapter_repository()
    chapters = chapter_repo.list_by_novel(NovelId(novel_id))

    points: List[TensionPoint] = []
    evaluated_scores: List[float] = []  # 0-100 原始分
    consecutive_low = 0
    max_consecutive_low = 0

    for ch in chapters:
        raw = getattr(ch, "tension_score", None)
        ch_status = getattr(ch, "status", None)
        # 🔥 判断是否已完成真实张力评估：
        # 1. raw 为 None 或 -1（UNEVALUATED）→ 未评估
        # 2. 章节 status=draft 且 tension_score=50.0（schema默认值）→ 未评估
        #    因为 draft 章节没有经过审计管线，50.0 只是建表默认值，不是真实评分
        is_unevaluated = raw is None or float(raw) == UNEVALUATED
        is_default_draft = (
            ch_status == "draft"
            and raw is not None
            and float(raw) == 50.0
            and getattr(ch, "plot_tension", None) is None
        )
        if is_unevaluated or is_default_draft:
            # 未评估的章节
            points.append(
                TensionPoint(
                    chapter=ch.number,
                    tension=0.0,
                    title=ch.title or f"第{ch.number}章",
                    evaluated=False,
                )
            )
        else:
            raw_tension = float(raw)
            tension = raw_tension / 10.0  # 0-100 → 0-10
            points.append(
                TensionPoint(
                    chapter=ch.number,
                    tension=tension,
                    title=ch.title or f"第{ch.number}章",
                    evaluated=True,
                )
            )
            evaluated_scores.append(raw_tension)

            # 连续低张力检测
            if raw_tension < 40:
                consecutive_low += 1
                max_consecutive_low = max(max_consecutive_low, consecutive_low)
            else:
                consecutive_low = 0

    points.sort(key=lambda p: p.chapter)

    # 计算统计信息
    stats = TensionCurveStats()
    evaluated_count = len(evaluated_scores)
    unevaluated_count = len(points) - evaluated_count

    if evaluated_count > 0:
        avg_100 = sum(evaluated_scores) / evaluated_count
        max_100 = max(evaluated_scores)
        min_100 = min(evaluated_scores)

        # 方差（在 0-10 刻度上计算）
        avg_10 = avg_100 / 10.0
        variance = sum((s / 10.0 - avg_10) ** 2 for s in evaluated_scores) / evaluated_count

        stats = TensionCurveStats(
            avg_tension=round(avg_100 / 10.0, 1),
            max_tension=round(max_100 / 10.0, 1),
            min_tension=round(min_100 / 10.0, 1),
            variance=round(variance, 2),
            is_flat=variance < 1.0 and evaluated_count >= 3,  # 方差<1.0=过于平缓
            evaluated_count=evaluated_count,
            unevaluated_count=unevaluated_count,
            consecutive_low=max_consecutive_low,
        )
    else:
        stats.evaluated_count = 0
        stats.unevaluated_count = unevaluated_count

    return TensionCurveResponse(novel_id=novel_id, points=points, stats=stats)


# 文风漂移告警阈值（drift = 1 - similarity，越高越糟）
DRIFT_WARNING = 0.3
DRIFT_CRITICAL = 0.5

# 基准样本门槛，与写入侧保持一致（voice_drift_service.py:108）
FINGERPRINT_MIN_SAMPLES = 10


class VoiceDriftResponse(BaseModel):
    """文风漂移。**漂移度越高越糟**，与 similarity 相反。

    三态由 ``evaluated`` 决定 —— 调用方不得只看 ``drift_score``，
    未评估时它是 ``None``（不是 0）。
    """

    novel_id: str
    drift_score: Optional[float]  # None = 未评估
    status: str  # "unevaluated" | "normal" | "warning" | "critical"
    evaluated: bool
    sample_count: int  # 真实的基准样本数
    last_scored_chapter: Optional[int]
    #: 本次评分的**真实**计算时刻（chapter_style_scores.computed_at）。
    #: None = 未评估。此前前端拿「拉取时刻」冒充「检测时刻」，
    #: 未评估时也会显示「最近检测=现在」，那是伪造时间戳。
    computed_at: Optional[str] = None
    #: 真实阈值（0-1 刻度）。前端此前自定 3.0/6.0 并展示给用户 ——
    #: 那是 0-10 刻度的另一套数，与后端判定不一致，且展示的是假信息。
    warning_threshold: float = DRIFT_WARNING
    critical_threshold: float = DRIFT_CRITICAL
    reason: str = ""  # 未评估原因；已评估时为空


class ForeshadowStatsResponse(BaseModel):
    total_planted: int
    total_resolved: int
    pending: int
    forgotten_risk: int
    resolution_rate: float


@router.get("/{novel_id}/monitor/tension-curve", response_model=TensionCurveResponse)
async def get_tension_curve(novel_id: str):
    """
    获取章节张力曲线数据

    返回每章的张力值（0-10），用于绘制张力曲线图
    """
    try:
        return await asyncio.to_thread(_get_tension_curve_sync, novel_id)
    except Exception as e:
        logger.error(f"Error fetching tension curve: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch tension curve")


def _unevaluated(
    novel_id: str, sample_count: int, reason: str
) -> VoiceDriftResponse:
    """未评估的诚实形态：drift_score 为 None，附原因。

    绝不能回填 0 —— 0 会被读成「测过了，文风完美」。
    """
    return VoiceDriftResponse(
        novel_id=novel_id,
        drift_score=None,
        status="unevaluated",
        evaluated=False,
        sample_count=sample_count,
        last_scored_chapter=None,
        reason=reason,
    )


def _get_voice_drift_sync(novel_id: str) -> Optional[VoiceDriftResponse]:
    # 0. 小说不存在 → None（端点据此回 404，与原行为一致）
    novel = get_novel_repository().get_by_id(NovelId(novel_id))
    if not novel:
        return None

    # 1. 有没有合格的作者风格基准？没有就一律未评估。
    fingerprint = get_voice_fingerprint_repository().get_by_novel(novel_id, None)
    sample_count = int((fingerprint or {}).get("sample_count") or 0)
    if fingerprint is None:
        return _unevaluated(novel_id, 0, "尚无作者风格基准（文风指纹需随章节生成）")
    if sample_count < FINGERPRINT_MIN_SAMPLES:
        return _unevaluated(
            novel_id,
            sample_count,
            f"基准样本不足（{sample_count}/{FINGERPRINT_MIN_SAMPLES}）",
        )

    # 2. 取最近一次真实评分。similarity 越高越像基准 ⇒ 漂移 = 1 - similarity。
    latest = (
        SqliteChapterStyleScoreRepository(get_database()).get_latest_by_novel(novel_id)
    )
    if latest is None:
        return _unevaluated(novel_id, sample_count, "已有基准，但尚无章节评分记录")

    similarity = float(latest["similarity_score"])
    drift = round(max(0.0, min(1.0, 1.0 - similarity)), 4)
    if drift >= DRIFT_CRITICAL:
        status = "critical"
    elif drift >= DRIFT_WARNING:
        status = "warning"
    else:
        status = "normal"

    return VoiceDriftResponse(
        novel_id=novel_id,
        drift_score=drift,
        status=status,
        evaluated=True,
        sample_count=sample_count,
        last_scored_chapter=int(latest["chapter_number"]),
        computed_at=latest.get("computed_at"),
        reason="",
    )


@router.get("/{novel_id}/monitor/voice-drift", response_model=VoiceDriftResponse)
async def get_voice_drift(novel_id: str):
    """
    获取文风漂移检测结果（**漂移度，0-1，越高越糟**，超过 0.3 为异常）

    没有基准或没有评分记录时返回 ``evaluated=False`` + ``drift_score=None``，
    而不是编一个看起来正常的数字。
    """
    try:
        results = await asyncio.to_thread(_get_voice_drift_sync, novel_id)
        if results is None:
            raise HTTPException(status_code=404, detail="Novel not found")
        return results
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching voice drift: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch voice drift data")


def _get_foreshadow_stats_sync(novel_id: str) -> ForeshadowStatsResponse:
    foreshadowing_repo = get_foreshadowing_repository()
    chapter_repo = get_chapter_repository()

    registry = foreshadowing_repo.get_by_novel_id(NovelId(novel_id))
    if not registry:
        return ForeshadowStatsResponse(
            total_planted=0,
            total_resolved=0,
            pending=0,
            forgotten_risk=0,
            resolution_rate=0.0,
        )

    entries = registry.subtext_entries
    total_planted = len(entries)
    total_resolved = sum(1 for e in entries if e.status == "consumed")
    pending = total_planted - total_resolved

    chapters = chapter_repo.list_by_novel(NovelId(novel_id))
    current_chapter = max((ch.number for ch in chapters), default=0)

    forgotten_risk = 0
    for entry in entries:
        if entry.status == "pending":
            planted_chapter = entry.chapter
            if current_chapter - planted_chapter > 10:
                forgotten_risk += 1

    resolution_rate = (total_resolved / total_planted * 100) if total_planted > 0 else 0.0

    return ForeshadowStatsResponse(
        total_planted=total_planted,
        total_resolved=total_resolved,
        pending=pending,
        forgotten_risk=forgotten_risk,
        resolution_rate=round(resolution_rate, 1),
    )


@router.get("/{novel_id}/monitor/foreshadow-stats", response_model=ForeshadowStatsResponse)
async def get_foreshadow_stats(novel_id: str):
    """
    获取伏笔统计数据

    返回已埋伏笔、已回收、待回收、遗忘风险等统计信息
    """
    try:
        return await asyncio.to_thread(_get_foreshadow_stats_sync, novel_id)
    except Exception as e:
        logger.error(f"Error fetching foreshadow stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch foreshadow statistics")
