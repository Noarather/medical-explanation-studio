import json
import tempfile
import unittest
from pathlib import Path

from db_manager import DatabaseManager
from services import ExportService

SAMPLE_A = {"id": "a1", "bank": "school", "type": "A1", "subject": "内科学", "question": "题干A", "options": ["A. 一", "B. 二"], "answer": "A"}
SAMPLE_B = {"id": "b1", "bank": "school", "type": "A1", "subject": "外科学", "question": "题干B", "options": ["A. 一", "B. 二"], "answer": "B"}


class ExportSplitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = DatabaseManager(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def _package(self, tag_label):
        return {"explanationBlocks": [], "briefExplanation": "简析", "knowledgePoints": [],
                "tags": [tag_label], "suggestedTags": [],
                "explanationMeta": {"mode": "textbook", "evidence": []}}

    def _seed(self):
        set_id = self.database.create_question_set("测试", "one.json", "json", [SAMPLE_A, SAMPLE_B])
        for question in self.database.list_imported_questions(set_id):
            # 每个学科写入各自的标签，tagCatalog 才会按学科出现真实条目。
            self.database.save_generated_result(
                question["id"], "generated", "textbook", 0.9, "解析", [],
                package=self._package(f"{question['subject']}标签"),
            )
            self.database.mark_question_viewed(question["id"])
            self.database.review_question(question["id"], "approved", "解析")
        return set_id

    def test_split_by_subject_writes_one_envelope_per_subject(self):
        set_id = self._seed()
        result = ExportService(self.database).export(set_id, self.temp.name, split_by_subject=True)
        files = [Path(item) for item in result["files"]]
        json_files = [item for item in files if item.suffix == ".json" and "映射" not in item.name]
        self.assertEqual(len(json_files), 2)
        subjects = set()
        for path in json_files:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(envelope["questions"]), 1)
            subject = envelope["questions"][0]["subject"]
            subjects.add(subject)
            self.assertTrue(all(item["subject"] == subject for item in envelope["tagCatalog"]))
            labels = {item["label"] for item in envelope["tagCatalog"]}
            self.assertEqual(labels, {f"{subject}标签"})
        self.assertEqual(subjects, {"内科学", "外科学"})
        self.assertTrue(Path(result["mapping"]).exists())

    def test_default_export_unchanged_single_file(self):
        set_id = self._seed()
        result = ExportService(self.database).export(set_id, self.temp.name)
        envelope = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
        self.assertEqual(len(envelope["questions"]), 2)


if __name__ == "__main__":
    unittest.main()
