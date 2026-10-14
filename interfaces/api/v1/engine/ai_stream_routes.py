"""AI 调用实时事件流（SSE）。

`GET /novels/{novel_id}/ai-stream`

优先从 `ai_stream_bus`（跨进程 mp.Queue）读取实时事件——含 delta，能看到
token 级增长。队列不可用时（独立启动的 daemon，`scripts/start_daemon.py`
不创建该队列）降级为轮询 `ai_trace_spans` 表，退化为调用级
（start / end / error，无 delta）。

事件契约见 docs/superpowers/specs/2026-10-14-ai-stream-popup-design.md §4.6。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/novels", tags=["engine-trace"])

AI_STREAM_SNAPSHOT_LIMIT = 20
"""连接建立时回填的历史条数。命中 idx_ai_trace_spans_novel(novel_id, created_at DESC)。"""

AI_STREAM_POLL_SECONDS = 0.4
"""降级轮询间隔。"""

AI_STREAM_MAX_LIFETIME_SECONDS = 1800
"""单条 SSE 存活上限，到点主动断开让前端重连。"""

AI_STREAM_DB_BATCH = 200
"""单轮降级轮询最多取多少条 span。"""

AI_STREAM_DRAIN_BATCH = 200
"""单轮从队列里最多取多少条消息。"""

AI_STREAM_TICK_TIMEOUT_SECONDS = 2.0
"""单轮 DB 读取上限。DB 被写锁住时放弃本轮，而不是让整条 SSE 挂死。

与 `autopilot_runtime_settings.sse_tick_timeout_seconds` 同为 2.0，但本模块不
import 它：那是 autopilot 的运行时配置，本端点与其无配置关系。
"""

DB_PHASE_TO_EVENT = {
    "llm_request": "start",
    "llm_response": "end",
    "error": "error",
}


async def _fetch_spans(fn):
    """在默认 executor 线程里跑无参 `fn`，超时返回 None。

    `fn` 是闭包而不是 `(fn, *args)` 形式——这是刻意的。store 的构造
    （`SqliteTraceStore.__init__` → `_ensure_tables()`，3 个 CREATE TABLE +
    4 个 CREATE INDEX + PRAGMA + commit）如果写成 `fn=store().list_xxx`，
    `store()` 是**实参**，会在事件循环线程上先求值，超时保护罩不住它，
    而这恰恰是 store 唯一被构造的地方——也就是守护进程正在写同一份
    SQLite、用户打开弹窗的那一刻。

    关于线程池：autopilot_routes 用的是它私有的 `_SSE_THREAD_POOL`，这里
    **不**复用。跨路由 import 私有名是耦合；`get_resource_manager()
    .get_thread_pool("sse-executor")` 又依赖 autopilot_routes 先被 import
    才有注册，换个导入顺序就静默拿不到池子。为一次只读查询再建一个受管
    线程池同样不划算。默认 executor + 硬超时兜底；超时不回收已启动的线程，
    但 sqlite 的 `busy_timeout_ms` 默认 30000ms（`sqlite_pragmas.py:14`），
    最坏 30s 而非无限——对一个 30 分钟就会自己断开的可选浮窗来说可以接受。
    """
    try:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, fn),
            timeout=AI_STREAM_TICK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.debug("AI 流 DB 读取超时（%.1fs），跳过本轮", AI_STREAM_TICK_TIMEOUT_SECONDS)
        return None
    except Exception as exc:
        logger.debug("AI 流 DB 读取失败（忽略）: %s", exc)
        return None


def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _build_event(message: Dict[str, Any]) -> Dict[str, Any]:
    """队列消息 → SSE 载荷。字段名与前端 TS 类型一一对应。"""
    kind = message.get("kind", "")
    event: Dict[str, Any] = {
        "type": kind,
        "novel_id": message.get("novel_id", ""),
        "trace_id": message.get("trace_id", ""),
        "span_id": message.get("span_id", ""),
        "stage": message.get("stage", ""),
        "stage_label": message.get("stage_label", ""),
        "model": message.get("model", ""),
        "profile": message.get("profile", ""),
        "ts": message.get("ts", 0.0),
    }
    if kind == "start":
        event["prompt"] = message.get("prompt") or {}
    elif kind == "delta":
        event["delta"] = message.get("delta", "")
    elif kind == "end":
        event["response"] = message.get("response", "")
        event["token_in"] = message.get("token_in")
        event["token_out"] = message.get("token_out")
        event["latency_ms"] = message.get("latency_ms")
        event["error"] = message.get("error", "")
    elif kind == "error":
        event["error"] = message.get("error", "")
        event["latency_ms"] = message.get("latency_ms")
    return event


def _db_span_to_event(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """`ai_trace_spans` 行 → SSE 载荷；非 LLM 相关 phase 返回 None。"""
    event_type = DB_PHASE_TO_EVENT.get(str(row.get("phase") or ""))
    if event_type is None:
        return None
    event: Dict[str, Any] = {
        "type": event_type,
        "novel_id": row.get("novel_id", ""),
        "trace_id": row.get("trace_id", ""),
        "span_id": row.get("span_id", ""),
        "stage": row.get("stage", "") or "",
        "stage_label": row.get("stage_label", "") or "",
        "model": row.get("model", "") or "",
        "profile": row.get("generation_profile", "") or "",
        "ts": row.get("created_at", ""),
    }
    if event_type == "start":
        event["prompt"] = row.get("prompt_full") or {}
    elif event_type == "end":
        event["response"] = row.get("response_full") or ""
        event["token_in"] = row.get("token_input")
        event["token_out"] = row.get("token_output")
        event["latency_ms"] = row.get("latency_ms")
        event["error"] = row.get("error") or ""
    else:
        event["error"] = row.get("error") or ""
        event["latency_ms"] = row.get("latency_ms")
    return event



def _trace_store():
    from engine.infrastructure.persistence.trace_store import SqliteTraceStore
    from interfaces.api.dependencies import get_database

    return SqliteTraceStore(get_database())


def _ai_stream_bus():
    from application.engine.services.ai_stream_bus import ai_stream_bus

    return ai_stream_bus


@router.get("/{novel_id}/ai-stream")
async def ai_stream(novel_id: str):
    """SSE 推送该书运行中的 AI 调用事件。"""

    async def generator():
        loop = asyncio.get_running_loop()
        started = loop.time()
        bus = _ai_stream_bus()
        queue_available = False

        try:
            from application.engine.services.ai_stream_bus import get_ai_stream_queue

            queue_available = get_ai_stream_queue() is not None
        except Exception as exc:  # pragma: no cover - 防御性
            logger.debug("AI 流队列探测失败: %s", exc)

        # TraceStore 每条连接建一次，且**只在 executor 线程里**构造——
        # 构造要走整套 CREATE TABLE IF NOT EXISTS。
        store_holder: List[Any] = []

        def store():
            if not store_holder:
                store_holder.append(_trace_store())
            return store_holder[0]

        yield _sse(
            {
                "type": "connected",
                "novel_id": novel_id,
                "queue": "mp_queue" if queue_available else "db_poll",
                "message": (
                    "AI 调用流已连接（实时增量）"
                    if queue_available
                    else "AI 调用流已连接（守护进程未共享队列，降级为调用级轮询）"
                ),
            }
        )

        cursor_created_at = ""
        cursor_id = 0

        # 快照回填，两种模式共用：队列里只有「本次连接之后」的事件，历史在库里。
        # 游标一并钉到快照里最新的一条，之后每轮只拿真正新增的 span——
        # 否则攒了上千 span 的书会在连接后把整段历史重放一遍。
        spans = await _fetch_spans(
            lambda: store().list_recent_ai_spans(novel_id, AI_STREAM_SNAPSHOT_LIMIT)
        )
        for span in spans or []:
            cursor_created_at = str(span.get("created_at") or "")
            cursor_id = int(span.get("id") or 0)
            event = _db_span_to_event(span)
            if event is not None:
                yield _sse(event)

        # `loop.time()` 是 time.monotonic()（开机以来的秒数），不是相对本次连接的
        # 偏移；写成 0.0 会让第一条 keepalive 在首次循环就发出去。
        last_ping = loop.time()
        while True:
            if (loop.time() - started) > AI_STREAM_MAX_LIFETIME_SECONDS:
                break

            emitted = 0
            if queue_available:
                try:
                    messages = bus.drain(novel_id, limit=AI_STREAM_DRAIN_BATCH)
                except Exception as exc:
                    logger.debug("AI 流 drain 失败: %s", exc)
                    messages = []
                for message in messages:
                    yield _sse(_build_event(message))
                    emitted += 1
            else:
                rows = await _fetch_spans(
                    lambda: store().list_ai_spans_since(
                        novel_id,
                        cursor_created_at,
                        cursor_id,
                        AI_STREAM_DB_BATCH,
                    )
                )
                # 游标每行都推进：SQL 已按 phase 过滤，`_db_span_to_event`
                # 在这里不会返回 None，推进游标是为了下一轮从正确位置继续。
                for row in rows or []:
                    cursor_created_at = str(row.get("created_at") or cursor_created_at)
                    cursor_id = int(row.get("id") or 0)
                    event = _db_span_to_event(row)
                    if event is not None:
                        yield _sse(event)
                        emitted += 1

            now = loop.time()
            if emitted == 0 and (now - last_ping) >= 15:
                last_ping = now
                yield _sse({"type": "ping"})

            await asyncio.sleep(0.05 if queue_available else AI_STREAM_POLL_SECONDS)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
