"""Stratify reviewed questions, calibrate hybrid scoring, and persist safe settings."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from db_manager import DatabaseManager
from retrieval_quality import calibrate_hybrid


def baseline_false_match_rate(samples: list[dict], threshold: float) -> float:
    false_matches = predictions = 0
    for row in samples:
        expected = {str(item) for item in row.get("relevant_chunk_ids") or []}
        accepted = sorted(
            (item for item in row.get("candidates") or [] if float(item.get("cosine") or 0) >= threshold),
            key=lambda item: float(item.get("cosine") or 0), reverse=True,
        )[:3]
        predicted = {str(item.get("chunk_id")) for item in accepted}
        predictions += int(bool(predicted))
        false_matches += int(bool(predicted) and not bool(predicted & expected))
    return false_matches / max(1, predictions)


def reviewed_samples(database: DatabaseManager) -> list[dict]:
    rows = database.conn.execute(
        "SELECT id,subject,question_type,raw_json,evidence_json FROM imported_questions "
        "WHERE review_status='approved' ORDER BY id"
    ).fetchall()
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        raw = json.loads(row["raw_json"] or "{}")
        extensions = raw.get("extensions") if isinstance(raw.get("extensions"), dict) else {}
        trace = extensions.get("retrievalTrace") if isinstance(extensions.get("retrievalTrace"), dict) else {}
        scores = trace.get("scores") if isinstance(trace.get("scores"), list) else []
        grade = str((raw.get("explanationMeta") or {}).get("evidenceGrade") or "").upper()
        if grade not in "ABC" or not scores:
            continue
        evidence = json.loads(row["evidence_json"] or "[]")
        groups[grade].append({
            "id": row["id"], "grade": grade, "subject": row["subject"],
            "question_type": row["question_type"],
            "relevant_chunk_ids": [str(item["chunk_id"]) for item in evidence if item.get("chunk_id") is not None],
            "candidates": [
                {"chunk_id": item.get("chunkId"), "cosine": item.get("embedding", 0), "rerank_score": item.get("rerank", 0)}
                for item in scores if item.get("chunkId") is not None and item.get("rerank") is not None
            ],
        })
    selected = []
    for grade in "ABC":
        strata: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for item in groups[grade]:
            strata[(item["subject"], item["question_type"])].append(item)
        grade_count = 0
        while strata and grade_count < 80:
            for key in sorted(list(strata)):
                selected.append(strata[key].pop(0)); grade_count += 1
                if not strata[key]: del strata[key]
                if grade_count >= 80: break
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--baseline-false-match-rate", type=float)
    parser.add_argument("--output", default=str(ROOT / "work" / "evals" / "calibration.json"))
    args = parser.parse_args()
    with DatabaseManager(args.database) as database:
        samples = reviewed_samples(database)
        baseline = args.baseline_false_match_rate
        if baseline is None:
            threshold = float(database.get_setting("similarity_threshold", "0.5") or 0.5)
            baseline = baseline_false_match_rate(samples, threshold)
        result = calibrate_hybrid(samples, baseline)
        if result.enabled:
            database.set_setting("rerank_calibrated_alpha", f"{result.alpha:.2f}")
            database.set_setting("rerank_calibrated_threshold", f"{result.threshold:.2f}")
    report = {**result.as_dict(), "baselineFalseMatchRate": baseline}
    target = Path(args.output); target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(target)


if __name__ == "__main__": main()
