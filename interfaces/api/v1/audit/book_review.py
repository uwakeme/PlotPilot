"""全书终审 API —— 分幕精审 + 汇总报告。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Path

from application.audit.services.book_review_service import BookReviewError, BookReviewService
from interfaces.api.dependencies import get_book_review_service
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/novels", tags=["book-review"])


class ActReviewRequest(BaseModel):
    force: bool = True  # 预留：是否重跑已审幕（当前始终重跑覆盖）


@router.post("/{novel_id}/book-review/acts/{act_number}/review")
async def review_act(
    novel_id: str,
    act_number: int = Path(..., ge=1),
    service: BookReviewService = Depends(get_book_review_service),
) -> dict:
    """精审单幕：把该幕完整正文交给审读模型，产出并持久化结构化 findings。"""
    try:
        payload = await service.run_act_review(novel_id, act_number)
    except BookReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("单幕精审失败 novel=%s act=%s", novel_id, act_number)
        raise HTTPException(status_code=500, detail=f"单幕精审失败: {exc}")
    return {"ok": True, "act": payload}


@router.post("/{novel_id}/book-review/synthesize")
async def synthesize_review(
    novel_id: str,
    service: BookReviewService = Depends(get_book_review_service),
) -> dict:
    """汇总全部已审幕结果，生成并持久化全书终审报告。"""
    try:
        payload = await service.synthesize(novel_id)
    except BookReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("终审汇总失败 novel=%s", novel_id)
        raise HTTPException(status_code=500, detail=f"终审汇总失败: {exc}")
    return {"ok": True, "report": payload}


@router.get("/{novel_id}/book-review/report")
async def get_review_report(
    novel_id: str,
    service: BookReviewService = Depends(get_book_review_service),
) -> dict:
    """最近终审报告 + 各幕精审进度（支持断点续审）。"""
    try:
        return service.get_report(novel_id)
    except Exception as exc:
        logger.exception("读取终审报告失败 novel=%s", novel_id)
        raise HTTPException(status_code=500, detail=f"读取终审报告失败: {exc}")
