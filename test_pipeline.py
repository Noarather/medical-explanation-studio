"""Offline unit tests that do not call DeepSeek or DashScope."""

import tempfile
import unittest
from pathlib import Path

from db_manager import DatabaseManager, decode_embedding, encode_embedding


class DatabaseTests(unittest.TestCase):
    def test_embedding_round_trip(self):
        values = [0.1, -0.2, 0.3]
        restored = decode_embedding(encode_embedding(values), 3)
        for actual, expected in zip(restored, values):
            self.assertAlmostEqual(actual, expected, places=6)

    def test_question_upsert_and_status_query(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            with DatabaseManager(str(path)) as database:
                database.save_question("school_1", "题目", [], 0.2, "unmatched")
                self.assertEqual(database.query_by_status("unmatched")[0]["question_id"], "school_1")
                database.save_question("school_1", "题目", [{"text": "证据"}], 0.8, "matched", "解析")
                self.assertEqual(database.get_question("school_1")["explanation"], "解析")


if __name__ == "__main__":
    unittest.main()

