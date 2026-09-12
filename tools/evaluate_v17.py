"""Deterministic offline evaluation with an explicit opt-in DeepEval judge."""

from __future__ import annotations

import argparse, json, os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from retrieval_quality import hybrid_score
from evals.citation_check import valid_page, verify_pdf_citations


def offline(rows: list[dict], alpha: float, threshold: float) -> dict:
    hits = reciprocal = false_matches = predictions = page_errors = schema_errors = 0
    grade_rules = {grade: {"total": 0, "passed": 0} for grade in "ABC"}
    for row in rows:
        try:
            grade = str(row["grade"]).upper(); expected = {str(x) for x in row["relevant_chunk_ids"]}
            candidates = sorted(row["candidates"], key=lambda x: hybrid_score(x["cosine"], x["rerank_score"], alpha), reverse=True)
        except (KeyError, TypeError, ValueError):
            schema_errors += 1; continue
        accepted = [x for x in candidates if hybrid_score(x["cosine"], x["rerank_score"], alpha) >= threshold][:3]
        predicted = {str(x["chunk_id"]) for x in accepted}
        rank = next((i for i, x in enumerate(accepted, 1) if str(x["chunk_id"]) in expected), 0)
        hits += int(bool(predicted & expected)); reciprocal += 1 / rank if rank else 0
        predictions += int(bool(predicted)); false_matches += int(bool(predicted) and not bool(predicted & expected))
        page_errors += sum(not valid_page(x.get("pdf_page")) for x in accepted if grade in {"A", "B"})
        grade_rules.setdefault(grade, {"total": 0, "passed": 0}); grade_rules[grade]["total"] += 1
        grade_rules[grade]["passed"] += int((grade in {"A", "B"} and bool(expected)) or (grade == "C" and not expected))
    return {"samples": len(rows), "recallAt3": hits / max(1, len(rows)), "mrrAt3": reciprocal / max(1, len(rows)), "falseMatchRate": false_matches / max(1, predictions), "citationPageErrors": page_errors, "schemaErrors": schema_errors, "gradeRules": grade_rules}


def judge(rows: list[dict], model_name: str) -> dict:
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key: return {"status": "skipped", "reason": "DEEPSEEK_API_KEY is not configured"}
    try:
        from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric, GEval
        from deepeval.models.base_model import DeepEvalBaseLLM
        from deepeval.test_case import LLMTestCase, LLMTestCaseParams
        from openai import OpenAI
    except ImportError as exc: return {"status": "skipped", "reason": f"evaluation dependencies unavailable: {exc}"}
    os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
    class DeepSeek(DeepEvalBaseLLM):
        def __init__(self): self.client = OpenAI(api_key=key, base_url="https://api.deepseek.com/v1", max_retries=0); self.tokens = 0; self.prompt_tokens = 0; self.completion_tokens = 0
        def load_model(self): return self.client
        def get_model_name(self): return model_name
        def generate(self, prompt, schema=None):
            response = self.client.chat.completions.create(model=model_name, messages=[{"role":"user","content":prompt}], temperature=0, response_format={"type":"json_object"})
            self.tokens += int(getattr(response.usage, "total_tokens", 0) or 0); self.prompt_tokens += int(getattr(response.usage, "prompt_tokens", 0) or 0); self.completion_tokens += int(getattr(response.usage, "completion_tokens", 0) or 0); text = response.choices[0].message.content or "{}"
            return schema.model_validate_json(text) if schema else text
        async def a_generate(self, prompt, schema=None): return self.generate(prompt, schema)
    model = DeepSeek(); metrics = [FaithfulnessMetric(model=model, async_mode=False), AnswerRelevancyMetric(model=model, async_mode=False), GEval(name="EvidenceCoverage", criteria="The answer covers claims directly supported by the supplied evidence.", evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT, LLMTestCaseParams.RETRIEVAL_CONTEXT], model=model, async_mode=False)]
    scores = {metric.__class__.__name__: [] for metric in metrics}; started = time.monotonic()
    for row in rows:
        if not row.get("answer"): continue
        case = LLMTestCase(input=str(row.get("question") or ""), actual_output=str(row["answer"]), retrieval_context=[str(x) for x in row.get("evidence") or []])
        for metric in metrics: metric.measure(case); scores[metric.__class__.__name__].append(float(metric.score or 0))
    rates = {"deepseek-v4-flash": (.14, .28), "deepseek-v4-pro": (.435, .87)}; input_rate, output_rate = rates.get(model_name, rates["deepseek-v4-flash"]); estimated_cost = model.prompt_tokens * input_rate / 1_000_000 + model.completion_tokens * output_rate / 1_000_000
    return {"status": "completed", "model": model_name, "scores": {k: sum(v) / max(1, len(v)) for k, v in scores.items()}, "tokens": {"prompt": model.prompt_tokens, "completion": model.completion_tokens, "total": model.tokens}, "estimatedCostUsd": round(estimated_cost, 6), "pricingUsdPerMillion": {"inputCacheMiss": input_rate, "output": output_rate}, "elapsedSeconds": round(time.monotonic() - started, 3)}


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("dataset", type=Path); parser.add_argument("--alpha", type=float, default=.5); parser.add_argument("--threshold", type=float, default=.5); parser.add_argument("--baseline-threshold", type=float, default=.5); parser.add_argument("--baseline-faithfulness", type=float); parser.add_argument("--llm-judge", action="store_true"); parser.add_argument("--judge-model", default="deepseek-v4-flash"); parser.add_argument("--output", type=Path, default=ROOT / "work" / "evals" / "report.json"); parser.add_argument("--verify-pdfs", action="store_true", help="Read local source_path PDFs and check physical page bounds"); args = parser.parse_args()
    rows = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    current = offline(rows, args.alpha, args.threshold)
    baseline = offline(rows, 1.0, (args.baseline_threshold + 1.0) / 2.0)
    recall_delta = current["recallAt3"] - baseline["recallAt3"]; mrr_delta = current["mrrAt3"] - baseline["mrrAt3"]
    quality_gate = current["falseMatchRate"] <= baseline["falseMatchRate"] and ((recall_delta >= .03 and mrr_delta >= 0) or (mrr_delta >= .03 and recall_delta >= 0))
    report = {"offline": current, "baseline": baseline, "qualityGate": {"passed": quality_gate, "recallDelta": recall_delta, "mrrDelta": mrr_delta}, "judge": judge(rows, args.judge_model) if args.llm_judge else {"status": "not_requested"}}
    if args.verify_pdfs:
        report["pdfCitationAudit"] = verify_pdf_citations(rows)
        report["qualityGate"]["passed"] = report["qualityGate"]["passed"] and report["pdfCitationAudit"]["errors"] == 0 and report["pdfCitationAudit"]["skippedMissingPath"] == 0 and report["pdfCitationAudit"]["checked"] > 0
    if args.baseline_faithfulness is not None and report["judge"].get("status") == "completed":
        faithfulness = float(report["judge"].get("scores", {}).get("FaithfulnessMetric", 0))
        report["qualityGate"]["faithfulnessDelta"] = faithfulness - args.baseline_faithfulness
        report["qualityGate"]["faithfulnessPassed"] = faithfulness >= args.baseline_faithfulness - .02
        report["qualityGate"]["passed"] = report["qualityGate"]["passed"] and report["qualityGate"]["faithfulnessPassed"]
    args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"); print(args.output)


if __name__ == "__main__": main()
