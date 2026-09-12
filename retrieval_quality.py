"""Deterministic hybrid-scoring calibration and offline retrieval metrics."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Iterable


def hybrid_score(cosine: float, rerank_score: float, alpha: float) -> float:
    return float(alpha) * ((float(cosine) + 1.0) / 2.0) + (1.0 - float(alpha)) * float(rerank_score)


def _metrics(rows: list[dict], alpha: float, threshold: float) -> dict:
    tp = fp = fn = 0
    recalls, reciprocal = [], []
    per_grade: dict[str, list[int]] = {}
    grade_confusion: dict[str, list[int]] = {}
    for row in rows:
        expected = {str(item) for item in row.get("relevant_chunk_ids") or []}
        ranked = sorted(
            row.get("candidates") or [],
            key=lambda item: hybrid_score(item["cosine"], item["rerank_score"], alpha),
            reverse=True,
        )
        accepted = [item for item in ranked if hybrid_score(item["cosine"], item["rerank_score"], alpha) >= threshold]
        predicted = {str(item["chunk_id"]) for item in accepted[:3]}
        has_truth = bool(expected)
        has_prediction = bool(predicted)
        tp += int(has_truth and bool(predicted & expected))
        fp += int(has_prediction and not bool(predicted & expected))
        fn += int(has_truth and not bool(predicted & expected))
        recall = int(bool(expected & predicted)) if expected else int(not predicted)
        recalls.append(recall)
        grade = str(row.get("grade") or "unknown").upper()
        per_grade.setdefault(grade, []).append(recall)
        confusion = grade_confusion.setdefault(grade, [0, 0, 0])
        confusion[0] += int(has_truth and bool(predicted & expected))
        confusion[1] += int(has_prediction and not bool(predicted & expected))
        confusion[2] += int(has_truth and not bool(predicted & expected))
        rank = next((index for index, item in enumerate(accepted[:3], 1) if str(item["chunk_id"]) in expected), 0)
        reciprocal.append(1.0 / rank if rank else 0.0)
    precision = tp / max(1, tp + fp)
    recall_binary = tp / max(1, tp + fn)
    f1_values = []
    for grade_tp, grade_fp, grade_fn in grade_confusion.values():
        grade_precision = grade_tp / max(1, grade_tp + grade_fp)
        grade_recall = grade_tp / max(1, grade_tp + grade_fn)
        f1_values.append(2 * grade_precision * grade_recall / max(1e-12, grade_precision + grade_recall))
    f1 = sum(f1_values) / max(1, len(f1_values))
    macro_recall = sum(sum(values) / len(values) for values in per_grade.values()) / max(1, len(per_grade))
    return {
        "macro_f1": f1,
        "macro_grade_recall": macro_recall,
        "recall_at_3": sum(recalls) / max(1, len(recalls)),
        "mrr_at_3": sum(reciprocal) / max(1, len(reciprocal)),
        "false_match_rate": fp / max(1, fp + tp),
    }


@dataclass(frozen=True)
class CalibrationResult:
    enabled: bool
    alpha: float | None
    threshold: float | None
    sample_count: int
    grade_counts: dict[str, int]
    metrics: dict
    reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def calibrate_hybrid(rows: Iterable[dict], baseline_false_match_rate: float) -> CalibrationResult:
    samples = list(rows)
    grade_counts = {grade: sum(str(row.get("grade") or "").upper() == grade for row in samples) for grade in "ABC"}
    if any(grade_counts[grade] < 30 for grade in "ABC"):
        return CalibrationResult(
            False, None, None, len(samples), grade_counts, {},
            "each evidence grade needs at least 30 reviewed questions",
        )
    best: tuple[tuple[float, float, float, float], float, float, dict] | None = None
    for alpha_step in range(21):
        alpha = alpha_step * 0.05
        for threshold_step in range(30, 91):
            threshold = threshold_step * 0.01
            metrics = _metrics(samples, alpha, threshold)
            if metrics["false_match_rate"] > float(baseline_false_match_rate) + 1e-12:
                continue
            objective = (
                metrics["macro_f1"], metrics["recall_at_3"], metrics["mrr_at_3"],
                -metrics["false_match_rate"],
            )
            if best is None or objective > best[0]:
                best = (objective, alpha, threshold, metrics)
    if best is None:
        return CalibrationResult(False, None, None, len(samples), grade_counts, {}, "no safe parameter pair")
    return CalibrationResult(True, best[1], best[2], len(samples), grade_counts, best[3])
