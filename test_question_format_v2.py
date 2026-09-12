"""Contract tests for MedLearning Question Exchange v2."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from db_manager import DatabaseManager
from importers import ImportValidationError, load_excel
from llm_client import (
    ExplanationPackageError, LLMClient, build_evidence_excerpt,
    build_local_context_summary, grounded_context_summary, select_best_evidence,
)
from question_format_v2 import (
    blocks_to_markdown,
    make_envelope,
    normalize_question_v2,
    read_xlsx_v2,
    write_xlsx_v2,
)


ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "schema" / "examples" / "v2-sample.json"


class FakeStructuredLLM(LLMClient):
    def __init__(self, responses: list[str]):
        self.responses = iter(responses)
        self.model = "fake-structured"

    def _complete(self, prompt: str) -> str:
        return next(self.responses)


class QuestionFormatV2Tests(unittest.TestCase):
    def test_best_evidence_keeps_one_page_and_prefers_the_answer_defining_sentence(self):
        prompt = """题型：单句最佳选择题
药动学是研究
A. 机体对药物的作用
B. 药物对机体的作用
C. 肾脏对药物的排泄
D. 肝脏对药物的代谢
E. 药物对机体的毒性
正确答案：A"""
        candidates = [
            {"source_file":"药理学.pdf","source_page":1,"score":0.742,"text":"药理学是一门医学相关学科。药动学研究机体对药物的作用及其规律。"},
            {"source_file":"药理学.pdf","source_page":34,"score":0.755,"text":"药物在机体内产生药理作用和效应，受多种因素影响。"},
            {"source_file":"药理学.pdf","source_page":27,"score":0.751,"text":"本章讨论药物剂量及给药方法。"},
        ]
        selected = select_best_evidence(prompt, candidates)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["source_page"], 1)
        excerpt, highlights = build_evidence_excerpt(prompt, selected[0])
        self.assertEqual(excerpt, "药动学研究机体对药物的作用及其规律。")
        self.assertIn("机体对药物的作用", highlights)

    def test_best_evidence_keeps_one_quality_page_per_textbook(self):
        prompt = "影响钾跨细胞转移的主要激素是？\nA. 胰岛素\n正确答案：A"
        candidates = [
            {"textbook":"病理生理学","source_page":76,"score":.69,"text":"胰岛素促进钾离子进入细胞内。"},
            {"textbook":"病理生理学","source_page":77,"score":.67,"text":"低钾血症还可见于其他情况。"},
            {"textbook":"生理学","source_page":373,"score":.63,"text":"胰岛素参与细胞代谢并影响钾离子转移。"},
            {"textbook":"药理学","source_page":184,"score":.49,"text":"钾通道开放剂属于药物靶点。"},
        ]
        selected = select_best_evidence(prompt, candidates)
        self.assertEqual([item["textbook"] for item in selected], ["病理生理学", "生理学"])
        self.assertEqual([item["source_page"] for item in selected], [76, 373])

    def test_deepseek_structured_completion_uses_json_mode(self):
        captured = {}

        class Completions:
            @staticmethod
            def create(**kwargs):
                captured.update(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content='{"ok":true}'),
                )])

        llm = LLMClient("test-key", "https://api.deepseek.com/v1", max_tokens=2200)
        llm.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        self.assertEqual(llm._complete("只输出 JSON 对象"), '{"ok":true}')
        self.assertEqual(captured["response_format"], {"type":"json_object"})
        self.assertEqual(captured["max_tokens"], 2200)

    def test_truncated_structured_completion_is_rejected_before_json_parsing(self):
        class Completions:
            @staticmethod
            def create(**_kwargs):
                return SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="length", message=SimpleNamespace(content='{"explanationBlocks":['),
                )])

        llm = LLMClient("test-key", "https://api.deepseek.com/v1", max_tokens=2200)
        llm.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        with self.assertRaisesRegex(ExplanationPackageError, "长度上限"):
            llm._complete("只输出 JSON 对象")

    def test_schema_and_fixture_match_web_repository_byte_for_byte(self):
        web_schema = ROOT.parent / "schema"
        if not web_schema.exists():
            web_schema = ROOT.parent / "web-question-bank" / "schema"
        if not web_schema.exists():
            self.skipTest("Optional sibling web repository is not included in the standalone source release")
        for relative in (Path("medlearning-question-set-v2.schema.json"), Path("examples/v2-sample.json")):
            self.assertEqual((ROOT / "schema" / relative).read_bytes(), (web_schema / relative).read_bytes())

    def test_json_normalization_keeps_unique_knowledge_and_moves_extensions(self):
        value = normalize_question_v2({
            "questionSource": " 本校历年题 ",
            "id": "school_1", "bank": "school", "subject": "外科学", "question": "题干",
            "knowledgePoints": ["胆囊结石首选腹部超声"], "tags": ["胆石症检查"],
            "customField": {"kept": True},
            "explanationBlocks": [{"section": "analysis", "type": "paragraph", "text": "<b>安全文本</b>"}],
        })
        self.assertEqual(value["knowledgePoints"], ["胆囊结石首选腹部超声"])
        self.assertEqual(value["tags"], ["胆石症检查"])
        self.assertEqual(value["extensions"]["customField"], {"kept": True})
        self.assertEqual(value["questionSource"], "本校历年题")
        self.assertNotIn("<b>", value["explanationBlocks"][0]["text"])

    def test_xlsx_five_sheet_round_trip_is_semantically_lossless(self):
        source = json.loads(FIXTURE.read_text(encoding="utf-8"))
        source["questions"][0]["explanationMeta"]["evidence"][0]["contextSummary"] = "尿细菌培养宜留取清洁中段尿，以减少污染。"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roundtrip.xlsx"
            write_xlsx_v2(path, source)
            value = read_xlsx_v2(path)
        self.assertEqual(value["schemaVersion"], 2)
        self.assertEqual(value["meta"]["bank"], source["meta"]["bank"])
        self.assertEqual(value["questions"][0]["explanationBlocks"], normalize_question_v2(source["questions"][0])["explanationBlocks"])
        self.assertEqual(value["questions"][0]["explanationMeta"]["evidence"][0]["sourcePage"], 88)
        self.assertEqual(value["questions"][0]["explanationMeta"]["evidence"][0]["contextSummary"], "尿细菌培养宜留取清洁中段尿，以减少污染。")
        self.assertEqual(value["questions"][0]["explanationMeta"]["evidenceGrade"], "A")
        self.assertEqual(value["questions"][0]["explanationMeta"]["reasoningType"], "direct_answer")
        self.assertEqual(value["questions"][0]["explanationMeta"]["evidence"][0]["supportedClues"], ["题目核心"])
        self.assertEqual(value["questions"][0]["briefExplanation"], source["questions"][0]["briefExplanation"])
        self.assertEqual(value["questions"][0]["questionSource"], source["questions"][0]["questionSource"])
        self.assertNotIn("sourcePath", json.dumps(value, ensure_ascii=False))

    def test_xlsx_removes_only_xml_forbidden_control_characters(self):
        source = make_envelope([{
            "id":"school_control", "bank":"school", "type":"A1", "subject":"生理学", "system":"体液\x0b平衡",
            "question":"细胞外液\x00主要阳离子是？", "options":["A. Na+", "B. K+\x0c"], "answer":"A",
            "explanationBlocks":[{"section":"analysis", "type":"paragraph", "text":"保留第一行\n保留第二行\x1f"}],
            "explanationMeta":{"evidence":[{"textbook":"生理学\ufffe", "sourcePage":11, "quote":"教材\x07原文"}]},
            "extensions":{"sourceText":"扩展\x08字段"},
        }], name="控制字符\x0b题库", bank="school")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.xlsx"
            write_xlsx_v2(path, source)
            value = read_xlsx_v2(path)
        question = value["questions"][0]
        self.assertEqual(question["system"], "体液平衡")
        self.assertEqual(question["question"], "细胞外液主要阳离子是？")
        self.assertEqual(question["options"][1], "B. K+")
        self.assertEqual(question["explanationBlocks"][0]["text"], "保留第一行\n保留第二行")
        self.assertEqual(question["explanationMeta"]["evidence"][0]["textbook"], "生理学")
        self.assertEqual(question["explanationMeta"]["evidence"][0]["quote"], "教材原文")
        # Extensions are JSON encoded inside one cell, so escaped control
        # characters remain semantically lossless without violating XML.
        self.assertEqual(question["extensions"]["sourceText"], "扩展\x08字段")

    def test_xlsx_rejects_question_bank_that_differs_from_meta(self):
        envelope = make_envelope([{
            "id":"kaoyan_mixed", "bank":"kaoyan", "subject":"生理学", "type":"A1",
            "question":"题干", "options":["A.甲","B.乙"], "answer":"A",
        }], name="混合题库", bank="school")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mixed.xlsx"
            write_xlsx_v2(path, envelope)
            with self.assertRaises(ImportValidationError):
                load_excel(path, {})

    def test_markdown_is_deterministic_and_uses_printed_page(self):
        blocks = [{
            "section": "analysis", "type": "table", "title": "方法选择阶梯",
            "columns": ["方法", "用途"], "rows": [["腹部超声", "首选"]],
        }]
        markdown = blocks_to_markdown(blocks, [{"textbook": "外科学", "sourcePage": 251, "pdfPage": 270}])
        self.assertIn("| 方法 | 用途 |", markdown)
        self.assertIn("课本第251页", markdown)
        self.assertNotIn("270", markdown)

    def test_structured_llm_repairs_fenced_json_and_reuses_official_tags(self):
        payload = {
            "explanationBlocks": [
                {"section": "analysis", "type": "callout", "title": "关键点", "text": "超声为首选"},
                {"section": "answerBasis", "type": "paragraph", "title": "答案依据", "text": "教材支持该选择"},
                {"section": "pitfalls", "type": "table", "title": "易错点", "columns": ["选项", "辨析"], "rows": [["其他", "不是首选"]]},
            ],
            "knowledgePoints": ["胆囊结石首选腹部超声"],
            "tags": ["胆石症检查", "新候选标签"],
        }
        llm = FakeStructuredLLM([f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"])
        package = llm.generate_explanation_package("题目", [{"source_file": "外科学.pdf", "source_page": 251, "pdf_page": 270, "text": "证据", "score": .9}], ["胆石症检查"])
        self.assertEqual(package["tags"], ["胆石症检查"])
        self.assertEqual(package["suggestedTags"], ["新候选标签"])
        self.assertEqual(package["knowledgePoints"], [])
        self.assertTrue(package["briefExplanation"])
        self.assertEqual(package["explanationMeta"]["evidence"][0]["sourcePage"], 251)

    def test_invalid_model_content_is_rejected_after_one_repair(self):
        llm = FakeStructuredLLM(["not json", "still not json"])
        with self.assertRaises(ExplanationPackageError):
            llm.generate_explanation_package("题目", [])

    def test_missing_tags_trigger_one_lightweight_repair(self):
        blocks=[{"section":"analysis","type":"paragraph","text":"已有解析"},{"section":"answerBasis","type":"paragraph","text":"答案依据"},{"section":"pitfalls","type":"table","columns":["易错点","辨析"],"rows":[["其他项","不符合"]]}]
        first = {"explanationBlocks":blocks,"briefExplanation":"一句话简析","tags":[]}
        repaired = {"explanationBlocks":blocks,"briefExplanation":"一句话简析","tags":["病史采集"]}
        llm = FakeStructuredLLM([json.dumps(first,ensure_ascii=False),json.dumps(repaired,ensure_ascii=False)])
        package = llm.generate_explanation_package("题目", [{"source_file":"诊断学.pdf","source_page":10,"pdf_page":12,"text":"证据","score":.8}])
        self.assertEqual(package["tags"],[])
        self.assertEqual(package["suggestedTags"],["病史采集"])
        self.assertEqual(package["knowledgePoints"],[])

    def test_tag_batch_maps_ids_and_rejects_empty_rows(self):
        response = {"items":[
            {"id":"11","tags":["病史采集","诱导性提问"],"briefExplanation":"正确选项属于诱导性提问。"},
            {"id":"12","tags":[],"briefExplanation":""},
            {"id":"unknown","tags":["忽略标签"]},
        ]}
        llm = FakeStructuredLLM([json.dumps(response, ensure_ascii=False)])
        result = llm.generate_tag_batch([
            {"id":11,"subject":"诊断学","prompt_text":"题目一","raw":{"answer":"D"},"explanation":"已有解析"},
            {"id":12,"subject":"诊断学","prompt_text":"题目二","raw":{"answer":"A"},"explanation":"已有解析"},
        ], {"诊断学":["病史采集"]})
        self.assertEqual(result["11"]["tags"], ["病史采集", "诱导性提问"])
        self.assertNotIn("12", result)
        self.assertNotIn("unknown", result)

    def test_schema_v8_promotes_third_candidate_and_backfills_all_questions(self):
        rows = [
            {"id": "q1", "bank": "school", "subject": "外科学", "type": "A1", "question": "一", "options": ["A.甲", "B.乙"], "answer": "A", "suggestedTags": ["胆石症检查"]},
            {"id": "q2", "bank": "school", "subject": "外科学", "type": "A1", "question": "二", "options": ["A.甲", "B.乙"], "answer": "B", "suggestedTags": ["胆石症检查"]},
            {"id": "q3", "bank": "school", "subject": "外科学", "type": "A1", "question": "三", "options": ["A.甲", "B.乙"], "answer": "A", "suggestedTags": ["胆石症检查"]},
        ]
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "v7.db"))
            try:
                set_id = db.create_question_set("标签", "source.json", "json", rows)
                self.assertEqual(db.conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], "8")
                catalog = db.list_question_tags(set_id, "外科学", "active")
                self.assertEqual([item["label"] for item in catalog], ["胆石症检查"])
                stored = db.conn.execute("SELECT raw_json FROM imported_questions WHERE set_id=? ORDER BY id", (set_id,)).fetchall()
                self.assertTrue(all(json.loads(item["raw_json"])["tags"] == ["胆石症检查"] for item in stored))
            finally:
                db.close()

    def test_evidence_excerpt_is_verbatim_and_invalid_model_excerpt_falls_back(self):
        quote = "患者出现右上腹疼痛。腹部超声是胆囊结石的首选检查。必要时进一步评估胆道。"
        llm = FakeStructuredLLM([json.dumps({
            "explanationBlocks":[
                {"section":"analysis","type":"paragraph","text":"解析"},
                {"section":"answerBasis","type":"paragraph","text":"依据"},
                {"section":"pitfalls","type":"table","columns":["项目","辨析"],"rows":[["CT","不是首选"]]},
            ],
            "briefExplanation":"腹部超声是胆囊结石的首选检查。",
            "tags":["胆石症检查"],
            "evidenceExcerpts":[{"evidenceId":"E1","excerpt":"模型捏造的原句","highlights":["胆囊结石"]}],
        },ensure_ascii=False)])
        package=llm.generate_explanation_package("胆囊结石首选什么检查？",[{"source_file":"外科学.pdf","source_page":20,"pdf_page":30,"text":quote,"score":.9}])
        evidence=package["explanationMeta"]["evidence"][0]
        self.assertIn(evidence["excerpt"],quote)
        self.assertEqual(evidence["evidenceId"],"E1")
        self.assertTrue(all(value in quote for value in evidence["highlights"]))

    def test_ai_context_is_kept_only_when_grounded_and_raw_quote_is_unchanged(self):
        prompt = "胆囊结石首选什么检查？\nA. 腹部超声\nB. CT\n正确答案：A"
        quote = "第三章 胆石病。患者出现右上腹疼痛。腹部超声是胆囊结石的首选检查。必要时进一步评估胆道。"
        evidence = {"quote": quote}
        grounded = "患者可出现右上腹疼痛；胆囊结石首选腹部超声，必要时进一步评估胆道。"
        self.assertEqual(grounded_context_summary(prompt, evidence, grounded), grounded)
        fallback = grounded_context_summary(prompt, evidence, "该病必须立即接受手术，死亡率为90%。")
        self.assertNotIn("死亡率", fallback)
        self.assertIn("腹部超声", fallback)
        self.assertEqual(evidence["quote"], quote)
        self.assertLessEqual(len(build_local_context_summary(prompt, evidence)), 320)


if __name__ == "__main__":
    unittest.main()
