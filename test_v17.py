from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

from db_manager import DatabaseManager
from pdf_parser import PDFParser
from reranker_client import DashScopeReranker, RerankError, RerankResult
from retrieval_quality import calibrate_hybrid, hybrid_score
from services import GenerationService
from structured_models import ExplanationPackageResponse


class RerankerTests(unittest.TestCase):
    def test_normal_response_and_retryable_429(self):
        calls = []
        def caller(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {"status_code": 429, "message": "limited"}
            return {"status_code": 200, "request_id": "r1", "output": {"results": [
                {"index": 1, "relevance_score": .9}, {"index": 0, "relevance_score": .2},
            ]}, "usage": {"total_tokens": 17}}
        result = DashScopeReranker("key", caller=caller).rerank("q", ["a", "b"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.scores, {1: .9, 0: .2})
        self.assertEqual(result.tokens, 17)

    def test_duplicate_index_is_rejected(self):
        caller = lambda **_: {"status_code": 200, "output": {"results": [
            {"index": 0, "relevance_score": .8}, {"index": 0, "relevance_score": .7},
        ]}}
        with self.assertRaises(RerankError):
            DashScopeReranker("key", caller=caller).rerank("q", ["a"])


class HybridTests(unittest.TestCase):
    def test_formula_and_safe_calibration(self):
        self.assertAlmostEqual(hybrid_score(.6, .8, .25), .8)
        rows = []
        for grade in "ABC":
            for index in range(30):
                rows.append({"grade": grade, "relevant_chunk_ids": [f"{grade}{index}"], "candidates": [
                    {"chunk_id": f"{grade}{index}", "cosine": .8, "rerank_score": .9},
                    {"chunk_id": f"x{grade}{index}", "cosine": .1, "rerank_score": .1},
                ]})
        result = calibrate_hybrid(rows, 0)
        self.assertTrue(result.enabled)
        self.assertLessEqual(result.metrics["false_match_rate"], 0)

    def test_insufficient_grade_disables_gate(self):
        result = calibrate_hybrid([], 0)
        self.assertFalse(result.enabled)
        self.assertIsNone(result.threshold)


class StructuredAndParserTests(unittest.TestCase):
    def test_unknown_output_field_is_forbidden(self):
        value = {"explanationBlocks": [{"section": "analysis", "type": "paragraph", "title": "t", "text": "x"}], "briefExplanation": "b", "tags": ["t"], "unknown": 1}
        with self.assertRaises(ValidationError):
            ExplanationPackageResponse.model_validate(value)

    def test_structured_chunks_never_cross_pages_and_keep_metadata(self):
        parser = PDFParser(20, 5)
        chunks = parser.chunks_from_pages([
            {"page_number": 1, "text": "# 标题\n第一段文字第一段文字第一段文字", "extraction_method": "docling"},
            {"page_number": 2, "text": "|列|值|\n|---|---|\n|甲|乙|", "extraction_method": "docling"},
        ])
        self.assertEqual({item["page_number"] for item in chunks}, {1, 2})
        self.assertTrue(all(item["metadata"]["parserVersion"] == "cloud-text-v1" for item in chunks))
        self.assertIn("table", {item["metadata"]["blockType"] for item in chunks})

    def test_metadata_json_migration_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            with DatabaseManager(str(Path(folder) / "db.sqlite")) as database:
                columns = {row["name"] for row in database.conn.execute("PRAGMA table_info(chunks_v2)")}
                self.assertIn("metadata_json", columns)
                self.assertEqual(database.conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], "8")


class RetrievalFinalizationTests(unittest.TestCase):
    def test_one_rerank_call_composite_trace_and_book_diversity(self):
        class FakeReranker:
            def __init__(self): self.calls = 0
            def rerank(self, query, documents):
                self.calls += 1
                return RerankResult({0: .1, 1: .95, 2: .8}, "qwen3-rerank", "req", 9, 12)
        config = {"retrieval": {"similarity_threshold": .5, "final_top_k": 3, "rerank": {"enabled": True, "candidate_count": 20, "calibrated_alpha": .5, "calibrated_threshold": .55}}, "memory_guard": {"max_memory_mb": 2048}}
        reranker = FakeReranker()
        service = GenerationService(SimpleNamespace(), config, None, None, reranker=reranker)
        result = service._finalize_retrieval({"prompt_text": "题干", "raw": {"question": "题干", "answer": "A", "options": ["A. 正确"]}}, {"matched_chunks": [
            {"chunk_id": 1, "text": "a", "textbook": "书甲", "score": .9},
            {"chunk_id": 2, "text": "b", "textbook": "书乙", "score": .7},
            {"chunk_id": 3, "text": "c", "textbook": "书乙", "score": .6},
        ]})
        self.assertEqual(reranker.calls, 1)
        self.assertTrue(result["retrieval_trace"]["succeeded"])
        self.assertEqual(result["retrieval_trace"]["tokens"], 9)
        self.assertEqual(len(result["matched_chunks"]), 2)


if __name__ == "__main__":
    unittest.main()
