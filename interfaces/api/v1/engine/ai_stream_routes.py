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
from typing import Any, Dict, Iterable, List, Optional

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

AI_STREAM_TICK_TIMEOUT_SECONDS = 2.0
"""单轮 DB 读取上限。DB 被写锁住时放弃本轮，而不是让整条 SSE 挂死。

与 `autopilot_runtime_settings.sse_tick_timeout_seconds` 同值口径，但本模块
不 import 它：那是 autopilot 的运行时配置，本端点与其无配置关系。
"""

DB_PHASE_TO_EVENT = {
    "llm_request": "start",
    "llm_response": "end",
    "error": "error",
}


async def _fetch_spans(executor, fn, *args):
    """跑一次同步 DB 读取，超时返回 None。

    与 autopilot_routes 的 SSE tick 同一套超时保护，但**不共用它的
    `_SSE_THREAD_POOL`**：那是 autopilot 路由模块的私有对象，跨路由 import
    私有名是耦合；`get_resource_manager().get_thread_pool("sse-executor")`
    又依赖 autopilot 先被 import 才有注册，注册顺序一换就静默拿不到池子。
    为一次只读查询再建一个受管线程池同样不划算。这里用默认 executor +
    硬超时兜底：超时只是不再等待那个线程，线程仍归默认池所有，
    而本端点的读最坏情况就是 sqlite 的 5s busy timeout。
    """
    try:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(executor, fn, *args),
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


def _snapshot_events(spans: Iterable[Dict[str, Any]], limit: int = AI_STREAM_SNAPSHOT_LIMIT) -> List[Dict[str, Any]]:
    """回填历史：只保留最新的 limit 条（输入按时间正序）。"""
    events = [e for e in (_db_span_to_event(s) for s in spans) if e is not None]
    events.sort(key=lambda e: str(e.get("ts") or ""))
    return events[-limit:] if limit > 0 else events


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

        # TraceStore 只建一次。`SqliteTraceStore.__init__` 会跑整套
        # CREATE TABLE IF NOT EXISTS + PRAGMA + commit，降级模式每 0.4s
        # 重建一次纯属浪费——而每开一个弹窗就是一条这样的连接。
        store_holder: List[SqliteTraceStore] = []

        def store() -> SqliteTraceStore:
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

        # 队列模式回填最近若干条已完成调用；降级模式由首轮轮询回填
        if queue_available:
            spans = await _fetch_spans(
                None, store().list_ai_spans_since, novel_id, "", 0, 200
            )
            for event in _snapshot_events(spans or []):
                yield _sse(event)

        # `loop.time()` 是 time.monotonic()（开机以来的秒数），不是相对本次连接的
        # 偏移；写成 0.0 会让第一条 keepalive 在首次循环就发出去。
        last_ping = loop.time()
        while True:
            if (loop.time() - started) > AI_STREAM_MAX_LIFETIME_SECONDS:
                break
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                break

            emitted = 0
            if queue_available:
                try:
                    messages = bus.drain(novel_id, limit=200)
                except Exception as exc:
                    logger.debug("AI 流 drain 失败: %s", exc)
                    messages = []
                for message in messages:
                    yield _sse(_build_event(message))
                    emitted += 1
            else:
                first_batch = not cursor_created_at
                rows = await _fetch_spans(
                    None,
                    store().list_ai_spans_since,
                    novel_id,
                    cursor_created_at,
                    cursor_id,
                    AI_STREAM_DB_BATCH,
                )
                # 游标无论是否产出事件都要推进，否则非 LLM phase 会卡住轮询。
                batch: List[Dict[str, Any]] = []
                for row in rows or []:
                    cursor_created_at = str(row.get("created_at") or cursor_created_at)
                    cursor_id = int(row.get("id") or 0)
                    event = _db_span_to_event(row)
                    if event is not None:
                        batch.append(event)
                # 首轮是升序取的历史，游标要从最老开始走。不裁剪的话，
                # 一本攒了上千 span 的书会先把几周前的调用倒给用户看，
                # 实时事件还得等游标爬完才出现。只在首轮裁，游标照样走到底。
                #
                # ⚠️ 这里**不能**复用 `_snapshot_events`：它内部还要再跑一遍
                # `_db_span_to_event`，而 `batch` 里已经是事件（没有 `phase` 键），
                # 会被整批过滤成空 —— 首轮回填静默丢光，弹窗开出来一片空白。
                # `batch` 本身就是升序，切末尾 N 条即「保留最新、仍按时间正序」。
                for event in batch[-AI_STREAM_SNAPSHOT_LIMIT:] if first_batch else batch:
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