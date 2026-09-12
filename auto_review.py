"""Deterministic exception-only review. Decisions are local, versioned and auditable."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone

from importers import normalize_question

POLICY_VERSION = 1


def ensure_schema(database):
    database.conn.execute("""CREATE TABLE IF NOT EXISTS automatic_review (
        question_pk INTEGER PRIMARY KEY REFERENCES imported_questions(id) ON DELETE CASCADE,
        fingerprint TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
        policy_version INTEGER NOT NULL, checked_at TEXT NOT NULL)""")


def exception_reason(database, row):
    try:
        reason = database._quick_review_reason(row, 0.75)
        if reason:
            return reason
        raw = json.loads(row["raw_json"])
        if not math.isfinite(float(row.get("match_score") or 0)):
            return "匹配分数异常"
        if str((raw.get("explanationMeta") or {}).get("evidenceGrade") or "").upper() != "A":
            return "需要明确的 A 级教材证据"
        _, errors = normalize_question(raw)
        if errors:
            return "；".join(errors)
        if raw.get("bank") not in {"school", "kaoyan"}:
            return "未指定有效题库"
        if (raw.get("extensions") or {}).get("manualReviewRequired"):
            return "人工标记待复核"
        if raw.get("type") in {"A1", "A2", "A3", "multiple"}:
            answer = str(raw.get("answer") or "").upper()
            allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"[:len(raw.get("options") or [])])
            if not answer or not set(answer).issubset(allowed):
                return "答案与选项不匹配"
            if raw.get("type") != "multiple" and len(answer) != 1:
                return "单选题答案数量异常"
        if raw.get("type") == "judge" and raw.get("answer") not in {"A", "B", "对", "错", "正确", "错误", "√", "×"}:
            return "判断题答案无效"
        for block in raw.get("explanationBlocks") or []:
            kind = block.get("type")
            if kind in {"paragraph", "callout"} and not str(block.get("text") or "").strip():
                return "解析含空区块"
            if kind == "list" and not block.get("items"):
                return "解析列表为空"
            if kind == "table":
                columns, rows = block.get("columns") or [], block.get("rows") or []
                if not columns or not rows or any(len(item) != len(columns) for item in rows):
                    return "解析表格不完整"
        return ""
    except (TypeError, ValueError, AttributeError, OverflowError):
        return "结构或证据数据异常"


def run(database, set_id=""):
    ensure_schema(database)
    enabled = database.get_setting("review.auto_enabled", "1") != "0"
    result = {"enabled": enabled, "approved": 0, "pending": 0, "reasons": {}}
    if not enabled:
        return result
    excluded = Counter()
    # The write lock covers both validation and state change; another worker cannot
    # replace an explanation between checking it and approving it.
    with database.conn:
        database.conn.execute("BEGIN IMMEDIATE")
        rows = database.conn.execute("""SELECT * FROM imported_questions
            WHERE pipeline_status='generated' AND review_status='pending'
              AND (?='' OR set_id=?) ORDER BY id""", (set_id, set_id)).fetchall()
        now = datetime.now(timezone.utc).isoformat()
        for source in rows:
            row = dict(source)
            fingerprint = hashlib.sha256(json.dumps(
                [row.get(key) for key in ("raw_json", "evidence_json", "explanation", "match_score", "generation_mode", "error_message")],
                ensure_ascii=False).encode()).hexdigest()
            previous = database.conn.execute("SELECT * FROM automatic_review WHERE question_pk=?", (row["id"],)).fetchone()
            if previous and previous["fingerprint"] == fingerprint and previous["policy_version"] == POLICY_VERSION:
                # A manual return to pending must not be approved again on opening the page.
                reason = previous["reason"] if previous["decision"] == "pending" else "人工退回待复核"
            else:
                reason = exception_reason(database, row)
            decision = "pending" if reason else "approved"
            if reason:
                excluded[reason] += 1
            else:
                database.conn.execute("""UPDATE imported_questions SET review_status='approved',
                    reviewed_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""", (now, row["id"]))
                database.conn.execute("INSERT INTO review_actions(question_pk,action,note) VALUES(?, 'approved', ?)",
                    (row["id"], f"自动校验 v{POLICY_VERSION}：题目结构、答案选项、解析完整性、A 级教材证据及相似度≥0.75 均通过"))
                result["approved"] += 1
            database.conn.execute("""INSERT INTO automatic_review VALUES(?,?,?,?,?,?)
                ON CONFLICT(question_pk) DO UPDATE SET fingerprint=excluded.fingerprint,
                decision=excluded.decision, reason=excluded.reason,
                policy_version=excluded.policy_version, checked_at=excluded.checked_at""",
                (row["id"], fingerprint, decision, reason or "自动校验通过", POLICY_VERSION, now))
    result["pending"] = sum(excluded.values())
    result["reasons"] = dict(excluded)
    return result


def reasons_for(database, ids):
    ensure_schema(database)
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    return {int(row["question_pk"]): row["reason"] for row in database.conn.execute(
        f"SELECT question_pk,reason FROM automatic_review WHERE question_pk IN ({marks})", ids)}
