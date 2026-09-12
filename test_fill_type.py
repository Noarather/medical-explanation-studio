import unittest
from question_format_v2 import normalize_question_v2


class FillTypeTests(unittest.TestCase):
    def test_fill_answer_array_preserved(self):
        raw = {
            "id": "f1", "bank": "school", "type": "fill", "subject": "生理学",
            "question": "房室瓣【1】，动脉瓣【2】", "options": [],
            "answer": [["关闭"], ["开放"]],
        }
        question = normalize_question_v2(raw)
        self.assertEqual(question["type"], "fill")
        self.assertEqual(question["answer"], [["关闭"], ["开放"]])

    def test_fill_answer_stringified_json_parsed(self):
        raw = {
            "id": "f2", "bank": "school", "type": "fill", "subject": "生理学",
            "question": "空位【1】", "options": [], "answer": '[["甲", "A 甲"]]',
        }
        question = normalize_question_v2(raw)
        self.assertEqual(question["answer"], [["甲", "A 甲"]])

    def test_choice_answer_unchanged(self):
        raw = {
            "id": "c1", "bank": "school", "type": "A1", "subject": "生理学",
            "question": "题干", "options": ["A. 一", "B. 二"], "answer": "B",
        }
        self.assertEqual(normalize_question_v2(raw)["answer"], "B")


if __name__ == "__main__":
    unittest.main()
