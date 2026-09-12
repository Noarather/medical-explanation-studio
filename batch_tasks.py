"""Bounded, cancellable import work outside the Qt GUI thread. No cloud calls."""
from __future__ import annotations

import copy
import threading
import uuid


class BatchCancelled(Exception):
    pass


class BatchTasks:
    def __init__(self):
        self._lock = threading.Lock()
        self._tasks = {}

    def active(self):
        with self._lock:
            return next(({"token": token, **copy.deepcopy(task)} for token, task in self._tasks.items()
                         if task["status"] == "running"), None)

    def start(self, action, handler):
        with self._lock:
            if any(t["status"] == "running" for t in self._tasks.values()):
                raise ValueError("batch_busy: 已有批量操作运行中，请等待完成或取消")
            token = uuid.uuid4().hex
            # Keep recent terminal results so a delayed browser poll still finds them.
            for old in list(self._tasks)[:-7]:
                del self._tasks[old]
            self._tasks[token] = dict(status="running", action=action, stage="准备",
                current=0, total=0, cancellable=True, cancel_requested=False)

        def progress(stage, current, total):
            with self._lock:
                task = self._tasks[token]
                if task["cancellable"] and task["cancel_requested"]:
                    raise BatchCancelled("批量操作已取消，未提交的数据未导入")
                if stage == "提交入库":
                    task["cancellable"] = False  # Never interrupt an atomic commit.
                task.update(stage=stage, current=current, total=total)

        def run():
            try:
                result = handler(progress)
                with self._lock:
                    self._tasks[token].update(status="completed", result=result, cancellable=False)
            except BatchCancelled as exc:
                with self._lock:
                    self._tasks[token].update(status="cancelled", message=str(exc), cancellable=False)
            except Exception as exc:
                with self._lock:
                    self._tasks[token].update(status="failed", message=str(exc), cancellable=False)

        threading.Thread(target=run, name="batch-import", daemon=True).start()
        return {"token": token}

    def status(self, token):
        with self._lock:
            if token not in self._tasks:
                raise ValueError("not_found: 批量任务不存在，请恢复已保存批次")
            return copy.deepcopy(self._tasks[token])

    def cancel(self, token):
        with self._lock:
            task = self._tasks.get(token)
            if task is None:
                raise ValueError("not_found: 批量任务不存在")
            accepted = task["status"] == "running" and task["cancellable"]
            if accepted:
                task["cancel_requested"] = True
            return {"accepted": accepted}
