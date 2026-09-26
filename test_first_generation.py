import json

import pytest

from db_manager import DatabaseManager
from ui_bridge.questions import QuestionsBridge
from ui_bridge.library import LibraryBridge


@pytest.fixture
def setup_batch(tmp_path, monkeypatch):
    path = str(tmp_path / "synthetic.db")
    with DatabaseManager(path) as db:
        db.conn.execute("INSERT INTO question_sets(id,name,source_path,source_type) VALUES('batch','合成批次','','json')")
        for i in range(505):
            db.conn.execute("INSERT INTO imported_questions(set_id,external_id,subject,question_type,prompt_text,raw_json) VALUES('batch',?,'测试','A1','合成题','{}')", (str(i),))
        db.conn.commit()
    monkeypatch.setattr(LibraryBridge, "api_list", lambda self: {"libraries": [{"id": 1, "index_state": "compatible"}, {"id": 2, "index_state": "stale"}]})
    return QuestionsBridge(path), path


def test_whole_batch_snapshot_and_duplicate_guard(setup_batch):
    bridge, path = setup_batch
    preview = bridge.api_first_generation_preview("batch")
    assert preview["count"] == 505
    assert "ids" not in preview
    result = bridge.api_first_generation_start("batch", preview["token"], [1], True)
    with DatabaseManager(path) as db:
        job = db.get_job(result["job_id"])
        assert len(job["payload"]["question_ids"]) == 505
        assert job["payload"]["library_ids"] == [1]
    with pytest.raises(ValueError, match="已有生成任务"):
        bridge.api_first_generation_start("batch", preview["token"], [1], True)


def test_regenerate_all_including_existing_reviewed_and_failed(setup_batch):
    bridge, path = setup_batch
    preview = bridge.api_first_generation_preview("batch")
    with DatabaseManager(path) as db:
        db.conn.execute("UPDATE imported_questions SET explanation='保留解析' WHERE id=1")
        db.conn.execute("UPDATE imported_questions SET raw_json=? WHERE id=2", (json.dumps({"explanationBlocks": [{"text": "保留区块"}]}),))
        db.conn.execute("UPDATE imported_questions SET review_status='approved',pipeline_status='generated',generation_status='generated',generation_mode='textbook' WHERE id=3")
        db.conn.execute("UPDATE imported_questions SET pipeline_status='error' WHERE id=4")
        db.conn.commit()
    with pytest.raises(ValueError, match="已变化"):
        bridge.api_first_generation_start("batch", preview["token"], [1], True)
    updated = bridge.api_first_generation_preview("batch")
    assert updated["count"] == 505 and updated["skipped"] == 0
    assert updated["previous"] == 2 and updated["reviewed"] == 1
    result = bridge.api_first_generation_start("batch", updated["token"], [1], True)
    with DatabaseManager(path) as db:
        assert set(range(1, 5)).issubset(db.get_job(result["job_id"])["payload"]["question_ids"])
        assert db.conn.execute("SELECT COUNT(*) FROM imported_questions WHERE set_id='batch' AND pipeline_status='queued' AND review_status='pending'").fetchone()[0] == 505
        # Submission keeps the former answer until the normal worker saves a result.
        assert db.conn.execute("SELECT explanation FROM imported_questions WHERE id=1").fetchone()[0] == "保留解析"


def test_consent_scope_and_library_validation(setup_batch):
    bridge, path = setup_batch
    with pytest.raises(ValueError):
        bridge.api_first_generation_preview("")
    preview = bridge.api_first_generation_preview("batch")
    for libraries, consent in [([1], False), ([], True), ([2], True), ([99], True)]:
        with pytest.raises(ValueError):
            bridge.api_first_generation_start("batch", preview["token"], libraries, consent)
    with DatabaseManager(path) as db:
        assert db.list_jobs() == []
