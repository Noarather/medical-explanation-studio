import json
import threading
import time

import pytest
from batch_tasks import BatchTasks, BatchCancelled
from batch_import import preview, commit
from db_manager import DatabaseManager
from test_efficiency_workflow import QUESTION


def terminal(tasks, token):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = tasks.status(token)
        if state["status"] != "running":
            return state
        time.sleep(.005)
    pytest.fail("background task did not finish")


def test_background_nonblocking_cancel_and_single_flight():
    tasks = BatchTasks()
    ready, release = threading.Event(), threading.Event()
    def handler(progress):
        ready.set()
        assert release.wait(3)
        progress("校验", 1, 3)
    token = tasks.start("preview", handler)["token"]
    assert ready.wait(2) and tasks.active()["token"] == token
    with pytest.raises(ValueError, match="batch_busy"):
        tasks.start("preview", handler)
    assert tasks.cancel(token)["accepted"]
    release.set()
    assert terminal(tasks, token)["status"] == "cancelled"
    assert tasks.active() is None


def test_atomic_commit_cannot_be_cancelled():
    tasks = BatchTasks()
    ready, release = threading.Event(), threading.Event()
    def handler(progress):
        progress("提交入库", 0, 1)
        ready.set()
        assert release.wait(3)
        return {"imported": 1}
    token = tasks.start("commit", handler)["token"]
    assert ready.wait(2)
    assert not tasks.cancel(token)["accepted"]
    release.set()
    assert terminal(tasks, token)["result"] == {"imported": 1}


def test_cancel_before_commit_rolls_back(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps([QUESTION]), encoding="utf-8")
    with DatabaseManager(tmp_path / "db.sqlite") as db:
        draft = preview(db, [str(path)], "测试")
        def progress(stage, *args):
            if stage == "提交入库":
                raise BatchCancelled()
        with pytest.raises(BatchCancelled):
            commit(db, draft["draft_id"], progress=progress)
        assert not db.list_question_sets()
        assert commit(db, draft["draft_id"])["result"]["imported"] == 1


def test_worker_failure_and_result_isolation():
    tasks = BatchTasks()
    token = tasks.start("preview", lambda p: 1 / 0)["token"]
    assert terminal(tasks, token)["status"] == "failed"
    token = tasks.start("preview", lambda p: {"items": [1]})["token"]
    state = terminal(tasks, token)
    state["result"]["items"].clear()
    assert tasks.status(token)["result"]["items"] == [1]


def test_bridge_async_preview_commit_idempotence(qtbot, tmp_path):
    from ui_bridge.imports import ImportsBridge
    bridge = ImportsBridge(str(tmp_path / "test.db"))
    source = tmp_path / "q.json"
    source.write_text(json.dumps([QUESTION]), encoding="utf-8")
    started = bridge.api_batch_start("preview", {"paths": [str(source)], "name": "后台"})
    result = terminal(bridge._batch_tasks, started["token"])["result"]
    payload = {"draft_id": result["draft_id"], "start_generation": False}
    for expected in (1, 0):
        token = bridge.api_batch_start("commit", payload)["token"]
        state = terminal(bridge._batch_tasks, token)
        assert state["result"]["result"]["newly_imported"] == expected
    assert bridge.api_batch_active() == {"task": None}
    with pytest.raises(ValueError):
        bridge.api_batch_start("__dict__", {})
