from __future__ import annotations

import threading
import time
import unittest

from generation_executor import (
    AdaptiveConcurrencyController, FatalGenerationError, GenerationRoute,
    generate_with_retry, route_generation,
)
from llm_client import LLMRequestError
from services import CancelledError, GenerationService, TagBackfillService


def config(concurrency: int = 3) -> dict:
    return {
        "generation": {
            "concurrency": concurrency,
            "adaptive_concurrency": True,
            "max_retries": 3,
            "success_ramp_window": 20,
        },
        "retrieval": {"bm25_top_k": 20, "final_top_k": 3, "similarity_threshold": 0.5},
        "memory_guard": {"max_memory_mb": 2048, "batch_process_size": 50},
    }


def questions(count: int) -> list[dict]:
    return [
        {"id": index + 1, "external_id": f"q{index + 1}", "subject": "外科学", "prompt_text": f"题目 {index + 1}"}
        for index in range(count)
    ]


class FakeDatabase:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.owner = threading.get_ident()
        self.writes: list[dict] = []

    def queued_questions(self, _set_id, _question_ids=None):
        return list(self.rows)

    def list_libraries(self):
        return [{"subject": "外科学"}]

    def save_generated_result(self, question_id, status, mode, score, explanation, evidence, error_message="", package=None, write_tags=True):
        if threading.get_ident() != self.owner:
            raise AssertionError("SQLite write escaped the generation main thread")
        self.writes.append({
            "id": question_id, "status": status, "mode": mode, "score": score,
            "explanation": explanation, "evidence": evidence, "error": error_message,
            "package": package,
        })


class FakeEmbedding:
    def __init__(self, bad_text: str = ""):
        self.calls: list[list[str]] = []
        self.bad_text = bad_text

    def get_embeddings(self, texts):
        values = list(texts)
        self.calls.append(values)
        if len(values) > 1 and self.bad_text:
            raise RuntimeError("batch rejected")
        if self.bad_text and values[0] == self.bad_text:
            raise RuntimeError("bad input")
        return [[float(index), 1.0] for index, _ in enumerate(values)]


class FakeRetrieval:
    def retrieve(self, _subject, prompt, _vector):
        return {
            "is_matched": True,
            "top_score": 0.9,
            "matched_chunks": [{"text": f"证据 {prompt}", "source_file": "教材.pdf", "source_page": 1}],
        }


class BelowThresholdRetrieval:
    def retrieve(self, _subject, prompt, _vector):
        return {
            "is_matched": True,
            "top_score": 0.49,
            "matched_chunks": [{
                "text": f"低分证据 {prompt}", "source_file": "教材.pdf",
                "source_page": 1, "score": 0.49,
            }],
        }


class FakeTagBackfillDatabase:
    def __init__(self, count: int = 20):
        self.rows = [{
            "id":index + 1, "external_id":f"tag-{index + 1}", "subject":"外科学",
            "prompt_text":f"题目 {index + 1}", "explanation":"已有解析",
            "raw":{"answer":"A", "briefExplanation":"已有简析"},
            "review_status":"approved", "repairable":True,
        } for index in range(count)]
        self.applied = []
        self.synced = []

    def list_missing_tag_questions(self, _set_id, _include_approved=True):
        return list(self.rows)

    def backup_database(self, destination):
        return str(destination)

    def list_question_tags(self, _set_id, _subject="", _status=""):
        return [{"label":"胆石症检查"}]

    def apply_tag_backfill(self, question_id, tags, brief=None, *, sync_catalog=True):
        self.applied.append((question_id, list(tags), brief, sync_catalog))

    def sync_question_tags(self, set_id, subject=""):
        self.synced.append((set_id, subject))


class FakeTagBatchLLM:
    def __init__(self):
        self.calls = []

    def generate_tag_batch(self, rows, _catalog):
        self.calls.append(len(rows))
        return {str(row["id"]):{"tags":["胆石症检查"],"briefExplanation":""} for row in rows}


class DelayedLLM:
    def __init__(self, delay: float = 0.02):
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def generate_explanation(self, prompt, _evidence):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            # Reverse completion order without changing the question/result mapping.
            number = int(prompt.rsplit(" ", 1)[-1])
            time.sleep(self.delay + (3 - number % 3) * 0.002)
            return f"解析 {prompt}"
        finally:
            with self.lock:
                self.active -= 1


class BlockingLLM(DelayedLLM):
    def __init__(self):
        super().__init__(0)
        self.started = threading.Event()
        self.release = threading.Event()

    def generate_explanation(self, prompt, _evidence):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.started.set()
        try:
            self.release.wait(3)
            return f"解析 {prompt}"
        finally:
            with self.lock:
                self.active -= 1


class FakeControl:
    def __init__(self):
        self.value = "run"
        self.condition = threading.Condition()
        self.checkpoint_entered = threading.Event()
        self.reports = []

    def state(self):
        with self.condition:
            return self.value

    def report(self, current, total, message):
        self.reports.append((current, total, message))

    def checkpoint(self):
        self.checkpoint_entered.set()
        with self.condition:
            while self.value == "pause":
                self.condition.wait(0.05)
            if self.value == "cancel":
                raise CancelledError("任务已取消")

    def set(self, value):
        with self.condition:
            self.value = value
            self.condition.notify_all()


class GenerationAccelerationTests(unittest.TestCase):
    def test_external_surgery_alias_routes_to_surgery_textbook_and_keeps_subject(self):
        rows = questions(1)
        rows[0]["subject"] = "外基"

        class AliasDatabase(FakeDatabase):
            def list_libraries(self):
                return [
                    {"id": 2, "name": "外科学（第10版）", "subject": "外科学", "file_status": "ready", "file_count": 1},
                    {"id": 9, "name": "药理学（第10版）", "subject": "药理学", "file_status": "ready", "file_count": 1},
                ]

        class ScopedRetrieval:
            def __init__(self): self.calls = []
            def retrieve(self, subject, prompt, _vector, library_ids=None):
                self.calls.append((subject, tuple(library_ids or [])))
                return {
                    "is_matched": True, "top_score": .82,
                    "matched_chunks": [{"chunk_id":1,"text":f"外科学证据 {prompt}","score":.82,"source_file":"外科学.pdf","source_page":1}],
                }

        database = AliasDatabase(rows)
        service = GenerationService(database, config(2), FakeEmbedding(), DelayedLLM(0.001))
        scoped = ScopedRetrieval(); service.retrieval = scoped
        result = service.generate_set("set", library_ids=[2, 9])
        self.assertEqual(result["generated"], 1)
        self.assertEqual(result["subject_routes"], {"外基→外科学": 1})
        self.assertEqual(result["searched_library_count"], 2)
        self.assertEqual(scoped.calls[0], ("外科学", (2, 9)))
        self.assertEqual(rows[0]["subject"], "外基")

    def test_explicit_textbooks_are_one_co_primary_scope_at_normal_threshold(self):
        class ScopedRetrieval:
            def retrieve(self, _subject, _prompt, _vector, library_ids=None):
                ids = tuple(library_ids or [])
                score = .69
                return {
                    "is_matched": score >= .5, "top_score": score,
                    "matched_chunks": [{"chunk_id":ids[0],"text":"候选证据","score":score,"source_file":"教材.pdf","source_page":1}],
                }

        settings = config(1); settings["retrieval"]["similarity_threshold"] = .6
        service = GenerationService(FakeDatabase([]), settings, FakeEmbedding(), DelayedLLM())
        service.retrieval = ScopedRetrieval()
        question = {"subject":"外基","prompt_text":"病例题"}
        libraries = [
            {"id":2,"subject":"外科学"}, {"id":9,"subject":"药理学"},
        ]
        result = service.retrieve_with_subject_fallback(question, [1.0, 0.0], libraries, [2, 9])
        self.assertFalse(result["cross_subject"])
        self.assertEqual(result["scope_mode"], "explicit_co_primary")
        self.assertEqual(result["top_score"], .69)
        self.assertEqual(result["required_threshold"], .60)
        self.assertTrue(result["is_matched"])
        self.assertEqual(result["searched_library_ids"], [2, 9])

    def test_legacy_implicit_cross_subject_fallback_keeps_stricter_threshold(self):
        class ScopedRetrieval:
            def retrieve(self, _subject, _prompt, _vector, library_ids=None):
                ids = tuple(library_ids or [])
                score = .55 if ids == (2,) else .69
                return {
                    "is_matched": score >= .5, "top_score": score,
                    "matched_chunks": [{"chunk_id":ids[0],"text":"候选证据","score":score,"source_file":"教材.pdf","source_page":1}],
                }

        settings = config(1); settings["retrieval"]["similarity_threshold"] = .6
        service = GenerationService(FakeDatabase([]), settings, FakeEmbedding(), DelayedLLM())
        service.retrieval = ScopedRetrieval()
        question = {"subject":"外基","prompt_text":"病例题"}
        libraries = [{"id":2,"subject":"外科学"}, {"id":9,"subject":"内科学"}]
        service.config["retrieval"]["subject_fallback_map"] = {"外科学": ["内科学"]}
        result = service.retrieve_with_subject_fallback(question, [1.0, 0.0], libraries, None)
        self.assertTrue(result["cross_subject"])
        self.assertEqual(result["required_threshold"], .70)
        self.assertFalse(result["is_matched"])

    def test_unknown_subject_without_explicit_scope_is_no_library_before_embedding(self):
        rows = questions(1); rows[0]["subject"] = "未知专科"

        class UnrelatedDatabase(FakeDatabase):
            def list_libraries(self):
                return [{"id":3,"subject":"内科学","file_status":"ready","file_count":1}]

        embedding = FakeEmbedding(); database = UnrelatedDatabase(rows)
        result = GenerationService(database, config(1), embedding, DelayedLLM()).generate_set("set")
        self.assertEqual(result["generated"], 0)
        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(database.writes[0]["status"], "no_library")
        self.assertIn("no_compatible_library", database.writes[0]["error"])
        self.assertEqual(embedding.calls, [])

    def test_smart_route_uses_flash_thinking_and_pro_without_using_match_score(self):
        simple = {"prompt_text": "药动学的定义是？", "raw": {"type": "A1", "difficulty": "easy", "answer": "A"}}
        medium = {"prompt_text": "患者服药后出现皮疹，应如何处理？", "raw": {"type": "A2", "difficulty": "medium", "answer": "B"}}
        hard = {"prompt_text": "计算半衰期与清除率", "raw": {"type": "A3", "difficulty": "hard", "answer": "C"}}
        settings = {"smart_routing": True, "hard_use_pro": True, "hard_model": "deepseek-v4-pro"}
        self.assertEqual((route_generation(simple, {"top_score": .1}, settings).model, route_generation(simple, {"top_score": .95}, settings).thinking), ("deepseek-v4-flash", False))
        self.assertTrue(route_generation(medium, {}, settings).thinking)
        self.assertEqual(route_generation(hard, {}, settings).model, "deepseek-v4-pro")

    def test_pro_unavailable_falls_back_to_flash_thinking(self):
        class RoutedLLM:
            def __init__(self): self.routes = []
            def generate_explanation_package(self, _prompt, _evidence, _tags, *, route):
                self.routes.append((route.model, route.thinking))
                if route.model == "deepseek-v4-pro":
                    raise LLMRequestError("model unavailable", status_code=404, fatal_global=True)
                return {"explanation": "成功"}
        llm = RoutedLLM()
        route = GenerationRoute("deepseek-v4-pro", True, 4, ["hard_or_a3_a4"], 6000)
        outcome = generate_with_retry(
            llm, {"prompt_text": "题目"}, {"matched_chunks": []},
            AdaptiveConcurrencyController(2), 2, route=route,
            pro_gate=threading.BoundedSemaphore(1), sleep=lambda _value: None,
        )
        self.assertFalse(outcome.error)
        self.assertTrue(outcome.model_fallback)
        self.assertEqual(llm.routes, [("deepseek-v4-pro", True), ("deepseek-v4-flash", True)])

    def test_below_threshold_result_never_calls_textbook_generation(self):
        class CountingLLM:
            def __init__(self):
                self.calls = 0

            def generate_explanation(self, _prompt, _evidence):
                self.calls += 1
                return "不应生成"

        llm = CountingLLM()
        database, service = self.service(1, llm=llm)
        service.retrieval = BelowThresholdRetrieval()

        result = service.generate_set("set")

        self.assertEqual(llm.calls, 0)
        self.assertEqual(result["generated"], 0)
        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(database.writes[0]["status"], "unmatched")

    def test_saved_evidence_below_current_threshold_cannot_bypass_gate(self):
        row = questions(1)[0]
        row.update({
            "match_score": 0.49, "match_confidence": "standard",
            "evidence": [{"text": "旧证据", "source_file": "教材.pdf", "score": 0.49}],
        })
        database = FakeDatabase([row])

        class CountingLLM:
            calls = 0

            def generate_explanation(self, _prompt, _evidence):
                self.calls += 1
                return "不应生成"

        llm = CountingLLM()
        result = GenerationService(database, config(1), FakeEmbedding(), llm).generate_set(
            "set", reuse_saved_evidence=True,
        )

        self.assertEqual(llm.calls, 0)
        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(database.writes[0]["status"], "unmatched")

    def test_enabled_automatic_general_fallback_generates_only_after_low_match(self):
        class AutoDatabase(FakeDatabase):
            def general_generation_questions(self, _set_id, question_ids=None):
                selected = set(question_ids or [])
                return [{
                    **row, "pipeline_status": "unmatched", "match_score": 0.49,
                    "evidence": [{"text": "低分证据", "score": 0.49}], "raw": {},
                } for row in self.rows if not selected or row["id"] in selected]

            def save_generation_failure(self, question_id, status, error, **_kwargs):
                self.writes.append({"id": question_id, "status": status, "error": error})

        class FallbackLLM:
            def __init__(self):
                self.calls = 0

            def generate_fallback_package(self, prompt):
                self.calls += 1
                return {
                    "explanationBlocks": [
                        {"section": "answerBasis", "type": "paragraph", "title": "结论", "text": f"通识 {prompt}"},
                        {"section": "pitfalls", "type": "table", "title": "易错点", "columns": ["项目", "辨析"], "rows": [["其他项", "不符合"]]},
                    ],
                    "briefExplanation": "基于通识说明正确答案。", "knowledgePoints": [],
                    "tags": [], "suggestedTags": ["通识标签"], "explanation": f"通识 {prompt}",
                    "explanationMeta": {"mode": "general_knowledge", "evidence": []},
                }

        settings = config(1)
        settings["retrieval"]["generate_fallback"] = True
        database = AutoDatabase(questions(1))
        llm = FallbackLLM()
        service = GenerationService(database, settings, FakeEmbedding(), llm)
        service.retrieval = BelowThresholdRetrieval()

        result = service.generate_set("set")

        self.assertEqual(llm.calls, 1)
        self.assertEqual(result["auto_general_generated"], 1)
        self.assertEqual(result["generated"], 1)
        self.assertEqual(result["unmatched"], 0)
        self.assertEqual([item["status"] for item in database.writes], ["unmatched", "generated"])
        self.assertEqual(database.writes[-1]["mode"], "general_knowledge")

    def test_batch_general_generation_is_concurrent_and_keeps_main_thread_writes(self):
        class GeneralDatabase(FakeDatabase):
            def general_generation_questions(self, _set_id, _question_ids=None):
                return [{**row, "match_score": 0.0, "evidence": [], "raw": {}} for row in self.rows]

            def save_generated_result(self, question_id, status, mode, score, explanation, evidence, error_message="", package=None, write_tags=True):
                if threading.get_ident() != self.owner:
                    raise AssertionError("SQLite write escaped the generation main thread")
                self.writes.append({"id":question_id,"status":status,"mode":mode,"package":package})

        class GeneralLLM(DelayedLLM):
            def generate_fallback_package(self, prompt):
                with self.lock:
                    self.active += 1; self.peak=max(self.peak,self.active)
                try:
                    time.sleep(self.delay)
                    blocks=[{"section":"answerBasis","type":"paragraph","title":"结论","text":f"简析 {prompt}"}]
                    return {"explanationBlocks":blocks,"briefExplanation":f"简析 {prompt}","knowledgePoints":[],
                            "tags":[],"suggestedTags":["通识标签"],"explanation":f"简析 {prompt}",
                            "explanationMeta":{"mode":"general_knowledge","evidence":[]}}
                finally:
                    with self.lock:self.active-=1

        database=GeneralDatabase(questions(6));llm=GeneralLLM(0.02)
        result=GenerationService(database,config(3),None,llm).generate_general_batch("set")
        self.assertEqual(result["generated"],6)
        self.assertGreaterEqual(llm.peak,2)
        self.assertTrue(all(item["status"]=="generated" and item["package"]["suggestedTags"] for item in database.writes))

    def test_candidate_only_tags_do_not_turn_valid_textbook_packages_into_format_failures(self):
        class CandidateOnlyLLM:
            def generate_explanation_package(self, prompt, _evidence, _official_tags=None):
                blocks = [
                    {"section":"analysis","type":"paragraph","title":"考点","text":f"解析 {prompt}"},
                    {"section":"answerBasis","type":"paragraph","title":"依据","text":"教材支持正确答案"},
                    {"section":"pitfalls","type":"table","title":"易错点","columns":["项目","辨析"],"rows":[["其他项","不符合题意"]]},
                ]
                return {
                    "explanationBlocks":blocks, "briefExplanation":"教材证据支持正确答案。",
                    "tags":[], "suggestedTags":["药理作用机制"], "explanation":f"解析 {prompt}",
                    "explanationMeta":{"mode":"textbook","evidence":[]},
                }

        database, service = self.service(3, llm=CandidateOnlyLLM())
        result = service.generate_set("set")
        self.assertEqual(result["generated"], 3)
        self.assertEqual(result["failed"], 0)
        self.assertTrue(all(row["package"]["suggestedTags"] == ["药理作用机制"] for row in database.writes))

    def service(self, count=10, concurrency=3, embedding=None, llm=None):
        database = FakeDatabase(questions(count))
        service = GenerationService(
            database, config(concurrency), embedding or FakeEmbedding(), llm or DelayedLLM(),
        )
        service.retrieval = FakeRetrieval()
        return database, service

    def test_ten_questions_use_one_embedding_batch_and_main_thread_writes(self):
        embedding = FakeEmbedding()
        llm = DelayedLLM()
        database, service = self.service(10, embedding=embedding, llm=llm)
        result = service.generate_set("set")
        self.assertEqual([len(call) for call in embedding.calls], [10])
        self.assertEqual(len(database.writes), 10)
        self.assertLessEqual(llm.peak, 3)
        self.assertEqual(result["peak_concurrency"], llm.peak)

    def test_saved_evidence_retry_skips_embedding_and_retrieval(self):
        embedding = FakeEmbedding()
        rows = questions(2)
        for index, row in enumerate(rows, start=1):
            row.update({
                "match_score": 0.74,
                "match_confidence": "strong",
                "evidence": [{
                    "text": f"教材证据 {index}", "source_file": "教材.pdf",
                    "source_page": index, "score": 0.74,
                }],
            })
        database = FakeDatabase(rows)
        service = GenerationService(database, config(2), embedding, DelayedLLM(0.005))

        class ForbiddenRetrieval:
            def retrieve(self, *_args, **_kwargs):
                raise AssertionError("saved-evidence retry must not retrieve again")

        service.retrieval = ForbiddenRetrieval()
        result = service.generate_set("set", reuse_saved_evidence=True)
        self.assertEqual(embedding.calls, [])
        self.assertEqual(result["generated"], 2)
        self.assertEqual([item["score"] for item in database.writes], [0.74, 0.74])
        self.assertTrue(all(item["evidence"] for item in database.writes))

    def test_out_of_order_results_are_saved_to_the_correct_question(self):
        database, service = self.service(9)
        service.generate_set("set")
        by_id = {item["id"]: item for item in database.writes}
        for row in questions(9):
            self.assertEqual(by_id[row["id"]]["explanation"], f"解析 {row['prompt_text']}")

    def test_embedding_batch_failure_falls_back_and_isolates_bad_question(self):
        embedding = FakeEmbedding("题目 2")
        database, service = self.service(3, embedding=embedding)
        result = service.generate_set("set")
        statuses = {item["id"]: item["status"] for item in database.writes}
        self.assertEqual(statuses, {1: "generated", 2: "error", 3: "generated"})
        self.assertEqual(result["failed"], 1)
        self.assertEqual([len(call) for call in embedding.calls], [3, 1, 1, 1])

    def test_adaptive_controller_halves_and_ramps_after_success_window(self):
        controller = AdaptiveConcurrencyController(4, True, 2)
        controller.report_throttle(0)
        self.assertEqual(controller.snapshot()["limit"], 2)
        controller.report_success()
        controller.report_success()
        self.assertEqual(controller.snapshot()["limit"], 3)
        self.assertEqual(controller.snapshot()["throttle_events"], 1)

    def test_retryable_429_retries_at_lower_concurrency(self):
        class ThrottledLLM:
            def __init__(self):
                self.calls = 0

            def generate_explanation(self, _prompt, _evidence):
                self.calls += 1
                if self.calls == 1:
                    raise LLMRequestError("429", status_code=429, retryable=True)
                return "完成"

        client = ThrottledLLM()
        controller = AdaptiveConcurrencyController(4, True, 20)
        outcome = generate_with_retry(
            client, questions(1)[0], {"matched_chunks": []}, controller,
            max_retries=3, base_delay=0, sleep=lambda _seconds: None, random_value=lambda: 0,
        )
        self.assertEqual(outcome.explanation, "完成")
        self.assertEqual(client.calls, 2)
        self.assertEqual(controller.snapshot()["limit"], 2)

    def test_fatal_account_error_stops_job_without_marking_questions_error(self):
        class FatalLLM:
            def generate_explanation(self, _prompt, _evidence):
                raise LLMRequestError("余额不足", status_code=402, fatal_global=True)

        database, service = self.service(10, llm=FatalLLM())
        with self.assertRaises(FatalGenerationError):
            service.generate_set("set")
        self.assertEqual(database.writes, [])

    def test_three_way_pipeline_is_materially_faster_than_serial(self):
        def elapsed(concurrency):
            _database, service = self.service(12, concurrency=concurrency, llm=DelayedLLM(0.035))
            started = time.perf_counter()
            service.generate_set("set")
            return time.perf_counter() - started

        serial = elapsed(1)
        parallel = elapsed(3)
        self.assertGreater(serial / parallel, 2.3)

    def test_pause_drains_submitted_requests_before_waiting_and_resumes_without_duplicates(self):
        llm = BlockingLLM()
        database, service = self.service(10, llm=llm)
        control = FakeControl()
        errors = []

        def run():
            database.owner = threading.get_ident()
            try:
                service.generate_set("set", control)
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(llm.started.wait(1))
        control.set("pause")
        llm.release.set()
        self.assertTrue(control.checkpoint_entered.wait(2))
        self.assertGreater(len(database.writes), 0)
        self.assertLess(len(database.writes), 10)
        self.assertTrue(thread.is_alive())
        control.set("run")
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(database.writes), 10)
        self.assertEqual(len({item["id"] for item in database.writes}), 10)

    def test_cancel_preserves_inflight_results_and_leaves_unsubmitted_questions_untouched(self):
        llm = BlockingLLM()
        database, service = self.service(10, llm=llm)
        control = FakeControl()
        errors = []

        def run():
            database.owner = threading.get_ident()
            try:
                service.generate_set("set", control)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(llm.started.wait(1))
        control.set("cancel")
        llm.release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(any(isinstance(item, CancelledError) for item in errors))
        self.assertGreater(len(database.writes), 0)
        self.assertLess(len(database.writes), 10)
        self.assertTrue(all(item["status"] == "generated" for item in database.writes))

    def test_twenty_tag_backfills_use_one_batch_and_preserve_catalog_batching(self):
        database = FakeTagBackfillDatabase(20)
        llm = FakeTagBatchLLM()
        result = TagBackfillService(database, llm).run("set-id")
        self.assertEqual(llm.calls, [20])
        self.assertEqual(result["updated"], 20)
        self.assertEqual(len(database.applied), 20)
        self.assertTrue(all(item[3] is False for item in database.applied))
        self.assertEqual(database.synced, [("set-id", "外科学")])


if __name__ == "__main__":
    unittest.main()
