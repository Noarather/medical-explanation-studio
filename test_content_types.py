import json
import tempfile
import unittest
from pathlib import Path

from db_manager import DatabaseManager

SAMPLE = {"id": "a1", "bank": "school", "type": "A1", "subject": "内科学", "question": "题干", "options": ["A. 一", "B. 二"], "answer": "A", "tags": ["旧标签"]}


class ContentTypesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = DatabaseManager(str(Path(self.temp.name) / "test.db"))
        self.set_id = self.database.create_question_set("测试", "one.json", "json", [SAMPLE])
        self.question = self.database.list_imported_questions(self.set_id)[0]

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def _package(self):
        return {"explanationBlocks": [], "briefExplanation": "简析", "knowledgePoints": [],
                "tags": ["新标签"], "suggestedTags": ["候选"], "explanationMeta": {"mode": "textbook", "evidence": []}}

    def _raw(self):
        # list_imported_questions does not select raw_json; read it directly.
        row = self.database.conn.execute(
            "SELECT raw_json FROM imported_questions WHERE id=?", (self.question["id"],)
        ).fetchone()
        return json.loads(row["raw_json"])

    def _seed_existing_tags(self):
        # Import moves SAMPLE["tags"] into suggestedTags and empties tags;
        # seed both keys so the mask has pre-existing values to preserve.
        raw = self._raw()
        raw["tags"] = ["旧标签"]
        raw["suggestedTags"] = ["旧候选"]
        with self.database.conn:
            self.database.conn.execute(
                "UPDATE imported_questions SET raw_json=? WHERE id=?",
                (json.dumps(raw, ensure_ascii=False), self.question["id"]),
            )

    def _tag_labels(self, raw):
        # sync_question_tags (called on every package save) redistributes labels
        # between tags/suggestedTags by usage count, so assert on the union.
        return set(raw.get("tags") or []) | set(raw.get("suggestedTags") or [])

    def test_write_tags_true_replaces_tags(self):
        self._seed_existing_tags()
        self.database.save_generated_result(self.question["id"], "generated", "textbook", 0.9, "解析", [], package=self._package())
        labels = self._tag_labels(self._raw())
        self.assertIn("新标签", labels)
        self.assertIn("候选", labels)
        self.assertNotIn("旧标签", labels)
        self.assertNotIn("旧候选", labels)

    def test_write_tags_false_preserves_existing_tags(self):
        self._seed_existing_tags()
        self.database.save_generated_result(self.question["id"], "generated", "textbook", 0.9, "解析", [], package=self._package(), write_tags=False)
        raw = self._raw()
        labels = self._tag_labels(raw)
        self.assertIn("旧标签", labels)
        self.assertIn("旧候选", labels)
        self.assertNotIn("新标签", labels)
        self.assertNotIn("候选", labels)
        self.assertEqual(raw["briefExplanation"], "简析")

    def test_study_points_only_pass_skips_main_pipeline(self):
        from services import GenerationService

        class FakeLLM:
            def __init__(self):
                self.study_calls = 0
            def generate_study_point_batch(self, rows):
                self.study_calls += 1
                return {str(row["id"]): [{"title": "考点一", "body": "考点一的复习正文。"}] for row in rows}
            def generate_explanation_package(self, *args, **kwargs):
                raise AssertionError("主管线不应被调用")

        # list_missing_study_point_questions 只统计 pipeline_status='generated' 的题目；
        # 新导入题目是 queued，先标记为已生成（无考点）以进入补缺范围。
        self.database.save_generated_result(self.question["id"], "generated", "textbook", 0.9, "解析", [])
        config = self._config()
        llm = FakeLLM()
        service = GenerationService(self.database, config, None, llm)
        result = service.generate_set(self.set_id, content_types={"studyPoints"})
        self.assertEqual(llm.study_calls, 1)
        raw = self._raw()
        self.assertEqual(raw["studyPoints"], [{"title": "考点一", "body": "考点一的复习正文。"}])
        self.assertEqual(result["studyPoints"]["updated"], 1)

    def _config(self, **overrides):
        config = {
            "generation": {"concurrency": 2, "adaptive_concurrency": True, "max_retries": 3, "success_ramp_window": 20},
            "retrieval": {"similarity_threshold": 0.5},
            "memory_guard": {"max_memory_mb": 2048, "batch_process_size": 50},
        }
        config.update(overrides)
        return config

    def _status(self):
        row = self.database.conn.execute(
            "SELECT pipeline_status, review_status FROM imported_questions WHERE id=?",
            (self.question["id"],),
        ).fetchone()
        return row["pipeline_status"], row["review_status"]

    def test_study_points_only_regenerate_keeps_approved_status(self):
        from services import GenerationService

        class FakeLLM:
            def generate_study_point_batch(self, rows):
                return {str(row["id"]): [{"title": "考点一", "body": "考点一的复习正文。"}] for row in rows}
            def generate_explanation_package(self, *args, **kwargs):
                raise AssertionError("主管线不应被调用")

        # 已批准 + 已生成但缺考点的题目：content_types 不含 explanation 时
        # 不允许被重置回 queued/pending，考点补缺仍应照常写入。
        self.database.save_generated_result(self.question["id"], "generated", "textbook", 0.9, "解析", [])
        self.database.mark_question_viewed(self.question["id"])
        self.database.review_question(self.question["id"], "approved", "解析")
        service = GenerationService(self.database, self._config(), None, FakeLLM())
        result = service.generate_set(
            self.set_id, content_types={"studyPoints"}, question_ids=[self.question["id"]],
        )
        self.assertEqual(self._status(), ("generated", "approved"))
        self.assertEqual(
            self._raw()["studyPoints"], [{"title": "考点一", "body": "考点一的复习正文。"}],
        )
        self.assertEqual(result["studyPoints"]["updated"], 1)

    def test_general_batch_write_tags_false_preserves_tags(self):
        from services import GenerationService

        class FakeLLM:
            def generate_fallback_package(self, prompt_text):
                return {
                    "explanationBlocks": [{"blockId": "b1", "section": "analysis", "type": "paragraph", "text": "通识解析"}],
                    "explanation": "通识解析",
                    "briefExplanation": "简析",
                    "knowledgePoints": [],
                    "tags": ["新标签"],
                    "suggestedTags": ["候选"],
                    "explanationMeta": {"mode": "general_knowledge", "evidence": []},
                }

        # 进入通识批量范围：未匹配 + 待审核；并预置旧标签以观察是否被遮蔽。
        self.database.save_generation_failure(self.question["id"], "unmatched", "相似度不足")
        self._seed_existing_tags()
        service = GenerationService(self.database, self._config(), None, FakeLLM())
        result = service.generate_general_batch(self.set_id, write_tags=False)
        self.assertEqual(result["generated"], 1)
        labels = self._tag_labels(self._raw())
        self.assertIn("旧标签", labels)
        self.assertNotIn("新标签", labels)

    def test_general_batch_default_writes_tags(self):
        from services import GenerationService

        class FakeLLM:
            def generate_fallback_package(self, prompt_text):
                return {
                    "explanationBlocks": [{"blockId": "b1", "section": "analysis", "type": "paragraph", "text": "通识解析"}],
                    "explanation": "通识解析",
                    "briefExplanation": "简析",
                    "knowledgePoints": [],
                    "tags": ["新标签"],
                    "suggestedTags": ["候选"],
                    "explanationMeta": {"mode": "general_knowledge", "evidence": []},
                }

        self.database.save_generation_failure(self.question["id"], "unmatched", "相似度不足")
        service = GenerationService(self.database, self._config(), None, FakeLLM())
        result = service.generate_general_batch(self.set_id)
        self.assertEqual(result["generated"], 1)
        labels = self._tag_labels(self._raw())
        self.assertIn("新标签", labels)


if __name__ == "__main__":
    unittest.main()
