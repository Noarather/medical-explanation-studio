import unittest
from question_format_v2 import normalize_study_points, normalize_question_v2


class NormalizeStudyPointsTests(unittest.TestCase):
    def test_trims_dedupes_and_caps(self):
        value = [
            {"title": " 胆囊结石首选超声 ", "body": " 首选腹部超声检查的复习正文。 "},
            {"title": "胆囊结石首选超声", "body": "重复标题应丢弃"},
            {"title": "标题超长的情况应该被截断到二十个字以内啊", "body": "正文" + "很" * 400},
            {"title": "", "body": "无标题丢弃"},
            "not-a-dict",
            {"title": "第四条", "body": "第四条正文"},
            {"title": "第五条", "body": "进不来"},
        ]
        result = normalize_study_points(value)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], {"title": "胆囊结石首选超声", "body": "首选腹部超声检查的复习正文。"})
        self.assertLessEqual(len(result[1]["title"]), 20)
        self.assertLessEqual(len(result[1]["body"]), 300)

    def test_invalid_input_becomes_empty(self):
        self.assertEqual(normalize_study_points(None), [])
        self.assertEqual(normalize_study_points("string"), [])

    def test_normalize_question_v2_keeps_study_points_out_of_extensions(self):
        raw = {
            "id": "q1", "bank": "school", "type": "A1", "subject": "外科学",
            "question": "题干", "options": ["A. 一", "B. 二"], "answer": "A",
            "studyPoints": [{"title": "考点一", "body": "考点一的复习正文。"}],
        }
        question = normalize_question_v2(raw)
        self.assertEqual(question["studyPoints"], [{"title": "考点一", "body": "考点一的复习正文。"}])
        self.assertNotIn("studyPoints", question["extensions"])


import json
from llm_client import LLMClient


class FakeStudyPointLLM(LLMClient):
    def __init__(self, payload):
        self.payload = payload
        self.prompts = []

    def _complete(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return json.dumps(self.payload, ensure_ascii=False)


def _llm(payload):
    return FakeStudyPointLLM(payload)


class GenerateStudyPointBatchTests(unittest.TestCase):
    def test_parses_and_normalizes_items(self):
        llm = _llm({"items": [
            {"id": "1", "studyPoints": [{"title": "胆石症检查", "body": "首选超声的完整复习正文。"}]},
            {"id": "999", "studyPoints": [{"title": "越界ID", "body": "应忽略"}]},
            {"id": "2", "studyPoints": "garbage"},
        ]})
        result = llm.generate_study_point_batch([
            {"id": 1, "subject": "外科学", "raw": {"question": "题干一", "answer": "A"}},
            {"id": 2, "subject": "外科学", "raw": {"question": "题干二", "answer": "B"}},
        ])
        self.assertEqual(set(result.keys()), {"1"})
        self.assertEqual(result["1"][0]["title"], "胆石症检查")
        self.assertIn("只输出 JSON", llm.prompts[0])

    def test_missing_items_array_raises(self):
        from llm_client import ExplanationPackageError
        llm = _llm({"wrong": True})
        with self.assertRaises((ExplanationPackageError, ValueError)):
            llm.generate_study_point_batch([{"id": 1, "raw": {"question": "x"}}])


import tempfile
from pathlib import Path

from question_format_v2 import make_envelope, read_xlsx_v2, write_xlsx_v2
from services import StudyPointBackfillService


class XlsxStudyPointsRoundTripTests(unittest.TestCase):
    def test_xlsx_round_trip_preserves_study_points(self):
        envelope = make_envelope([{
            "id": "school_sp1", "bank": "school", "type": "A1", "subject": "外科学",
            "question": "胆囊结石首选什么检查？", "options": ["A. 腹部超声", "B. CT"], "answer": "A",
            "studyPoints": [{"title": "胆石症检查", "body": "首选超声的复习正文。"}],
        }], name="考点题库", bank="school")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roundtrip.xlsx"
            write_xlsx_v2(path, envelope)
            value = read_xlsx_v2(path)
        reloaded_question = value["questions"][0]
        self.assertEqual(reloaded_question["studyPoints"],
                         [{"title": "胆石症检查", "body": "首选超声的复习正文。"}])


class FakeStudyPointDatabase:
    def __init__(self, count=20):
        self.rows = [{
            "id": index + 1, "external_id": f"sp-{index + 1}", "subject": "外科学",
            "prompt_text": f"题目 {index + 1}", "explanation": "已有解析",
            "raw": {"answer": "A"}, "review_status": "approved",
        } for index in range(count)]
        self.applied = []
        self.backed_up = False

    def list_missing_study_point_questions(self, _set_id, _include_approved=True):
        return list(self.rows)
    def backup_database(self, destination):
        self.backed_up = True
        return str(destination)
    def apply_study_point_backfill(self, question_id, study_points):
        self.applied.append((question_id, list(study_points)))


class FakeStudyPointBatchLLM:
    def __init__(self):
        self.calls = []

    def generate_study_point_batch(self, rows):
        self.calls.append(len(rows))
        return {str(row["id"]): [{"title": "胆石症检查", "body": "首选超声的复习正文。"}] for row in rows}


class StudyPointBackfillServiceTests(unittest.TestCase):
    def test_twenty_questions_use_one_batch(self):
        database = FakeStudyPointDatabase(20)
        llm = FakeStudyPointBatchLLM()
        result = StudyPointBackfillService(database, llm).run("set-id")
        self.assertEqual(llm.calls, [20])
        self.assertEqual(result["updated"], 20)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(len(database.applied), 20)
        self.assertTrue(database.backed_up)

    def test_failed_batch_falls_back_to_single_question(self):
        database = FakeStudyPointDatabase(2)

        class FlakyLLM:
            def __init__(self):
                self.calls = []
            def generate_study_point_batch(self, rows):
                self.calls.append(len(rows))
                if len(rows) > 1:
                    raise RuntimeError("batch boom")
                return {str(rows[0]["id"]): [{"title": "t", "body": "b"}]}

        llm = FlakyLLM()
        result = StudyPointBackfillService(database, llm).run("set-id")
        self.assertEqual(llm.calls, [2, 1, 1])
        self.assertEqual(result["updated"], 2)


from db_manager import DatabaseManager


class ApplyStudyPointBackfillTests(unittest.TestCase):
    def _make_database(self, directory, raw_extra=None):
        database = DatabaseManager(str(Path(directory) / "backfill.db"))
        raw = {"id": "q1", "bank": "school", "type": "A1", "subject": "外科学",
               "question": "题干", "options": ["A. 一", "B. 二"], "answer": "A",
               "tags": ["胆石症检查"], "briefExplanation": "已有简析"}
        if raw_extra:
            raw.update(raw_extra)
        set_id = database.create_question_set("考点", "source.json", "json", [raw])
        question_id = database.conn.execute(
            "SELECT id FROM imported_questions WHERE set_id=?", (set_id,)
        ).fetchone()["id"]
        return database, question_id

    def test_backfill_writes_study_points_and_preserves_other_raw_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            database, question_id = self._make_database(directory)
            try:
                raw = database.apply_study_point_backfill(
                    question_id, [{"title": "胆石症检查", "body": "首选超声的复习正文。"}])
                self.assertEqual(raw["studyPoints"],
                                 [{"title": "胆石症检查", "body": "首选超声的复习正文。"}])
                stored = json.loads(database.conn.execute(
                    "SELECT raw_json FROM imported_questions WHERE id=?", (question_id,)
                ).fetchone()["raw_json"])
                self.assertEqual(stored["studyPoints"], raw["studyPoints"])
                self.assertEqual(stored["briefExplanation"], "已有简析")
                self.assertEqual(stored["answer"], "A")
                self.assertEqual(stored["question"], "题干")
            finally:
                database.close()

    def test_backfill_rejects_points_that_normalize_to_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            database, question_id = self._make_database(directory)
            try:
                with self.assertRaises(ValueError):
                    database.apply_study_point_backfill(question_id, "garbage")
                with self.assertRaises(ValueError):
                    database.apply_study_point_backfill(question_id, [{"title": "", "body": "无标题"}])
                stored = json.loads(database.conn.execute(
                    "SELECT raw_json FROM imported_questions WHERE id=?", (question_id,)
                ).fetchone()["raw_json"])
                self.assertNotIn("studyPoints", stored)
            finally:
                database.close()

    def test_backfill_rejects_missing_question_id(self):
        with tempfile.TemporaryDirectory() as directory:
            database, _ = self._make_database(directory)
            try:
                with self.assertRaises(ValueError):
                    database.apply_study_point_backfill(999999, [{"title": "t", "body": "b"}])
            finally:
                database.close()


class ListMissingStudyPointQuestionsTests(unittest.TestCase):
    def _make_database(self, directory):
        database = DatabaseManager(str(Path(directory) / "missing.db"))
        raw_with_points = {
            "id": "q-has", "bank": "school", "type": "A1", "subject": "外科学",
            "question": "已有考点的题", "options": ["A. 一", "B. 二"], "answer": "A",
            "studyPoints": [{"title": "胆石症检查", "body": "首选超声的复习正文。"}],
        }
        raw_without_points = {
            "id": "q-missing", "bank": "school", "type": "A1", "subject": "外科学",
            "question": "缺少考点的题", "options": ["A. 一", "B. 二"], "answer": "B",
        }
        set_id = database.create_question_set(
            "考点", "source.json", "json", [raw_with_points, raw_without_points])
        with database.conn:
            database.conn.execute(
                "UPDATE imported_questions SET pipeline_status='generated' WHERE set_id=?",
                (set_id,))
        rows = database.conn.execute(
            "SELECT id,external_id FROM imported_questions WHERE set_id=?", (set_id,)
        ).fetchall()
        ids = {row["external_id"]: row["id"] for row in rows}
        return database, set_id, ids

    def test_returns_only_generated_questions_without_study_points(self):
        with tempfile.TemporaryDirectory() as directory:
            database, set_id, ids = self._make_database(directory)
            try:
                result = database.list_missing_study_point_questions(set_id)
                self.assertEqual([row["id"] for row in result], [ids["q-missing"]])
                self.assertEqual(result[0]["external_id"], "q-missing")
            finally:
                database.close()

    def test_include_approved_false_excludes_approved_questions(self):
        with tempfile.TemporaryDirectory() as directory:
            database, set_id, ids = self._make_database(directory)
            try:
                with database.conn:
                    database.conn.execute(
                        "UPDATE imported_questions SET review_status='approved' WHERE id=?",
                        (ids["q-missing"],))
                pending_only = database.list_missing_study_point_questions(
                    set_id, include_approved=False)
                self.assertEqual(pending_only, [])
                everything = database.list_missing_study_point_questions(
                    set_id, include_approved=True)
                self.assertEqual([row["id"] for row in everything], [ids["q-missing"]])
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
