"""Process memory monitoring and bounded garbage collection.

Dependencies: psutil.
"""

from __future__ import annotations

import gc
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

import psutil


class MemoryState(str, Enum):
    NORMAL = "normal"
    SOFT = "soft"
    HARD = "hard"


@dataclass(frozen=True)
class MemorySnapshot:
    current_mb: float
    soft_limit_mb: float
    hard_limit_mb: float
    state: MemoryState
    label: str = ""


class MemoryLimitExceeded(MemoryError):
    """Memory pressure that remained above the hard limit after recovery."""

    code = "MEMORY_LIMIT_EXCEEDED"

    def __init__(
        self, snapshot: MemorySnapshot, *, stage: str = "", file_path: str = "",
        page_range: tuple[int, int] | None = None, batch_size: int | None = None,
    ):
        self.snapshot = snapshot
        self.stage = stage or snapshot.label
        self.file_path = file_path
        self.page_range = page_range
        self.batch_size = batch_size
        details = [
            self.code,
            f"stage={self.stage or 'unknown'}",
            f"rss={snapshot.current_mb:.1f}MB",
            f"soft={snapshot.soft_limit_mb:.1f}MB",
            f"hard={snapshot.hard_limit_mb:.1f}MB",
        ]
        if page_range:
            details.append(f"pages={page_range[0]}-{page_range[1]}")
        if batch_size is not None:
            details.append(f"batch={batch_size}")
        if file_path:
            details.append(f"file={file_path}")
        super().__init__("; ".join(details))


class MemoryGuard:
    def __init__(
        self, max_memory_mb: int = 2048, pause_seconds: float = 0.2,
        soft_memory_mb: float | None = None,
    ):
        self.max_memory_mb = int(max_memory_mb)
        default_margin = max(256.0, self.max_memory_mb * 0.15)
        self.soft_memory_mb = float(
            soft_memory_mb if soft_memory_mb is not None
            else max(0.0, self.max_memory_mb - default_margin)
        )
        if self.soft_memory_mb > self.max_memory_mb:
            raise ValueError("soft memory limit cannot exceed hard memory limit")
        self.pause_seconds = pause_seconds
        self.process = psutil.Process(os.getpid())

    def current_mb(self) -> float:
        return self.process.memory_info().rss / (1024 * 1024)

    def sample(self, label: str = "") -> MemorySnapshot:
        current = self.current_mb()
        if current > self.max_memory_mb:
            state = MemoryState.HARD
        elif current >= self.soft_memory_mb:
            state = MemoryState.SOFT
        else:
            state = MemoryState.NORMAL
        return MemorySnapshot(
            current_mb=current,
            soft_limit_mb=self.soft_memory_mb,
            hard_limit_mb=float(self.max_memory_mb),
            state=state,
            label=label,
        )

    def recover(
        self, label: str = "", release: Callable[[], None] | None = None,
        *, stage: str = "", file_path: str = "",
        page_range: tuple[int, int] | None = None, batch_size: int | None = None,
        raise_on_hard: bool = True,
    ) -> MemorySnapshot:
        if release is not None:
            release()
        gc.collect()
        if self.pause_seconds:
            time.sleep(self.pause_seconds)
        snapshot = self.sample(label)
        if raise_on_hard and snapshot.state is MemoryState.HARD:
            raise MemoryLimitExceeded(
                snapshot, stage=stage, file_path=file_path,
                page_range=page_range, batch_size=batch_size,
            )
        return snapshot

    def check(self, label: str = "") -> float:
        snapshot = self.sample(label)
        if snapshot.state is MemoryState.HARD:
            print(
                f"[memory] {label} uses {snapshot.current_mb:.1f}MB, "
                f"above {self.max_memory_mb}MB; collecting garbage"
            )
            snapshot = self.recover(label)
        return snapshot.current_mb
