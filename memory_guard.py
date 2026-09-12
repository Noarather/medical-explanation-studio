"""Process memory monitoring and bounded garbage collection.

Dependencies: psutil.
"""

from __future__ import annotations

import gc
import os
import time

import psutil


class MemoryGuard:
    def __init__(self, max_memory_mb: int = 2048, pause_seconds: float = 0.2):
        self.max_memory_mb = int(max_memory_mb)
        self.pause_seconds = pause_seconds
        self.process = psutil.Process(os.getpid())

    def current_mb(self) -> float:
        return self.process.memory_info().rss / (1024 * 1024)

    def check(self, label: str = "") -> float:
        current = self.current_mb()
        if current > self.max_memory_mb:
            print(f"[memory] {label} uses {current:.1f}MB, above {self.max_memory_mb}MB; collecting garbage")
            gc.collect()
            time.sleep(self.pause_seconds)
            current = self.current_mb()
            if current > self.max_memory_mb:
                raise MemoryError(f"Memory remains above limit: {current:.1f}MB")
        return current

