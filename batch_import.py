"""Saved source profiles, resumable exception queues and deduplicated batch ingestion."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from importers import (auto_mapping, excel_headers, load_excel, load_json,
                       normalize_question, question_prompt)

DEFAULT_FIELDS = {"bank", "subject", "system", "questionSource", "type", "difficulty"}
CORE = ("bank", "type", "caseInfo", "question", "options")
CONTENT = CORE + ("answer", "subject", "system", "explanation", "explanationBlocks",
                  "briefExplanation", "knowledgePoints", "tags", "suggestedTags", "mnemonic",
                  "studyPoints", "memoryCards", "optionExplanations", "reasoningSteps", "year",
                  "difficulty", "questionSource", "memoryCardMeta", "frequencyLevel", "explanationMeta", "extensions")


def schema(db):
    db.conn.execute("""CREATE TABLE IF NOT EXISTS import_drafts (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, items_json TEXT NOT NULL,
        result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")


def canonical(value):
    if isinstance(value, str):
        return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    return value


def fingerprint(row, fields):
    values = {key: row.get(key) for key in fields}
    if "extensions" in values and isinstance(values["extensions"], dict):
        values["extensions"] = {key: value for key, value in values["extensions"].items() if key != "sourceBlock"}
    return hashlib.sha256(json.dumps(canonical(values),
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def profiles(db, name="", profile=None):
    saved = json.loads(db.get_setting("imports.profiles", "{}"))
    if name:
        if profile is None:
            saved.pop(name, None)
        else:
            saved[name] = {"mapping": dict(profile.get("mapping") or {}),
                           "defaults": {k: v for k, v in (profile.get("defaults") or {}).items() if k in DEFAULT_FIELDS}}
        db.set_setting("imports.profiles", json.dumps(saved, ensure_ascii=False))
    return saved


def classify(db, items, progress=None):
    by_id, by_core = defaultdict(list), defaultdict(list)
    entries_by_row = {}
    def add(row):
        by_id[(row.get("bank"), row["id"])].append(row)
        by_core[fingerprint(row, CORE)].append(row)
    for number, source in enumerate(db.conn.execute("SELECT raw_json FROM imported_questions")):
        if progress and number % 100 == 0:
            progress("读取已有题目", number, 0)
        raw = json.loads(source["raw_json"])
        add(normalize_question(raw)[0])
    result = []
    for index, item in enumerate(items):
        if progress and index % 25 == 0:
            progress("校验与去重", index, len(items))
        raw = item.get("row")
        entry = {**item, "index": index}
        if not isinstance(raw, dict):
            entry.update(status="issue", reasons=[item.get("error") or "题目不是对象"])
            result.append(entry)
            continue
        row, errors = normalize_question(raw, index + 1)
        if row.get("bank") not in {"school", "kaoyan"}:
            errors.append("请选择题库：校内或考研")
        entry["row"] = row
        candidates = by_id.get((row.get("bank"), row["id"]), []) + by_core.get(fingerprint(row, CORE), [])
        if errors:
            entry.update(status="issue", reasons=errors)
        elif any(fingerprint(row, CONTENT) != fingerprint(other, CONTENT) for other in candidates):
            conflict = "答案冲突" if any(other.get("answer") != row.get("answer") for other in candidates) else "ID 或题干重复，但其他字段存在差异"
            entry.update(status="issue", reasons=[conflict])
            for other in candidates:
                previous = entries_by_row.get(id(other))
                if previous is not None:
                    previous.update(status="issue", reasons=["同一批次内存在冲突：" + conflict])
            add(row)
            entries_by_row[id(row)] = entry
        elif candidates:
            entry.update(status="duplicate", reasons=["内容完全重复，自动跳过"])
        else:
            entry.update(status="ready", reasons=[])
            add(row)
            entries_by_row[id(row)] = entry
        result.append(entry)
    return result


def summary(draft_id, items, result=None):
    counts = Counter(item["status"] for item in items)
    return {"draft_id": draft_id, "total": len(items), "ready": counts["ready"],
            "duplicates": counts["duplicate"], "issues": counts["issue"],
            "items": [item for item in items if item["status"] == "issue"],
            "result": result or {}}


def preview(db, paths, name, profile=None, progress=None):
    if not name.strip():
        raise ValueError("请填写批次名称")
    if not paths:
        raise ValueError("请选择文件")
    profile = profile or {}
    defaults = {key: value for key, value in (profile.get("defaults") or {}).items() if key in DEFAULT_FIELDS}
    items = []
    for file_index, source_path in enumerate(dict.fromkeys(paths)):
        if progress:
            progress("读取文件", file_index, len(paths))
        source = Path(source_path)
        try:
            if source.suffix.lower() == ".json":
                rows = load_json(source, validate=False)
            elif source.suffix.lower() in {".xlsx", ".xlsm"}:
                mapping = {key: value for key, value in (profile.get("mapping") or {}).items() if value} or auto_mapping(excel_headers(source))
                rows = load_excel(source, mapping, validate=False)
            else:
                raise ValueError("批量文件导入支持 JSON、XLSX、XLSM；原始文档请使用智能整理")
            for number, raw in enumerate(rows, 1):
                if isinstance(raw, dict):
                    raw = dict(raw)
                    for key, value in defaults.items():
                        if not raw.get(key):
                            raw[key] = value
                    if not raw.get("id"):
                        raw["id"] = "import_" + fingerprint(raw, CORE + ("answer",))[:24]
                items.append({"source": str(source), "number": number, "row": raw})
        except Exception as exc:
            items.append({"source": str(source), "number": 0, "row": None, "error": str(exc)})
    schema(db)
    draft_id = uuid.uuid4().hex
    checked = classify(db, items, progress)
    if progress:
        progress("保存批次", len(items), len(items))
    with db.conn:
        db.conn.execute("INSERT INTO import_drafts(id,name,items_json) VALUES(?,?,?)",
                        (draft_id, name.strip(), json.dumps(checked, ensure_ascii=False)))
    return summary(draft_id, checked)


def resume(db, draft_id, corrections=None, progress=None):
    schema(db)
    draft = db.conn.execute("SELECT * FROM import_drafts WHERE id=?", (draft_id,)).fetchone()
    if not draft:
        raise ValueError("批次不存在")
    items = json.loads(draft["items_json"])
    for key, value in (corrections or {}).items():
        index = int(key)
        if index < 0 or index >= len(items) or items[index]["status"] != "issue":
            raise ValueError("只能修正异常记录")
        items[index]["row"] = value
    checked = classify(db, items, progress)
    if progress:
        progress("保存批次", len(items), len(items))
    with db.conn:
        db.conn.execute("UPDATE import_drafts SET items_json=? WHERE id=?", (json.dumps(checked, ensure_ascii=False), draft_id))
    return summary(draft_id, checked, json.loads(draft["result_json"]))


def commit(db, draft_id, progress=None):
    from incremental_export import capture
    schema(db)
    imported, sets = 0, []
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        draft = db.conn.execute("SELECT * FROM import_drafts WHERE id=?", (draft_id,)).fetchone()
        if not draft:
            raise ValueError("批次不存在")
        # Validation and inserts share a write lock; stale previews cannot import twice.
        items = classify(db, json.loads(draft["items_json"]), progress)
        if progress:
            progress("提交入库", 0, len(items))
        groups = defaultdict(list)
        for item in items:
            if item["status"] == "ready":
                groups[item["row"]["bank"]].append(item["row"])
        for bank, rows in groups.items():
            set_id = str(uuid.uuid4())
            db.conn.execute("INSERT INTO question_sets(id,name,source_path,source_type,question_count) VALUES(?,?,?,?,?)",
                            (set_id, f"{draft['name']} · {bank}", f"batch://{draft_id}", "batch", len(rows)))
            db.conn.executemany("""INSERT INTO imported_questions
                (set_id,external_id,subject,question_type,prompt_text,raw_json) VALUES(?,?,?,?,?,?)""",
                [(set_id, row["id"], row["subject"], row["type"], question_prompt(row), json.dumps(row, ensure_ascii=False)) for row in rows])
            capture(db, set_id)
            imported += len(rows)
            sets.append(set_id)
        previous = json.loads(draft["result_json"])
        result = {"imported": previous.get("imported", 0) + imported,
                  "set_ids": previous.get("set_ids", []) + sets, "new_set_ids": sets, "newly_imported": imported}
        db.conn.execute("UPDATE import_drafts SET result_json=? WHERE id=?", (json.dumps(result), draft_id))
    for set_id in sets:
        db.sync_question_tags(set_id)
    return resume(db, draft_id)
