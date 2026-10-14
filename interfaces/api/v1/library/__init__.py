"""书库只读 API。

与 ``interfaces/api/v1/reader``（读者模拟分析）无关，勿混淆。
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from application.library.services.library_service import (
    LibraryService,
    ProgressWriteNotFlushedError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/library", tags=["library"])

_service: Optional[LibraryService] = None


def _get_service() -> LibraryService:
    global _service
    if _service is None:
        _service = LibraryService()
    return _service


class RecordProgressRequest(BaseModel):
    chapter_number: int = Field(..., ge=1, description="最后读到的章号")


@router.get("/books")
def list_books(sort: str = "recent"):
    """书库卡片列表。不含任何章节正文。"""
    return {"success": True, "data": {"books": _get_service().list_books(sort=sort)}}


@router.get("/books/{novel_id}/toc")
def get_toc(novel_id: str):
    """章节目录。不含章节正文。"""
    toc = _get_service().get_toc(novel_id)
    if toc is None:
        raise HTTPException(status_code=404, detail=f"Novel not found: {novel_id}")
    return {"success": True, "data": toc}


@router.get("/books/{novel_id}/chapters/{number}")
def get_chapter(novel_id: str, number: int):
    """单章正文与前后章号。只读。"""
    chapter = _get_service().get_chapter(novel_id, number)
    if chapter is None:
        raise HTTPException(
            status_code=404, detail=f"Chapter not found: {novel_id}#{number}"
        )
    return {"success": True, "data": chapter}


@router.put("/books/{novel_id}/progress")
def record_progress(novel_id: str, request: RecordProgressRequest):
    """记录读到哪一章。重复上报同一章不会产生第二行。

    503 分支不可省：写队列超时时服务层拒绝回读（否则会把上一章的旧章号
    当成新章号返回），这里必须如实告诉调用方「写没确认落盘」。
    """
    try:
        result = _get_service().record_progress(novel_id, request.chapter_number)
    except ProgressWriteNotFlushedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if result is None:
        raise HTTPException(status_code=404, detail=f"Novel not found: {novel_id}")
    return {"success": True, "data": result}
