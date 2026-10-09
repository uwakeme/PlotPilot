"""500 响应不得回显内部异常原文（路径/base_url/密钥片段等只进日志）。"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from interfaces.api.middleware.error_handler import add_error_handlers


def _client() -> TestClient:
    app = FastAPI()
    add_error_handlers(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("sqlite3.OperationalError: no such table: D:\\secret\\path\\data.db")

    return TestClient(app, raise_server_exceptions=False)


def test_500_response_does_not_echo_exception_text():
    resp = _client().get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "INTERNAL_ERROR"
    # 异常原文（含路径/SQL 细节）不得出现在响应体
    assert "secret" not in body["message"]
    assert "sqlite3" not in body["message"]
    assert body["message"]  # 有通用文案
