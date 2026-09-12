import copy
import json

import pytest

from auto_review import run as auto_review
from batch_import import preview, commit, resume, profiles
from db_manager import DatabaseManager
from importers import normalize_formatted_rows, normalize_question, load_json
from incremental_export import preflight, export
from question_format_v2 import make_envelope, write_xlsx_v2
from services import QuestionFormattingService


QUESTION = {"id": "q1", "bank": "school", "subject": "内科学", "type": "A1",
            "question": "以下属于典型表现的是？", "options": ["A. 胸痛", "B. 皮疹"], "answer": "A"}
EVIDENCE = [{"textbook": "内科学", "source_file": "内科学.pdf", "source_page": 88,
             "pdf_page": 100, "score": .88, "text": "教材记载典型表现为持续性胸痛，并可伴随大汗，应结合症状综合判断。"}]
PACKAGE = {
    "briefExplanation": "典型表现为持续性胸痛。", "tags": ["典型表现"],
    "explanationBlocks": [
        {"section": "analysis", "type": "paragraph", "text": "本题考查典型临床表现，需结合持续性胸痛的特点识别。" * 3},
        {"section": "answerBasis", "type": "paragraph", "text": "教材记载持续性胸痛为典型表现，因此选择 A。"},
        {"section": "pitfalls", "type": "table", "columns": ["选项", "辨析"], "rows": [["皮疹", "并非典型表现"]]},
    ],
    "explanationMeta": {"mode": "textbook", "evidenceGrade": "A", "evidence": EVIDENCE},
}


@pytest.fixture
def db(tmp_path):
    with DatabaseManager(tmp_path / "test.db") as database:
        yield database


def generate(db, questions=None):
    set_id = db.create_question_set("自动化测试", "fixture.json", "json", questions or [QUESTION])
    ids = [row["id"] for row in db.list_imported_questions(set_id)]
    for pk in ids:
        db.save_generated_result(pk, "generated", "textbook", .88, "", EVIDENCE, package=copy.deepcopy(PACKAGE))
    return set_id, ids


def test_automatic_pass_and_exceptions_are_audited_without_viewing(db):
    set_id, ids = generate(db, [{**QUESTION, "id": f"q{i}"} for i in range(3)])
    with db.conn:
        db.conn.execute("UPDATE imported_questions SET match_score=.6 WHERE id=?", (ids[1],))
        db.conn.execute("UPDATE imported_questions SET review_status='rejected' WHERE id=?", (ids[2],))
    result = auto_review(db, set_id)
    assert (result["approved"], result["pending"]) == (1, 1)
    row = db.get_imported_question(ids[0])
    assert row["review_status"] == "approved" and not row["viewed_at"]
    assert db.conn.execute("SELECT COUNT(*) FROM review_actions WHERE question_pk=?", (ids[0],)).fetchone()[0] == 1
    assert auto_review(db, set_id)["approved"] == 0
    with db.conn:
        db.conn.execute("UPDATE imported_questions SET review_status='pending' WHERE id=?", (ids[0],))
    assert auto_review(db, set_id)["approved"] == 0
    assert db.get_imported_question(ids[2])["review_status"] == "rejected"


@pytest.mark.parametrize("field,value", [("answer", "Z"), ("bank", ""), ("type", "unknown")])
def test_invalid_question_is_not_automatically_approved(db, field, value):
    set_id, ids = generate(db, [{**QUESTION, field: value}])
    assert auto_review(db, set_id)["approved"] == 0
    assert db.get_imported_question(ids[0])["review_status"] == "pending"


def test_policy_can_be_disabled_and_survives_database_reopen(db):
    set_id, _ = generate(db)
    db.set_setting("review.auto_enabled", "0")
    assert auto_review(db, set_id)["enabled"] is False
    db.set_setting("review.auto_enabled", "1")
    assert auto_review(db, set_id)["approved"] == 1


def test_fill_survives_preview_json_excel_and_import(tmp_path):
    row = {**QUESTION, "type": "填空题", "options": [], "question": "化合物为【1】，名称为【2】。", "answer": [["ATP", "atp"], ["乙"]]}
    formatted = normalize_formatted_rows([row])[0]
    normalized, errors = normalize_question(formatted)
    assert not errors
    assert normalized["type"] == "fill" and normalized["answer"] == row["answer"]
    envelope = make_envelope([normalized], name="填空测试", bank="school")
    path = tmp_path / "fill.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    assert load_json(path)[0]["answer"] == row["answer"]
    from importers import load_excel
    path = tmp_path / "fill.xlsx"
    write_xlsx_v2(path, envelope)
    assert load_excel(path, {})[0]["answer"] == row["answer"]
    assert normalize_question({**row, "question": "只有【2】。"})[1]
    assert normalize_question({**row, "answer": [["甲"], []]})[1]


def test_batch_dedup_conflict_recovery_and_retry(db, tmp_path):
    path = tmp_path / "batch.json"
    good = {**QUESTION, "bank": "", "subject": ""}
    path.write_text(json.dumps([good, good, {**good, "id": "broken", "question": "另一题", "answer": ""}]), encoding="utf-8")
    profile = {"defaults": {"bank": "school", "subject": "内科学"}}
    profiles(db, "来源模板", profile)
    saved = profiles(db)["来源模板"]
    result = preview(db, [str(path)], "批次", saved)
    assert (result["ready"], result["duplicates"], result["issues"]) == (1, 1, 1)
    draft = result["draft_id"]
    result = commit(db, draft)
    assert result["result"]["imported"] == 1
    assert commit(db, draft)["result"]["newly_imported"] == 0
    issue = result["items"][0]
    resume(db, draft, {str(issue["index"]): {**issue["row"], "answer": "B"}})
    assert commit(db, draft)["result"]["imported"] == 2
    assert db.conn.execute("SELECT COUNT(*) FROM question_baselines").fetchone()[0] == 2


def test_both_answers_in_conflicting_batch_are_held(db, tmp_path):
    path = tmp_path / "conflict.json"
    path.write_text(json.dumps([QUESTION, {**QUESTION, "id": "q2", "answer": "B"}]), encoding="utf-8")
    result = preview(db, [str(path)], "冲突")
    assert result["ready"] == 0 and result["issues"] == 2
    assert commit(db, result["draft_id"])["result"]["imported"] == 0


def test_one_bad_file_does_not_block_other_files(db, tmp_path):
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps([QUESTION]), encoding="utf-8")
    bad.write_text("{", encoding="utf-8")
    result = preview(db, [str(good), str(bad)], "多文件")
    assert (result["ready"], result["issues"]) == (1, 1)


def test_ai_only_receives_failed_blocks_and_does_not_lose_good_rows():
    class Client:
        calls = []
        def organize_questions(self, text, subject):
            self.calls.append(text)
            raise RuntimeError("temporary failure")
    client = Client()
    result = QuestionFormattingService(client).organize("1. 正常题\nA. 甲\nB. 乙\n答案：A\n2. 无法识别的题目", "内科学")
    assert len(client.calls) == 1 and "正常题" not in client.calls[0]
    assert len(result["rows"]) == 2 and not result["warnings"][0]
    assert result["warnings"][1]


def test_delta_contains_only_changed_fields_and_preserves_baseline(db, tmp_path):
    set_id, ids = generate(db)
    result = preflight(db, set_id)
    assert result["ready"] == 1
    item = result["questions"][0]
    patch = item["extensions"]["medqPatch"]
    assert "explanationBlocks" in patch["fields"]
    assert "answer" not in patch["fields"] and "memoryCards" not in patch["fields"]
    assert patch["baseValues"]["explanationBlocks"] == []
    output = export(db, set_id, tmp_path)
    assert len(output["files"]) == 1
    value = json.loads(open(output["files"][0], encoding="utf-8").read())
    assert value["schema"] == "medlearning.question-patch" and value["schemaVersion"] == 1
    assert value["questions"][0]["extensions"]["medqPatch"] == patch
    with db.conn:
        raw = json.loads(db.conn.execute("SELECT raw_json FROM imported_questions WHERE id=?", (ids[0],)).fetchone()[0])
        raw["answer"] = "B"
        db.conn.execute("UPDATE imported_questions SET raw_json=? WHERE id=?", (json.dumps(raw), ids[0]))
    assert preflight(db, set_id)["ready"] == 0
    assert "核心字段" in preflight(db, set_id)["issues"][0]["reasons"][0]


def test_old_rows_without_snapshot_are_not_exported_as_updates(db):
    set_id, _ = generate(db)
    with db.conn:
        db.conn.execute("DELETE FROM question_baselines")
    assert preflight(db, set_id)["ready"] == 0
    assert "快照" in preflight(db, set_id)["issues"][0]["reasons"][0]


def test_append_does_not_create_false_baselines_for_old_generated_rows(db):
    set_id, ids = generate(db)
    with db.conn:
        db.conn.execute("DELETE FROM question_baselines")
    db.append_questions_to_set(set_id, [{**QUESTION, "id": "added"}])
    assert db.conn.execute("SELECT COUNT(*) FROM question_baselines WHERE question_pk=?", (ids[0],)).fetchone()[0] == 0


def test_python_delta_is_accepted_by_web_normalizer(db, tmp_path):
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    module = Path(__file__).resolve().parent.parent / "web-question-bank/lib/question-patch.js"
    if not node or not module.is_file():
        pytest.skip("需要相邻网页项目与 Node 才能执行跨语言协议测试")
    set_id, _ = generate(db)
    result = export(db, set_id, tmp_path)
    baseline = tmp_path / "base.json"
    baseline.write_text(json.dumps(QUESTION), encoding="utf-8")
    script = r"""
const fs = require('fs'), p = require(process.argv[1]);
const current = JSON.parse(fs.readFileSync(process.argv[2]));
const envelope = p.validatePatchEnvelope(JSON.parse(fs.readFileSync(process.argv[3])));
const plan = p.planPatchMerge(envelope, [current]);
if (plan.conflicts.length || plan.missing.length || plan.rejected.length || plan.updates.length !== 1) {
    throw Error(JSON.stringify(plan));
}
const updated = p.applyPatchUpdate(current, plan.updates[0]);
if (updated.answer !== current.answer || !updated.briefExplanation) throw Error('roundtrip failed');
const replay = p.planPatchMerge(envelope, [updated]);
if (replay.updates.length || replay.conflicts.length || replay.alreadyApplied.length !== 1) {
    throw Error('replay failed: ' + JSON.stringify(replay));
}
"""
    subprocess.run([node, "-e", script, str(module), str(baseline), result["files"][0]], check=True, capture_output=True, text=True)
