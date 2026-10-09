"""规划进度的有界存储：容量 LRU + TTL 淘汰 + 线程安全（dict 兼容接口）。

替换模块级裸 dict——进度/结果按 novel_id、LLM 流文本按 act_id 累积，
无淘汰时守护进程长跑数月会无限增长（流文本一章规划可达数十 KB）。
范式参考 MemoryEngine._cache（OrderedDict + TTL + max_size）。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Optional


class BoundedStore:
    """dict 兼容的有界键值存储。

    - 容量：超出 max_size 时按 LRU 淘汰最久未访问项
    - TTL：读取时惰性过期（超期视为不存在并在访问时清除）
    - 线程安全：粗粒度锁（规划写入与 SSE 读取可能在异线程）
    """

    def __init__(self, max_size: int = 64, ttl_seconds: float = 6 * 3600.0) -> None:
        self._max_size = max(1, int(max_size))
        self._ttl = float(ttl_seconds)
        self._data: "OrderedDict[str, Any]" = OrderedDict()
        self._loaded_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def get(self, key: str, default: Any = None) -> Any:
        now = time.monotonic()
        with self._lock:
            if key not in self._data:
                return default
            if self._ttl > 0 and now - self._loaded_at.get(key, now) >= self._ttl:
                self._data.pop(key, None)
                self._loaded_at.pop(key, None)
                return default
            self._data.move_to_end(key)
            return self._data[key]

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._loaded_at[key] = time.monotonic()
            self._data.move_to_end(key)
            self._evict_locked()

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)
            self._loaded_at.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._loaded_at.clear()

    def __contains__(self, key: str) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def __getitem__(self, key: str) -> Any:
        value = self.get(key, _MISSING)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def _evict_locked(self) -> None:
        while len(self._data) > self._max_size:
            oldest, _ = self._data.popitem(last=False)
            self._loaded_at.pop(oldest, None)

    def snapshot_items(self) -> list:
        """诊断用：返回 (key, age_seconds) 列表。"""
        now = time.monotonic()
        with self._lock:
            return [(k, now - self._loaded_at.get(k, now)) for k in self._data]


_MISSING = object()
