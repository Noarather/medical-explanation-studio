"""Jobs domain: queue CRUD plus the QProcess worker scheduler."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from db_manager import DatabaseManager
from ui_bridge.protocol import BridgeBase

# job_type -> (required keys, optional keys); mirrors worker.py run_job dispatch.
JOB_PAYLOAD_SPECS: dict[str, tuple[set, set]] = {
    "scan": ({"library_id"}, {"force_ocr"}),
    "generate": ({"set_id"}, {"question_ids", "library_ids", "reuse_saved_evidence", "content_types"}),
    "general": ({"question_pk"}, set()),
    "general_batch": ({"set_id"}, {"question_ids"}),
    "tag_backfill": ({"set_id"}, {"include_approved"}),
    "study_point_backfill": ({"set_id"}, {"include_approved"}),
    "memory_card_backfill": ({"set_id"}, {"include_approved"}),
    "upgrade_v2": ({"set_id"}, set()),
}

CONTROL_ACTIONS = {"pause": "pause", "resume": "run", "cancel": "cancel"}


class JobRunner(QObject):
    """Launch queued jobs one at a time as `desktop_app.py --worker <id>` subprocesses."""

    jobs_changed = Signal(str)
    job_finished = Signal(str)

    def __init__(self, database_path: str, parent=None) -> None:
        super().__init__(parent)
        self._database_path = database_path
        self.process = QProcess(self)
        self.process.finished.connect(self._on_finished)
        self._timer = QTimer(self)
        self._timer.setInterval(900)
        self._timer.timeout.connect(self.tick)

    def start(self) -> None:
        with DatabaseManager(self._database_path) as database:
            database.recover_incomplete_jobs()
        self._timer.start()
        self.tick()

    def shutdown(self) -> None:
        self._timer.stop()
        if self.process.state() != QProcess.NotRunning:
            self.process.kill()
            self.process.waitForFinished(2000)

    def tick(self) -> None:
        self._emit_snapshot()
        if self.process.state() != QProcess.NotRunning:
            return
        with DatabaseManager(self._database_path) as database:
            job = database.next_queued_job()
        if not job:
            return
        if getattr(sys, "frozen", False):
            program, args = sys.executable, ["--worker", job["id"]]
        else:
            program, args = sys.executable, [str(Path(__file__).resolve().parent.parent / "desktop_app.py"), "--worker", job["id"]]
        self.process.start(program, args)

    def _on_finished(self, _exit_code: int, _status) -> None:
        self.job_finished.emit("{}")
        self.tick()

    def _emit_snapshot(self) -> None:
        with DatabaseManager(self._database_path) as database:
            jobs = database.list_jobs()
        self.jobs_changed.emit(json.dumps({"jobs": jobs}, ensure_ascii=False, default=str))


class JobsBridge(BridgeBase):
    def __init__(self, database_path: str, runner: JobRunner, parent=None) -> None:
        super().__init__(parent)
        self._database_path = database_path
        self._runner = runner

    def api_list(self) -> dict:
        with DatabaseManager(self._database_path) as database:
            return {"jobs": database.list_jobs()}

    def api_enqueue(self, job_type: str, title: str, payload: dict) -> dict:
        spec = JOB_PAYLOAD_SPECS.get(str(job_type))
        if spec is None:
            raise ValueError(f"bad_payload: 未知任务类型 {job_type}")
        required, _optional = spec
        missing = sorted(key for key in required if key not in (payload or {}))
        if missing:
            raise ValueError(f"bad_payload: 缺少必填字段 {', '.join(missing)}")
        with DatabaseManager(self._database_path) as database:
            job_id = database.create_job(str(job_type), str(title), dict(payload))
        self._runner.tick()
        return {"job_id": job_id}

    def api_control(self, job_id: str, action: str) -> dict:
        control = CONTROL_ACTIONS.get(str(action))
        if control is None:
            raise ValueError(f"bad_action: 不支持的操作 {action}")
        with DatabaseManager(self._database_path) as database:
            job = database.get_job(str(job_id))
            if job is None:
                raise ValueError("not_found: 任务不存在")
            terminal = {"completed", "failed", "cancelled"}
            if action == "resume" and job["control"] != "pause":
                raise ValueError("bad_state: 仅暂停中的任务可以继续")
            if action in {"pause", "cancel"} and job["status"] in terminal:
                raise ValueError(f"bad_state: 任务已终态（{job['status']}）")
            database.set_job_control(str(job_id), control)
            if action == "resume":
                database.update_job(str(job_id), status="queued", message="等待恢复")
        self._runner.tick()
        return {"job_id": job_id, "action": action}

    def api_delete(self, job_ids: list) -> dict:
        with DatabaseManager(self._database_path) as database:
            result = database.delete_jobs([str(item) for item in job_ids])
        return result

    def api_events(self, job_id: str) -> dict:
        with DatabaseManager(self._database_path) as database:
            rows = database.conn.execute(
                "SELECT level, message, created_at FROM job_events WHERE job_id=? ORDER BY id",
                (str(job_id),),
            ).fetchall()
        return {"events": [dict(row) for row in rows]}
