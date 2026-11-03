"""进程内滑动窗口限速。

够用的理由：单进程部署（uvicorn 单 worker 或 gunicorn 多 worker 各自一份），
目的是挡住「4 位提取码暴力枚举」和脚本灌包，不是精确计费。

键拆成两类：
  * ``write:<ip>``   整体写入速率
  * ``unlock:<ip>:<share>``  同一 IP 对同一份分享的提取码尝试次数
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock


class SlidingWindow:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def hit(self, key: str, limit: int, window_seconds: float = 60.0) -> bool:
        """记一次；超过限额返回 False。"""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            cutoff = now - window_seconds
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            # 顺手回收长期不用的键，避免一个扫描器把字典撑爆
            if len(self._hits) > 4096:
                for stale in [k for k, v in self._hits.items() if not v]:
                    self._hits.pop(stale, None)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()
