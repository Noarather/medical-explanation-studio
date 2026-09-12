import json
import tempfile
import unittest
from pathlib import Path

from db_manager import DatabaseManager
from llm_client import LLMClient
from question_format_v2 import (
    make_envelope, normalize_memory_cards, normalize_question_v2,
    read_xlsx_v2, write_xlsx_v2,
)
from services import MemoryCardBackfillService


class MemoryCardFormatTests(unittest.TestCase):
    def test_rejects_exact_legacy_content_and_keeps_new_cards(self):
        raw = {
            "tags": ["尿标本采集"],
            "briefExplanation": "清洁中段尿可减少污染。",
        }
        cards = normalize_memory_cards([
            {"title": "尿标本采集", "content": "新正文"},
            {"title": "复制简析", "content": "清洁中段尿可减少污染。"},
            {"title": "污染控制原则", "content": "采集、保存与运送共同决定微生物标本的诊断价值。"},
        ], raw)
        self.assertEqual(cards, [{
            "title": "污染控制原则",
            "content": "采集、保存与运送共同决定微生物标本的诊断价值。",
        }])

    def test_normalization_and_xlsx_round_trip_keep_memory_cards_separate(self):
        question = normalize_question_v2({
            "id": "school-m1", "bank": "school", "type": "A1", "subject": "实验诊断学",
            "question": "题干", "options": ["A. 一", "B. 二"], "answer": "A",
            "tags": ["尿标本采集"], "briefExplanation": "旧简析",
            "memoryCards": [{"title": "标本质量控制", "content": "独立教材式背诵正文。",
                             "memoryCue": "采存运", "contrast": "侵入性方法污染更少。"}],
            "memoryCardMeta": {"schemaVersion": 1, "generator": "test-model",
                               "generatedAt": "2026-07-28T00:00:00Z"},
        })
        self.assertNotIn("memoryCards", question["extensions"])
        envelope = make_envelope([question], name="背诵卡", bank="school")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory-cards.xlsx"
            write_xlsx_v2(path, envelope)
            reloaded = read_xlsx_v2(path)["questions"][0]
        self.assertEqual(reloaded["memoryCards"], question["memoryCards"])
        self.assertEqual(reloaded["memoryCardMeta"], question["memoryCardMeta"])


class FakeMemoryCardLLM(LLMClient):
    def __init__(self, payload):
        self.payload = payload
        self.prompts = []
        self.model = "test-model"

    def _complete(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return json.dumps(self.payload, ensure_ascii=False)


class MemoryCardGenerationTests(unittest.TestCase):
    def test_generator_uses_new_field_and_explicit_non_overlap_contract(self):
        llm = FakeMemoryCardLLM({"items": [{
            "id": "1",
            "memoryCards": [{"title": "标本质量控制", "content": "采集、保存与运输构成连续质量链。"}],
        }]})
        result = llm.generate_memory_card_batch([{
            "id": 1, "subject": "实验诊断学", "prompt_text": "题目",
            "explanation": "旧答案解析", "raw": {"answer": "A", "tags": ["旧标签"]},
        }])
        self.assertEqual(result["1"][0]["title"], "标本质量控制")
        self.assertIn("不是答案解析摘要", llm.prompts[0])
        self.assertIn("禁止逐句复述", llm.prompts[0])


class MemoryCardPersistenceTests(unittest.TestCase):
    def test_backfill_preserves_legacy_fields_and_records_generation_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseManager(str(Path(directory) / "memory.db"))
            try:
                raw = {
                    "id": "q1", "bank": "school", "type": "A1", "subject": "外科学",
                    "question": "题干", "options": ["A. 一", "B. 二"], "answer": "A",
                    "tags": ["旧标签"], "briefExplanation": "旧解析",
                }
                set_id = database.create_question_set("知识卡", "source.json", "json", [raw])
                question_id = database.conn.execute(
                    "SELECT id FROM imported_questions WHERE set_id=?", (set_id,)
                ).fetchone()["id"]
                before = json.loads(database.conn.execute(
                    "SELECT raw_json FROM imported_questions WHERE id=?", (question_id,)
                ).fetchone()["raw_json"])
                stored = database.apply_memory_card_backfill(
                    question_id,
                    [{"title": "独立知识", "content": "可脱离原题直接背诵的新内容。"}],
                    generator="test-model",
                )
                self.assertEqual(stored["tags"], before["tags"])
                self.assertEqual(stored["briefExplanation"], before["briefExplanation"])
                self.assertEqual(stored["answer"], before["answer"])
                self.assertEqual(stored["memoryCardMeta"]["generator"], "test-model")
                self.assertTrue(stored["memoryCardMeta"]["generatedAt"])
            finally:
                database.close()

    def test_backfill_service_batches_twenty_questions(self):
        class Database:
            rows = [{"id": index + 1, "external_id": str(index + 1), "review_status": "approved"}
                    for index in range(20)]

            def __init__(self):
                self.applied = []

            def list_missing_memory_card_questions(self, *_args):
                return list(self.rows)

            def backup_database(self, destination):
                return str(destination)

            def apply_memory_card_backfill(self, question_id, cards, **_kwargs):
                self.applied.append((question_id, cards))

        class LLM:
            model = "test-model"

            def __init__(self):
                self.calls = []

            def generate_memory_card_batch(self, rows):
                self.calls.append(len(rows))
                return {str(row["id"]): [{"title": "独立知识", "content": "新的背诵正文。"}]
                        for row in rows}

        database, llm = Database(), LLM()
        result = MemoryCardBackfillService(database, llm).run("set-id")
        self.assertEqual(llm.calls, [20])
        self.assertEqual(result["updated"], 20)
        self.assertEqual(len(database.applied), 20)


if __name__ == "__main__":
    unittest.main()
