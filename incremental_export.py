"""Export changed learning fields against an immutable import-time baseline."""
import json
import re
from datetime import datetime
from pathlib import Path

from importers import normalize_question
from question_format_v2 import make_envelope, normalize_question_v2

FIELDS = ("briefExplanation", "explanationBlocks", "explanation", "optionExplanations",
          "reasoningSteps", "studyPoints", "memoryCards", "memoryCardMeta", "frequencyLevel",
          "knowledgePoints", "knowledgePoint", "tags", "suggestedTags", "mnemonic", "explanationMeta")
CORE = ("id", "bank", "type", "question", "caseInfo", "options", "answer", "subject", "system")


def schema(db):
    db.conn.execute("""CREATE TABLE IF NOT EXISTS question_baselines (
        question_pk INTEGER PRIMARY KEY REFERENCES imported_questions(id) ON DELETE CASCADE,
        raw_json TEXT NOT NULL)""")


def capture(db, set_id, after_pk=0):
    schema(db)
    db.conn.execute("""INSERT OR IGNORE INTO question_baselines(question_pk,raw_json)
        SELECT id,raw_json FROM imported_questions WHERE set_id=? AND id>?""", (set_id, after_pk))


def preflight(db, set_id, incremental=True):
    from auto_review import run
    auto = run(db, set_id)
    schema(db)
    issues, updates, unchanged = [], [], 0
    rows = db.approved_questions(set_id)
    for source in rows:
        row = dict(source)
        raw = json.loads(row["raw_json"])
        raw["explanation"] = row["explanation"]
        raw["explanationMeta"] = {**(raw.get("explanationMeta") or {}),
            "mode": row["generation_mode"], "score": row["match_score"],
            "evidence": json.loads(row["evidence_json"] or "[]"), "reviewedAt": row["reviewed_at"]}
        current, errors = normalize_question(raw)
        if current.get("bank") not in {"school", "kaoyan"}:
            errors.append("缺少有效题库")
        if errors:
            issues.append({"id": raw.get("id"), "reasons": errors})
            continue
        current = normalize_question_v2(current)
        if not incremental:
            updates.append(current)
            continue
        base_row = db.conn.execute("SELECT raw_json FROM question_baselines WHERE question_pk=?", (row["id"],)).fetchone()
        if not base_row:
            issues.append({"id": current["id"], "reasons": ["没有导入时快照，请重新从网页导出并导入后生成；也可使用完整导出"]})
            continue
        baseline = normalize_question_v2(json.loads(base_row["raw_json"]))
        changed_core = [key for key in CORE if current.get(key) != baseline.get(key)]
        if changed_core:
            issues.append({"id": current["id"], "reasons": ["核心字段发生变化：" + "、".join(changed_core)]})
            continue
        changed = [key for key in FIELDS if current.get(key) != baseline.get(key)]
        if not changed:
            unchanged += 1
            continue
        current["extensions"] = {**(current.get("extensions") or {}), "medqPatch": {
            "version": 1, "fields": changed, "baseValues": {key: baseline.get(key) for key in changed}}}
        updates.append(current)
    pending = db.conn.execute("SELECT COUNT(*) FROM imported_questions WHERE set_id=? AND review_status!='approved'", (set_id,)).fetchone()[0]
    return {"automatic_review": auto, "ready": len(updates), "unchanged": unchanged,
            "pending": pending, "issues": issues, "questions": updates}


def export(db, set_id, output_dir):
    from services import ExportService
    selected = next((row for row in db.list_question_sets() if row["id"] == set_id), None)
    if not selected:
        raise ValueError("题目集不存在")
    result = preflight(db, set_id)
    updates = result.pop("questions")
    target = Path(output_dir)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    name = re.sub(r'[<>:"/\\|?*]+', "_", selected["name"]).strip() or "题目集"
    files = []
    for bank in sorted({row["bank"] for row in updates}):
        questions = [row for row in updates if row["bank"] == bank]
        path = target / f"{name}-{bank}-{stamp}-增量.json"
        envelope = make_envelope(questions, name=name, bank=bank, source="med-explain-incremental")
        # An older server must reject this file, not treat empty unselected fields
        # as a full replacement. Question objects still use the v2 field contract.
        envelope.update(schema="medlearning.question-patch", schemaVersion=1)
        ExportService._atomic_json(path, envelope)
        db.add_export_record(set_id, "incremental_v2", str(path), len(questions))
        files.append(str(path))
    report = target / f"{name}-{stamp}-导出检查.json"
    ExportService._atomic_json(report, result)
    return {**result, "files": files, "report": str(report)}
