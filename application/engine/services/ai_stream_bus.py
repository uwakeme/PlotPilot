"""AI 调用实时事件总线 — 跨进程单消费者队列。

与 `application.engine.services.streaming_bus` 结构对称，但**必须独立成一条
队列**：streaming_bus 的队列已被 `/autopilot/{id}/stream` 日志 SSE 单消费者占用，
两个 SSE 共用一条 mp.Queue 会互相抢消息（正文 chunk 被弹窗抢走会导致句首残缺，
见 streaming_bus.publish 的 docstring 警告）。

队列在 API 主进程创建（`init_ai_stream_bus`），经进程参数注入 daemon 子进程
（`inject_ai_stream_queue`）。独立启动的 daemon（`scripts/start_daemon.py`）
不会创建它，此时队列为 `None`，所有 publish 静默 no-op，SSE 端点降级为轮询
`ai_trace_spans` 表。

delta 合并发生在**生产者侧**：正文流式一次产生数千 chunk，一个 chunk 塞一条
消息会撞满队列。因此累积到 AI_DELTA_FLUSH_CHARS 字或距上次 flush 超过
AI_DELTA_FLUSH_INTERVAL_S 秒才推一条。
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import threading
import time
from queue import Empty, Full
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

AI_STREAM_QUEUE_MAX_SIZE = 2000
"""队列容量。delta 已批量，单条消息体积可控。"""

AI_DELTA_FLUSH_CHARS = 200
"""累积多少字符强制 flush 一次 delta。"""

AI_DELTA_FLUSH_INTERVAL_S = 0.1
"""距上次 flush 超过多少秒强制 flush。"""

AI_STREAM_DEFAULT_DRAIN_LIMIT = 200
"""单次 drain 最多取多少条。"""

_ai_stream_queue: Optional[mp.Queue] = None
_queue_lock = threading.Lock()


def init_ai_stream_bus() -> mp.Queue:
    """创建跨进程队列（API 主进程调用）。"""
    global _ai_stream_queue
    with _queue_lock:
        if _ai_stream_queue is None:
            _ai_stream_queue = mp.Queue(maxsize=AI_STREAM_QUEUE_MAX_SIZE)
            logger.info("[AiStreamBus] 队列已创建, maxsize=%d", AI_STREAM_QUEUE_MAX_SIZE)
    return _ai_stream_queue


def inject_ai_stream_queue(queue: mp.Queue) -> None:
    """注入已创建的队列（daemon 子进程调用）。"""
    global _ai_stream_queue
    _ai_stream_queue = queue
    logger.info("[AiStreamBus] 子进程已注入队列")


def get_ai_stream_queue() -> Optional[mp.Queue]:
    return _ai_stream_queue


class AiStreamBus:
    def __init__(self, queue: Optional[mp.Queue] = None) -> None:
        if queue is not None:
            inject_ai_stream_queue(queue)
        # span_id -> 待 flush 的增量文本 / 已累积字数 / 上次 flush 时刻
        self._pending: Dict[str, str] = {}
        self._pending_len: Dict[str, int] = {}
        self._last_flush: Dict[str, float] = {}

    # ── 内部 ──────────────────────────────────────────────

    def _emit(self, message: Dict[str, Any]) -> None:
        """写入队列。任何失败都只记日志，绝不向上抛。"""
        queue = get_ai_stream_queue()
        if queue is None:
            return
        message.setdefault("ts", time.time())
        try:
            queue.put_nowait(message)
        except Full:
            logger.warning(
                "[AiStreamBus] 队列满，丢弃事件 kind=%s span=%s",
                message.get("kind"),
                message.get("span_id"),
            )
        except Exception as exc:  # pragma: no cover - 防御性
            logger.debug("[AiStreamBus] 发布失败（忽略）: %s", exc)

    def _flush_pending(self, span_id: str, novel_id: str, trace_id: str) -> None:
        text = self._pending.pop(span_id, "")
        self._pending_len.pop(span_id, None)
        self._last_flush[span_id] = time.time()
        if not text:
            return
        self._emit(
            {
                "novel_id": novel_id,
                "trace_id": trace_id,
                "span_id": span_id,
                "kind": "delta",
                "delta": text,
            }
        )

    # ── publish ───────────────────────────────────────────

    def publish_start(
        self,
        *,
        novel_id: str,
        trace_id: str,
        span_id: str,
        stage: str = "",
        stage_label: str = "",
        model: str = "",
        profile: str = "",
        prompt: Optional[Dict[str, str]] = None,
    ) -> None:
        self._last_flush[span_id] = time.time()
        self._emit(
            {
                "novel_id": novel_id,
                "trace_id": trace_id,
                "span_id": span_id,
                "kind": "start",
                "stage": stage,
                "stage_label": stage_label,
                "model": model,
                "profile": profile,
                "prompt": prompt or {},
            }
        )

    def publish_delta(
        self,
        *,
        novel_id: str,
        trace_id: str,
        span_id: str,
        text: str,
        ts: Optional[float] = None,
    ) -> None:
        if not text:
            return
        self._pending[span_id] = self._pending.get(span_id, "") + text
        accumulated = self._pending_len.get(span_id, 0) + len(text)
        self._pending_len[span_id] = accumulated
        now = time.time()
        last = self._last_flush.get(span_id)
        if last is None:
            # 没有 flush 基准（如缺少 publish_start）时以此刻起算间隔，
            # 而不是立刻 flush —— 否则首个短 delta 就把攒字清空，合并形同虚设。
            last = now
            self._last_flush[span_id] = now
        if accumulated >= AI_DELTA_FLUSH_CHARS or (
            now - last >= AI_DELTA_FLUSH_INTERVAL_S
        ):
            self._flush_pending(span_id, novel_id, trace_id)

    def publish_end(
        self,
        *,
        novel_id: str,
        trace_id: str,
        span_id: str,
        response: str = "",
        token_in: Optional[int] = None,
        token_out: Optional[int] = None,
        latency_ms: Optional[int] = None,
        error: str = "",
    ) -> None:
        # 先把没满阈值的尾巴推出去，否则最后几个字会丢
        self._flush_pending(span_id, novel_id, trace_id)
        self._pending.pop(span_id, None)
        self._pending_len.pop(span_id, None)
        self._last_flush.pop(span_id, None)
        self._emit(
            {
                "novel_id": novel_id,
                "trace_id": trace_id,
                "span_id": span_id,
                "kind": "end",
                "response": response,
                "token_in": token_in,
                "token_out": token_out,
                "latency_ms": latency_ms,
                "error": error,
            }
        )

    def publish_error(
        self,
        *,
        novel_id: str,
        trace_id: str,
        span_id: str,
        error: str,
        latency_ms: Optional[int] = None,
    ) -> None:
        self._flush_pending(span_id, novel_id, trace_id)
        self._pending.pop(span_id, None)
        self._pending_len.pop(span_id, None)
        self._last_flush.pop(span_id, None)
        self._emit(
            {
                "novel_id": novel_id,
                "trace_id": trace_id,
                "span_id": span_id,
                "kind": "error",
                "error": error,
                "latency_ms": latency_ms,
            }
        )

    # ── 消费 ─────────────────────────────────────────────

    def drain(
        self, novel_id: str, limit: int = AI_STREAM_DEFAULT_DRAIN_LIMIT
    ) -> List[Dict[str, Any]]:
        """取出属于该小说的消息；不属于该小说的放回队尾。

        放回队尾会打乱顺序——前端 store 按 `span_id` 聚合、按事件 `ts` 排序，
        对此免疫。不要改成"丢弃不匹配的消息"，那会吃掉别的书的实时事件。
        """
        queue = get_ai_stream_queue()
        if queue is None:
            return []

        matched: List[Dict[str, Any]] = []
        others: List[Dict[str, Any]] = []
        for _ in range(limit):
            try:
                message = queue.get_nowait()
            except Empty:
                break
            except Exception as exc:  # pragma: no cover - 防御性
                logger.debug("[AiStreamBus] drain 异常: %s", exc)
                break
            if not isinstance(message, dict):
                continue
            if message.get("novel_id") == novel_id:
                matched.append(message)
            else:
                others.append(message)

        for message in others:
            try:
                queue.put_nowait(message)
            except Full:
                pass
        return matched


# 全局实例
ai_stream_bus = AiStreamBus()