"""书库路由单元测试。"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.library.services.library_service import ProgressWriteNotFlushedError
from interfaces.api.v1 import library as library_module


class _Db:
    """最小只读实现：路由测试只验证 HTTP 契约与状态码。"""

    def __init__(self):
        self.flush_timed_out = False
        self.books = [
            {
                "slug": "n1",
                "title": "甲书",
                "author": "作者",
                "genre": "仙侠",
                "world_preset": "东方修真",
                "stage": "writing",
                "chapter_count": 2,
                "target_chapters": 100,
                "total_word_count": 5,
                "last_read": None,
            }
        ]
        self.recorded = []

    def list_books(self, sort="recent"):
        return list(self.books)

    def get_toc(self, novel_id):
        if novel_id != "n1":
            return None
        return {
            "slug": "n1",
            "title": "甲书",
            "chapters": [
                {"number": 1, "title": "第一章", "word_count": 2, "status": "draft"},
                {"number": 2, "title": "第二章", "word_count": 3, "status": "draft"},
            ],
        }

    def get_chapter(self, novel_id, number):
        if novel_id != "n1" or number not in (1, 2):
            return None
        return {
            "slug": "n1",
            "number": number,
            "title": f"第{number}章",
            "content": "正文内容",
            "word_count": 4,
            "prev_number": number - 1 if number > 1 else None,
            "next_number": number + 1 if number < 2 else None,
        }

    def record_progress(self, novel_id, chapter_number):
        if novel_id != "n1":
            return None
        if self.flush_timed_out:
            raise ProgressWriteNotFlushedError("读进度写队列超时，未确认落盘")
        self.recorded.append((novel_id, chapter_number))
        return {"slug": "n1", "last_chapter_number": chapter_number, "read_at": "2026-10-14 00:00:00"}


@pytest.fixture
def client(monkeypatch):
    db = _Db()
    monkeypatch.setattr(library_module, "_get_service", lambda: db)

    app = FastAPI()
    app.state.db = db
    app.include_router(library_module.router, prefix="/api/v1")
    return TestClient(app)


def test_list_books_returns_ok_envelope(client):
    response = client.get("/api/v1/library/books")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["books"][0]["slug"] == "n1"


def test_list_books_passes_sort_query(client, monkeypatch):
    seen = {}

    class _Spy(_Db):
        def list_books(self, sort="recent"):
            seen["sort"] = sort
            return []

    monkeypatch.setattr(library_module, "_get_service", lambda: _Spy())
    app = FastAPI()
    app.include_router(library_module.router, prefix="/api/v1")

    TestClient(app).get("/api/v1/library/books?sort=updated")

    assert seen["sort"] == "updated"


def test_toc_returns_chapters(client):
    response = client.get("/api/v1/library/books/n1/toc")

    assert response.status_code == 200
    assert len(response.json()["data"]["chapters"]) == 2


def test_toc_unknown_novel_returns_404(client):
    assert client.get("/api/v1/library/books/nope/toc").status_code == 404


def test_get_chapter_returns_content(client):
    response = client.get("/api/v1/library/books/n1/chapters/1")

    assert response.status_code == 200
    assert response.json()["data"]["content"] == "正文内容"


def test_get_chapter_unknown_number_returns_404(client):
    assert client.get("/api/v1/library/books/n1/chapters/99").status_code == 404


def test_record_progress_returns_stored_chapter(client):
    response = client.put(
        "/api/v1/library/books/n1/progress", json={"chapter_number": 2}
    )

    assert response.status_code == 200
    assert response.json()["data"]["last_chapter_number"] == 2


def test_record_progress_unknown_novel_returns_404(client):
    response = client.put(
        "/api/v1/library/books/nope/progress", json={"chapter_number": 1}
    )

    assert response.status_code == 404


def test_record_progress_rejects_non_positive_chapter(client):
    response = client.put(
        "/api/v1/library/books/n1/progress", json={"chapter_number": 0}
    )

    assert response.status_code == 422


def test_record_progress_maps_write_timeout_to_503(client):
    """服务层拒绝回读时，路由必须如实报 503，不能静默返回旧章号。"""
    client.app.state.db.flush_timed_out = True

    response = client.put(
        "/api/v1/library/books/n1/progress", json={"chapter_number": 7}
    )

    assert response.status_code == 503
