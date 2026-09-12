"""Contract tests for the QWebChannel bridge layer."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication

from db_manager import DatabaseManager
from ui_bridge.dashboard import DashboardBridge
from ui_bridge.jobs import JOB_PAYLOAD_SPECS, JobRunner, JobsBridge
from ui_bridge.protocol import BridgeBase, err, ok
from ui_bridge.settings import SETTINGS_DEFAULTS, SettingsBridge


class EchoBridge(BridgeBase):
    def api_ping(self, text: str = "") -> dict:
        return {"pong": text}

    def api_boom(self) -> None:
        raise ValueError("炸了")


class ProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        self.bridge = EchoBridge()

    def test_ok_envelope(self) -> None:
        payload = json.loads(ok({"a": 1}))
        self.assertEqual(payload, {"ok": True, "data": {"a": 1}})

    def test_err_envelope(self) -> None:
        payload = json.loads(err("some_code", "坏消息"))
        self.assertEqual(payload, {"ok": False, "error": {"code": "some_code", "message": "坏消息"}})

    def test_invoke_dispatches_to_api_method(self) -> None:
        payload = json.loads(self.bridge.invoke("ping", json.dumps({"text": "你好"})))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"], {"pong": "你好"})

    def test_invoke_unknown_method(self) -> None:
        payload = json.loads(self.bridge.invoke("nope", "{}"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "unknown_method")

    def test_invoke_bad_json_params(self) -> None:
        payload = json.loads(self.bridge.invoke("ping", "{not json"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "bad_params")

    def test_invoke_wraps_exceptions(self) -> None:
        payload = json.loads(self.bridge.invoke("boom", "{}"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "ValueError")
        self.assertIn("炸了", payload["error"]["message"])


class DashboardBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.bridge = DashboardBridge(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_counts_shape_on_empty_db(self) -> None:
        payload = json.loads(self.bridge.invoke("counts", "{}"))
        self.assertTrue(payload["ok"])
        self.assertEqual(
            set(payload["data"].keys()),
            {"libraries", "files", "questions", "waiting", "pending", "unmatched", "failed"},
        )
        self.assertTrue(all(isinstance(v, int) for v in payload["data"].values()))

    def test_counts_reflect_inserted_question(self) -> None:
        sample = {
            "id": "q_001", "subject": "内科学", "type": "A1",
            "question": "急性心肌梗死的典型表现是？",
            "options": ["A. 胸痛", "B. 皮疹"], "answer": "A",
        }
        with DatabaseManager(self.db_path) as db:
            db.create_question_set("测试集", "test.json", "json", [sample])
        payload = json.loads(self.bridge.invoke("counts", "{}"))
        self.assertEqual(payload["data"]["questions"], 1)

    def test_api_status_uses_env_credentials(self) -> None:
        saved = {k: os.environ.get(k) for k in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY")}
        os.environ["DEEPSEEK_API_KEY"] = "test-key"
        os.environ.pop("DASHSCOPE_API_KEY", None)
        try:
            payload = json.loads(self.bridge.invoke("api_status", "{}"))
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["data"]["deepseek"])
            self.assertFalse(payload["data"]["dashscope"])
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class JobsBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.runner = JobRunner(self.db_path)
        self.runner.tick = lambda: None  # isolate: never spawn real worker subprocesses in tests
        self.bridge = JobsBridge(self.db_path, self.runner)

    def tearDown(self) -> None:
        self.runner.shutdown()
        self.tmp.cleanup()

    def test_payload_specs_cover_worker_types(self) -> None:
        self.assertEqual(
            set(JOB_PAYLOAD_SPECS),
            {"scan", "generate", "general", "general_batch",
             "tag_backfill", "study_point_backfill", "memory_card_backfill", "upgrade_v2"},
        )

    def test_enqueue_writes_queued_job(self) -> None:
        payload = json.loads(self.bridge.invoke(
            "enqueue",
            json.dumps({"job_type": "generate", "title": "生成解析",
                        "payload": {"set_id": "s1", "content_types": ["explanation"]}}),
        ))
        self.assertTrue(payload["ok"])
        job_id = payload["data"]["job_id"]
        with DatabaseManager(self.db_path) as db:
            job = db.get_job(job_id)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["job_type"], "generate")
        self.assertEqual(job["payload"]["set_id"], "s1")

    def test_enqueue_rejects_missing_required_key(self) -> None:
        payload = json.loads(self.bridge.invoke(
            "enqueue", json.dumps({"job_type": "scan", "title": "扫描", "payload": {}}),
        ))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "bad_payload")
        self.assertIn("library_id", payload["error"]["message"])

    def test_enqueue_rejects_unknown_type(self) -> None:
        payload = json.loads(self.bridge.invoke(
            "enqueue", json.dumps({"job_type": "hack", "title": "x", "payload": {}}),
        ))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "bad_payload")

    def test_control_pause_resume_cancel(self) -> None:
        job_id = json.loads(self.bridge.invoke(
            "enqueue",
            json.dumps({"job_type": "general", "title": "通识", "payload": {"question_pk": 1}}),
        ))["data"]["job_id"]
        for action, expected in (("pause", "pause"), ("resume", "run"), ("cancel", "cancel")):
            result = json.loads(self.bridge.invoke("control", json.dumps({"job_id": job_id, "action": action})))
            self.assertTrue(result["ok"])
            with DatabaseManager(self.db_path) as db:
                self.assertEqual(db.get_job(job_id)["control"], expected)

    def test_control_rejects_bad_action(self) -> None:
        payload = json.loads(self.bridge.invoke(
            "control", json.dumps({"job_id": "x", "action": "explode"}),
        ))
        self.assertFalse(payload["ok"])

    def test_control_rejects_unknown_job(self) -> None:
        payload = json.loads(self.bridge.invoke(
            "control", json.dumps({"job_id": "no-such-job", "action": "pause"}),
        ))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_control_rejects_resume_when_not_paused(self) -> None:
        job_id = json.loads(self.bridge.invoke(
            "enqueue",
            json.dumps({"job_type": "general", "title": "通识", "payload": {"question_pk": 1}}),
        ))["data"]["job_id"]
        payload = json.loads(self.bridge.invoke(
            "control", json.dumps({"job_id": job_id, "action": "resume"}),
        ))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "bad_state")

    def test_list_returns_jobs(self) -> None:
        self.bridge.invoke("enqueue", json.dumps(
            {"job_type": "general", "title": "通识", "payload": {"question_pk": 1}}))
        payload = json.loads(self.bridge.invoke("list", "{}"))
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["data"]["jobs"]), 1)
        self.assertEqual(payload["data"]["jobs"][0]["title"], "通识")

    def test_runner_does_not_launch_when_none_queued(self) -> None:
        JobRunner.tick(self.runner)  # bypass the instance stub to exercise real tick logic
        self.assertEqual(self.runner.process.state(), QProcess.NotRunning)


class SettingsBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.bridge = SettingsBridge(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_get_returns_defaults_and_credential_flags(self) -> None:
        payload = json.loads(self.bridge.invoke("get", "{}"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["settings"], SETTINGS_DEFAULTS)
        self.assertEqual(set(payload["data"]["credentials"]), {"deepseek", "dashscope", "claude", "gemini", "custom"})

    def test_save_roundtrip_and_unknown_key_rejected(self) -> None:
        result = json.loads(self.bridge.invoke("save", json.dumps({
            "settings": {"similarity_threshold": "0.5", "generation_concurrency": "4"},
            "credentials": {},
        })))
        self.assertTrue(result["ok"])
        with DatabaseManager(self.db_path) as db:
            self.assertEqual(db.get_setting("similarity_threshold"), "0.5")
            self.assertEqual(db.get_setting("generation_concurrency"), "4")
        bad = json.loads(self.bridge.invoke("save", json.dumps({
            "settings": {"evil_key": "1"}, "credentials": {},
        })))
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["code"], "bad_settings")

    def test_save_credentials_via_env_backed_store(self) -> None:
        # CredentialStore prefers env vars; saving must not crash when keyring is absent.
        result = json.loads(self.bridge.invoke("save", json.dumps({
            "settings": {}, "credentials": {"deepseek": "  "},
        })))
        self.assertTrue(result["ok"])

    def test_api_test_starts_background_check(self) -> None:
        self.bridge._run_test = lambda *args: None  # isolate: no real network in tests
        result = json.loads(self.bridge.invoke("test", json.dumps({"provider": "deepseek"})))
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["started"])
        self.assertTrue(result["data"]["request_id"])
        bad = json.loads(self.bridge.invoke("test", json.dumps({"provider": "hack"})))
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["code"], "bad_provider")


from ui_bridge.imports import ImportsBridge

IMPORT_SAMPLE = {
    "id": "q1", "subject": "外科学", "type": "A1",
    "question": "题干", "options": ["A. 甲", "B. 乙"], "answer": "A",
}


class ImportsBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        # runtime_config() 经 app_paths.database_path() 读取该环境变量
        os.environ["MEDEXPLAIN_DATABASE_PATH"] = self.db_path
        self.bridge = ImportsBridge(self.db_path)

    def tearDown(self) -> None:
        os.environ.pop("MEDEXPLAIN_DATABASE_PATH", None)
        self.tmp.cleanup()

    def _write_json(self, questions: list[dict]) -> str:
        path = Path(self.tmp.name) / "外科学题库.json"
        path.write_text(json.dumps(questions, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def _write_xlsx(self) -> str:
        import openpyxl
        path = Path(self.tmp.name) / "题库.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["题号", "学科", "题干", "答案", "A", "B"])
        sheet.append(["x1", "外科学", "题干内容", "A", "甲", "乙"])
        workbook.save(path)
        return str(path)

    def test_inspect_json_file(self) -> None:
        path = self._write_json([IMPORT_SAMPLE])
        payload = json.loads(self.bridge.invoke("inspect_file", json.dumps({"path": path})))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["kind"], "json")
        self.assertEqual(payload["data"]["name"], "外科学题库")

    def test_inspect_excel_file_returns_auto_mapping(self) -> None:
        path = self._write_xlsx()
        payload = json.loads(self.bridge.invoke("inspect_file", json.dumps({"path": path})))
        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["kind"], "excel")
        self.assertIn("题号", data["headers"])
        self.assertEqual(data["mapping"]["id"], "题号")
        self.assertIn("options", data["fields"])

    def test_inspect_rejects_other_suffix(self) -> None:
        path = Path(self.tmp.name) / "notes.txt"
        path.write_text("text", encoding="utf-8")
        payload = json.loads(self.bridge.invoke("inspect_file", json.dumps({"path": str(path)})))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "bad_payload")

    def test_import_file_json_success(self) -> None:
        path = self._write_json([IMPORT_SAMPLE])
        payload = json.loads(self.bridge.invoke(
            "import_file", json.dumps({"path": path, "name": "外科学题库"})))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["question_count"], 1)
        sets = json.loads(self.bridge.invoke("list_sets", "{}"))
        self.assertEqual(sets["data"]["sets"][0]["id"], payload["data"]["set_id"])

    def test_import_file_validation_errors_open_review_without_inserting(self) -> None:
        path = self._write_json([{**IMPORT_SAMPLE, "answer": ""}])
        payload = json.loads(self.bridge.invoke(
            "import_file", json.dumps({"path": path, "name": "坏题"})))
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["data"]["needs_review"])
        review = payload["data"]["review"]
        self.assertEqual(review["issues"], 1)
        self.assertIn("answer", "\n".join(review["items"][0]["errors"]))
        blocked = json.loads(self.bridge.invoke("commit_file_import", json.dumps({"draft_id": review["draft_id"]})))
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"]["code"], "import_invalid")
        self.assertEqual(json.loads(self.bridge.invoke("list_sets", "{}"))["data"]["sets"], [])

    def test_import_file_requires_name(self) -> None:
        path = self._write_json([IMPORT_SAMPLE])
        payload = json.loads(self.bridge.invoke(
            "import_file", json.dumps({"path": path, "name": "  "})))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "bad_payload")

    def test_upgrade_estimate_counts_eligible_questions(self) -> None:
        with DatabaseManager(self.db_path) as database:
            set_id = database.create_question_set("集", "source.json", "json", [IMPORT_SAMPLE])
        payload = json.loads(self.bridge.invoke("upgrade_estimate", json.dumps({"set_id": set_id})))
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["data"], {"questions": 1, "estimated_requests": 0, "skipped": 1})

    def test_read_organize_source_text_file(self) -> None:
        path = Path(self.tmp.name) / "题目.txt"
        path.write_text("1. 题干\nA. 甲\nB. 乙\n答案：A", encoding="utf-8")
        payload = json.loads(self.bridge.invoke("read_organize_source", json.dumps({"path": str(path)})))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["mode"], "text")
        self.assertIn("答案：A", payload["data"]["text"])

    def test_read_organize_source_structured_docx_by_name(self) -> None:
        path = Path(self.tmp.name) / "内科学_题目集合.docx"
        path.write_bytes(b"")  # 识别只看文件名，不打开文档
        payload = json.loads(self.bridge.invoke("read_organize_source", json.dumps({"path": str(path)})))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["mode"], "structured")
        self.assertEqual(payload["data"]["kind"], "structured-docx")
        self.assertEqual(payload["data"]["subject"], "内科学")

    def test_read_organize_source_rejects_summary_docx(self) -> None:
        path = Path(self.tmp.name) / "question-set 统计汇总.docx"
        path.write_bytes(b"")
        payload = json.loads(self.bridge.invoke("read_organize_source", json.dumps({"path": str(path)})))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "bad_payload")

    def test_organize_local_rules_roundtrip(self) -> None:
        from PySide6.QtCore import Qt
        results: list[dict] = []
        self.bridge.organize_result.connect(
            lambda raw: results.append(json.loads(raw)), Qt.DirectConnection)
        self.bridge._run_organize(
            "t1", "1. 急性心肌梗死最常见的症状是？\nA. 胸痛\nB. 皮疹\n答案：A",
            "内科学", False, "")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[0]["token"], "t1")
        self.assertEqual(results[0]["data"]["method"], "local")
        self.assertEqual(results[0]["data"]["rows"][0]["answer"], "A")

    def test_organize_failure_reports_token(self) -> None:
        # 不存在的结构化文件会让 organize_file 抛异常；注意 use_ai=False 时
        # 纯文本路径即使识别为空也不会抛错，所以用缺失文件覆盖失败分支。
        from PySide6.QtCore import Qt
        results: list[dict] = []
        self.bridge.organize_result.connect(
            lambda raw: results.append(json.loads(raw)), Qt.DirectConnection)
        missing = str(Path(self.tmp.name) / "内科学_题目集合.docx")  # 不创建文件
        self.bridge._run_organize("t2", "", "", False, missing)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["token"], "t2")
        self.assertEqual(results[0]["error"]["code"], "organize_failed")

    def test_organize_requires_input(self) -> None:
        payload = json.loads(self.bridge.invoke("organize", json.dumps({"token": "t3"})))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "bad_payload")

    def test_validate_rows_flags_duplicate_and_imported_ids(self) -> None:
        rows = [dict(IMPORT_SAMPLE), {**IMPORT_SAMPLE, "question": "另一题"}]
        payload = json.loads(self.bridge.invoke("validate_rows", json.dumps(
            {"rows": rows, "imported_ids": ["q1"]})))
        self.assertTrue(payload["ok"])
        warnings = payload["data"]["warnings"]
        self.assertTrue(any("重复" in item for item in warnings[1]))
        self.assertTrue(any("已导入" in item for item in warnings[0]))
        self.assertEqual(payload["data"]["valid_count"], 0)

    def test_import_rows_create_then_append(self) -> None:
        first = json.loads(self.bridge.invoke("import_rows", json.dumps(
            {"name": "整理集", "rows": [IMPORT_SAMPLE], "source_path": "pasted://manual"})))
        self.assertTrue(first["ok"])
        set_id = first["data"]["set_id"]
        second = json.loads(self.bridge.invoke("import_rows", json.dumps(
            {"rows": [{**IMPORT_SAMPLE, "id": "q2"}], "set_id": set_id})))
        self.assertTrue(second["ok"])
        with DatabaseManager(self.db_path) as database:
            sets = database.list_question_sets()
        self.assertEqual(sets[0]["question_count"], 2)

    def test_import_rows_duplicate_id_is_import_invalid(self) -> None:
        payload = json.loads(self.bridge.invoke("import_rows", json.dumps(
            {"name": "整理集", "rows": [dict(IMPORT_SAMPLE), dict(IMPORT_SAMPLE)]})))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "import_invalid")

    def test_save_rows_json_and_issue_report(self) -> None:
        json_path = str(Path(self.tmp.name) / "导出.json")
        saved = json.loads(self.bridge.invoke("save_rows_json", json.dumps(
            {"path": json_path, "rows": [IMPORT_SAMPLE]})))
        self.assertTrue(saved["ok"])
        self.assertEqual(json.loads(Path(json_path).read_text(encoding="utf-8"))[0]["id"], "q1")

        bad = {**IMPORT_SAMPLE, "id": "q9", "answer": ""}
        csv_path = str(Path(self.tmp.name) / "报告.csv")
        report = json.loads(self.bridge.invoke("save_issue_report", json.dumps(
            {"path": csv_path, "rows": [IMPORT_SAMPLE, bad]})))
        self.assertTrue(report["ok"])
        self.assertEqual(report["data"]["count"], 1)
        content = Path(csv_path).read_text(encoding="utf-8-sig")
        self.assertIn("题目序号", content)
        self.assertIn("q9", content)


from ui_bridge.questions import QuestionsBridge

QUESTION_SAMPLE = {
    "id": "q1", "subject": "外科学", "type": "A1",
    "question": "题干", "options": ["A. 甲", "B. 乙"], "answer": "A",
}


class QuestionsBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        os.environ["MEDEXPLAIN_DATABASE_PATH"] = self.db_path
        self.bridge = QuestionsBridge(self.db_path)
        with DatabaseManager(self.db_path) as database:
            self.set_a = database.create_question_set("集A", "a.json", "json", [
                {**QUESTION_SAMPLE, "id": "a1", "subject": "外科学",
                 "questionSource": "医考帮", "tags": ["休克"], "question": "休克处理？"},
                {**QUESTION_SAMPLE, "id": "a2", "subject": "内科学",
                 "questionSource": "本校历年题", "tags": ["心衰"], "question": "心衰诱因？"},
            ])
            self.set_b = database.create_question_set("集B", "b.json", "json", [
                {**QUESTION_SAMPLE, "id": "b1", "subject": "外科学", "question": "补液原则？"},
            ])

    def tearDown(self) -> None:
        os.environ.pop("MEDEXPLAIN_DATABASE_PATH", None)
        self.tmp.cleanup()

    def _invoke(self, method: str, **params) -> dict:
        return json.loads(self.bridge.invoke(method, json.dumps(params)))

    def test_list_spans_sets_with_total(self) -> None:
        payload = self._invoke("list")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["total"], 3)
        self.assertEqual([row["external_id"] for row in payload["data"]["rows"]], ["a1", "a2", "b1"])

    def test_list_filters_and_pagination(self) -> None:
        payload = self._invoke("list", subject="外科学", tag="休克", limit=1, offset=0)
        rows = payload["data"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["external_id"], "a1")
        self.assertEqual(payload["data"]["total"], 1)
        self.assertEqual(rows[0]["tags"], ["休克"])
        self.assertEqual(rows[0]["question_source"], "医考帮")
        payload = self._invoke("list", pipeline_status="matched_generation_error")
        self.assertEqual(payload["data"]["total"], 0)

    def test_ids_and_filters_endpoints(self) -> None:
        ids = self._invoke("ids", subject="外科学")["data"]["ids"]
        self.assertEqual(len(ids), 2)
        options = self._invoke("filters")["data"]
        self.assertEqual(options["subjects"], ["内科学", "外科学"])
        self.assertEqual(options["sources"], ["医考帮", "本校历年题"])
        self.assertEqual({item["name"] for item in options["sets"]}, {"集A", "集B"})

    def test_detail_and_not_found(self) -> None:
        first = self._invoke("list")["data"]["rows"][0]
        detail = self._invoke("detail", question_pk=first["id"])["data"]
        self.assertEqual(detail["raw"]["question"], "休克处理？")
        missing = self._invoke("detail", question_pk=99999)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error"]["code"], "not_found")

    def test_bulk_set_subject(self) -> None:
        target = self._invoke("list", search="心衰")["data"]["rows"][0]
        payload = self._invoke("bulk_set_subject", question_pks=[target["id"]], subject="外科学")
        self.assertEqual(payload["data"], {"updated": 1})
        self.assertEqual(self._invoke("list", subject="外科学")["data"]["total"], 3)
        bad = self._invoke("bulk_set_subject", question_pks=[target["id"]], subject="  ")
        self.assertEqual(bad["error"]["code"], "bad_payload")

    def test_move_to_trash_and_delete(self) -> None:
        rows = self._invoke("list")["data"]["rows"]
        moved = self._invoke("move_to_trash", question_pks=[rows[0]["id"]])
        self.assertEqual(moved["data"], {"moved": 1})
        self.assertEqual(self._invoke("list")["data"]["total"], 2)
        deleted = self._invoke("delete", question_pks=[rows[1]["id"]])
        self.assertEqual(deleted["data"], {"deleted": 1})
        self.assertEqual(self._invoke("list")["data"]["total"], 1)

    def test_export_selected_writes_v2_json(self) -> None:
        from question_format_v2 import is_v2
        rows = self._invoke("list")["data"]["rows"]
        path = str(Path(self.tmp.name) / "导出.json")
        payload = self._invoke("export_selected", question_pks=[rows[0]["id"], rows[2]["id"]], path=path)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["count"], 2)
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertTrue(is_v2(envelope))
        self.assertEqual(envelope["meta"]["questionCount"], 2)
        empty = self._invoke("export_selected", question_pks=[], path=path)
        self.assertEqual(empty["error"]["code"], "bad_payload")

    def test_trash_list_restore_and_purge(self) -> None:
        rows = self._invoke("list")["data"]["rows"]
        self._invoke("move_to_trash", question_pks=[rows[0]["id"]])
        trash = self._invoke("trash_list")["data"]
        self.assertEqual(trash["total"], 1)
        entry = trash["rows"][0]
        self.assertEqual(entry["external_id"], "a1")
        self.assertEqual(entry["set_name"], "集A")
        self.assertIn("休克处理", entry["prompt_text"])
        self.assertNotIn("snapshot_json", entry)

        restored = self._invoke("trash_restore", trash_ids=[entry["id"]])["data"]
        self.assertEqual(restored, {"restored": 1, "conflicts": []})
        self.assertEqual(self._invoke("list")["data"]["total"], 3)

        # 恢复会插入新行（新主键），重新取当前主键再移入回收站
        restored_pk = self._invoke("list", search="休克")["data"]["rows"][0]["id"]
        self._invoke("move_to_trash", question_pks=[restored_pk])
        entry = self._invoke("trash_list")["data"]["rows"][0]
        purged = self._invoke("trash_purge", trash_ids=[entry["id"]])["data"]
        self.assertEqual(purged, {"purged": 1})
        self.assertEqual(self._invoke("trash_list")["data"]["total"], 0)

    def test_trash_restore_conflict_keeps_entry(self) -> None:
        rows = self._invoke("list")["data"]["rows"]
        self._invoke("move_to_trash", question_pks=[rows[0]["id"]])
        entry = self._invoke("trash_list")["data"]["rows"][0]
        with DatabaseManager(self.db_path) as database:
            # 同 (set_id, external_id) 题目重新导入后，恢复必须报冲突并保留回收站条目
            database.append_questions_to_set(self.set_a, [{**QUESTION_SAMPLE, "id": "a1"}])
        result = self._invoke("trash_restore", trash_ids=[entry["id"]])["data"]
        self.assertEqual(result["restored"], 0)
        self.assertEqual(result["conflicts"], ["a1"])
        self.assertEqual(self._invoke("trash_list")["data"]["total"], 1)

    def test_trash_search_count_matches_list(self) -> None:
        rows = self._invoke("list")["data"]["rows"]
        self._invoke("move_to_trash", question_pks=[rows[0]["id"], rows[1]["id"]])
        self.assertEqual(self._invoke("trash_list", search="心衰")["data"]["total"], 1)


from ui_bridge.review import ReviewBridge

REVIEW_SAMPLE = {
    "id": "r1", "subject": "外科学", "type": "A1",
    "question": "休克的首选处理？", "options": ["A. 补液", "B. 止血"], "answer": "A",
    "explanation": "### 考点解析\n\n补液是首选。",
    # 标签治理要求同集同学科 >=3 题共用才保留在 raw["tags"]（否则降级为 suggestedTags），
    # 故夹具三题都带该标签
    "tags": ["休克"],
}


class ReviewBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        os.environ["MEDEXPLAIN_DATABASE_PATH"] = self.db_path
        self.bridge = ReviewBridge(self.db_path)
        with DatabaseManager(self.db_path) as database:
            self.set_id = database.create_question_set("外科", "a.json", "json", [
                dict(REVIEW_SAMPLE),
                {**REVIEW_SAMPLE, "id": "r2", "question": "第二题？"},
                {**REVIEW_SAMPLE, "id": "r3", "question": "第三题？"},
            ])
            rows = database.list_questions_global()
            self.pk1, self.pk2 = rows[0]["id"], rows[1]["id"]
            database.conn.execute(
                "UPDATE imported_questions SET pipeline_status='generated', generation_mode='textbook',"
                " match_score=0.82, explanation=? WHERE id=?",
                ("### 考点解析\n\n补液是首选。", self.pk1))
            database.conn.commit()

    def tearDown(self) -> None:
        os.environ.pop("MEDEXPLAIN_DATABASE_PATH", None)
        self.tmp.cleanup()

    def _invoke(self, method: str, **params) -> dict:
        return json.loads(self.bridge.invoke(method, json.dumps(params)))

    def test_list_default_review_queue(self) -> None:
        payload = self._invoke("list", set_id=self.set_id, review_status="pending", pipeline_status="generated")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["total"], 1)
        self.assertEqual(payload["data"]["rows"][0]["external_id"], "r1")

    def test_open_marks_viewed(self) -> None:
        detail = self._invoke("open", question_pk=self.pk1)["data"]
        self.assertEqual(detail["raw"]["question"], "休克的首选处理？")
        with DatabaseManager(self.db_path) as database:
            row = database.get_imported_question(self.pk1)
        self.assertIsNotNone(row["viewed_at"])
        missing = self._invoke("open", question_pk=99999)
        self.assertEqual(missing["error"]["code"], "not_found")

    def test_save_updates_blocks_and_preserves_review_state(self) -> None:
        blocks = [{"blockId": "b01-x", "section": "analysis", "type": "paragraph",
                   "title": "考点解析", "text": "先补液。"}]
        payload = self._invoke("save", question_pk=self.pk1, explanation_blocks=blocks,
                               tags=["休克"], brief_explanation="一句话")
        self.assertTrue(payload["ok"])
        with DatabaseManager(self.db_path) as database:
            row = database.get_imported_question(self.pk1)
        self.assertIn("先补液", row["explanation"])
        self.assertEqual(row["review_status"], "pending")
        self.assertEqual(row["pipeline_status"], "generated")
        self.assertEqual(row["raw"]["tags"], ["休克"])

    def test_save_none_fields_keep_original(self) -> None:
        self._invoke("save", question_pk=self.pk1, explanation_blocks=[
            {"blockId": "b01-x", "section": "analysis", "type": "paragraph", "title": "考点解析", "text": "甲"}],
            tags=["休克"])
        # 不传 tags/mnemonic（None）时保留原值
        self._invoke("save", question_pk=self.pk1, explanation_blocks=[
            {"blockId": "b01-x", "section": "analysis", "type": "paragraph", "title": "考点解析", "text": "乙"}])
        with DatabaseManager(self.db_path) as database:
            row = database.get_imported_question(self.pk1)
        self.assertEqual(row["raw"]["tags"], ["休克"])
        self.assertIn("乙", row["explanation"])

    def test_review_requires_open_and_explanation(self) -> None:
        # 未打开（viewed_at 为空）批准 → bad_state
        blocked = self._invoke("review", question_pk=self.pk2, action="approved", explanation="有解析")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"]["code"], "bad_state")
        bad_action = self._invoke("review", question_pk=self.pk1, action="maybe", explanation="x")
        self.assertEqual(bad_action["error"]["code"], "bad_payload")
        self._invoke("open", question_pk=self.pk1)
        done = self._invoke("review", question_pk=self.pk1, action="approved", explanation="### 考点解析\n\n补液。")
        self.assertEqual(done["data"], {"reviewed": "approved"})
        with DatabaseManager(self.db_path) as database:
            row = database.get_imported_question(self.pk1)
            actions = database.conn.execute(
                "SELECT action FROM review_actions WHERE question_pk=?", (self.pk1,)).fetchall()
        self.assertEqual(row["review_status"], "approved")
        self.assertEqual(actions[0][0], "approved")

    def test_regenerate_queues_and_enqueues_job(self) -> None:
        payload = self._invoke("regenerate", question_pk=self.pk1)
        self.assertTrue(payload["ok"])
        with DatabaseManager(self.db_path) as database:
            row = database.get_imported_question(self.pk1)
            job = database.get_job(payload["data"]["job_id"])
        self.assertEqual(row["pipeline_status"], "queued")
        self.assertEqual(job["job_type"], "generate")
        import json as _json
        job_payload = _json.loads(job["payload_json"])
        self.assertEqual(job_payload["question_ids"], [self.pk1])
        self.assertNotIn("library_ids", job_payload)

    def test_pdf_info_registered_only(self) -> None:
        pdf = Path(self.tmp.name) / "book.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        with DatabaseManager(self.db_path) as database:
            library_id = database.add_library("外科教材", "外科学", str(pdf), "第10版")
            database.upsert_file_metadata(library_id, str(pdf.resolve()), pdf.name, 10, 0, "hash")
            # upsert_file_metadata 的第 4/5 位参数是 file_size/modified_ns，
            # page_count 由索引流程单独写入，这里用 SQL 模拟索引完成后的状态
            database.conn.execute(
                "UPDATE textbook_files SET page_count=10 WHERE absolute_path=?", (str(pdf.resolve()),))
            database.conn.commit()
        ok = self._invoke("pdf_info", path=str(pdf.resolve()))
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["data"]["page_count"], 10)
        missing = self._invoke("pdf_info", path="D:/not-a-textbook.pdf")
        self.assertEqual(missing["error"]["code"], "not_found")

    def _make_generated_question(self, external_id: str, subject: str = "外科学",
                                 score: float = 0.85) -> int:
        """追加一道 generated+pending、教材模式、结构完整的题目，返回 pk。"""
        explanation = (
            "### 考点解析\n\n补液是首选。" + "长" * 80 + "\n\n"
            "### 正确答案依据\n\n依据。\n\n### 易错点提示\n\n| 项目 | 说明 |\n| 甲 | 乙 |"
        )
        raw = {**REVIEW_SAMPLE, "id": external_id, "subject": subject,
               "tags": ["休克"], "briefExplanation": "一句话简析"}
        with DatabaseManager(self.db_path) as database:
            database.append_questions_to_set(self.set_id, [raw])
            pk = database.list_questions_global(search=external_id)[0]["id"]
            evidence = [{"textbook": "外科学", "source_file": "book.pdf", "source_page": 508,
                         "pdf_page": 547, "score": score,
                         "text": "教材原文摘录内容，需要至少二十个字符才能通过快速审核门槛校验。"}]
            blocks = [
                {"blockId": "b01", "section": "analysis", "type": "paragraph", "title": "考点解析", "text": "补液是首选。" + "长" * 80},
                {"blockId": "b02", "section": "answerBasis", "type": "paragraph", "title": "正确答案依据", "text": "依据。"},
                {"blockId": "b03", "section": "pitfalls", "type": "table", "title": "易错点提示",
                 "columns": ["项目", "说明"], "rows": [["甲", "乙"]]},
            ]
            raw_json = {**raw, "explanationBlocks": blocks,
                        "explanationMeta": {"mode": "textbook", "evidenceGrade": "A",
                                            "score": score, "evidence": evidence}}
            database.conn.execute(
                "UPDATE imported_questions SET pipeline_status='generated', generation_mode='textbook',"
                " match_score=?, explanation=?, evidence_json=?, raw_json=? WHERE id=?",
                (score, explanation, json.dumps(evidence, ensure_ascii=False),
                 json.dumps(raw_json, ensure_ascii=False), pk))
            database.conn.commit()
        return pk

    def test_quick_review_preview_and_approve(self) -> None:
        pk = self._make_generated_question("qr1")
        preview = self._invoke("quick_review_preview", set_id=self.set_id, minimum_score=0.75)
        self.assertTrue(preview["ok"], preview)
        # setUp 的 pk1 也是 generated+pending（解析过短被排除），total 含它
        self.assertEqual(preview["data"]["total"], 2)
        self.assertEqual(len(preview["data"]["eligible"]), 1)
        self.assertEqual(preview["data"]["excluded"], {"解析过短": 1})
        approved = self._invoke("quick_review_approve", set_id=self.set_id,
                                minimum_score=0.75, expected_ids=[pk])
        self.assertEqual(approved["data"]["approved"], 1)
        with DatabaseManager(self.db_path) as database:
            row = database.get_imported_question(pk)
            self.assertEqual(row["review_status"], "approved")
            self.assertEqual(database.get_setting("quick_review_min_score"), "0.75")
        empty_set = self._invoke("quick_review_preview", set_id="")
        self.assertEqual(empty_set["error"]["code"], "bad_payload")

    def test_quick_review_preview_reads_saved_threshold(self) -> None:
        with DatabaseManager(self.db_path) as database:
            database.set_setting("quick_review_min_score", "0.88")
        preview = self._invoke("quick_review_preview", set_id=self.set_id)
        self.assertEqual(preview["data"]["minimum_score"], 0.88)

    def test_general_authorize_requires_unmatched(self) -> None:
        # 夹具修正：pk2 默认是 queued，先把夹具置为 unmatched 以满足授权预检（断言不变）
        with DatabaseManager(self.db_path) as database:
            database.conn.execute(
                "UPDATE imported_questions SET pipeline_status='unmatched' WHERE id=?", (self.pk2,))
            database.conn.commit()
        ok = self._invoke("general_authorize", question_pk=self.pk2)
        self.assertTrue(ok["ok"])
        with DatabaseManager(self.db_path) as database:
            job = database.get_job(ok["data"]["job_id"])
        self.assertEqual(job["job_type"], "general")
        blocked = self._invoke("general_authorize", question_pk=self.pk1)
        self.assertEqual(blocked["error"]["code"], "bad_state")

    def test_general_batch_preview_counts_only_unmatched(self) -> None:
        payload = self._invoke("general_batch_preview", set_id=self.set_id)
        # pk1 是 generated、pk2 是 queued：都不是 unmatched/no_library
        self.assertEqual(payload["data"], {"count": 0})
        with DatabaseManager(self.db_path) as database:
            database.conn.execute(
                "UPDATE imported_questions SET pipeline_status='unmatched' WHERE id=?", (self.pk2,))
            database.conn.commit()
        payload = self._invoke("general_batch_preview", set_id=self.set_id)
        self.assertEqual(payload["data"], {"count": 1})

    def test_regenerate_batch_queues_and_enqueues(self) -> None:
        payload = self._invoke("regenerate_batch", set_id=self.set_id,
                               question_ids=[self.pk1], library_ids=[3, 7])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["count"], 1)
        with DatabaseManager(self.db_path) as database:
            row = database.get_imported_question(self.pk1)
            job = database.get_job(payload["data"]["job_id"])
            job_payload = json.loads(job["payload_json"])
        self.assertEqual(row["pipeline_status"], "queued")
        self.assertEqual(job_payload["library_ids"], [3, 7])
        self.assertEqual(job_payload["question_ids"], [self.pk1])

    def test_backfill_estimate(self) -> None:
        payload = self._invoke("backfill_estimate", kind="tags", set_id=self.set_id)
        self.assertTrue(payload["ok"])
        self.assertIn("estimated_requests", payload["data"])
        self.assertEqual(payload["data"]["kind"], "tags")
        bad = self._invoke("backfill_estimate", kind="nope", set_id=self.set_id)
        self.assertEqual(bad["error"]["code"], "bad_payload")

    def test_generation_libraries_roundtrip(self) -> None:
        self.assertEqual(self._invoke("get_generation_libraries")["data"], {"library_ids": []})
        saved = self._invoke("save_generation_libraries", library_ids=[3, 7])
        self.assertEqual(saved["data"], {"saved": True})
        self.assertEqual(self._invoke("get_generation_libraries")["data"], {"library_ids": [3, 7]})


from index_profile import build_index_profile, index_fingerprint
from runtime_config import runtime_config
from ui_bridge.library import LibraryBridge


class LibraryBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        os.environ["MEDEXPLAIN_DATABASE_PATH"] = self.db_path
        self.bridge = LibraryBridge(self.db_path)
        self.pdf = Path(self.tmp.name) / "外科学.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake")

    def tearDown(self) -> None:
        os.environ.pop("MEDEXPLAIN_DATABASE_PATH", None)
        self.tmp.cleanup()

    def _invoke(self, method: str, **params) -> dict:
        return json.loads(self.bridge.invoke(method, json.dumps(params)))

    def _add_library(self) -> int:
        payload = self._invoke(
            "add", name="外科学（第10版）", subject="外科学",
            root_path=str(self.pdf), version="第10版", page_offset=39)
        self.assertTrue(payload["ok"], payload)
        return payload["data"]["library_id"]

    def test_add_and_list_with_index_state_none(self) -> None:
        library_id = self._add_library()
        self.assertIsInstance(library_id, int)
        payload = self._invoke("list")
        self.assertTrue(payload["ok"])
        rows = payload["data"]["libraries"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "外科学（第10版）")
        self.assertEqual(rows[0]["page_offset"], 39)
        self.assertEqual(rows[0]["index_state"], "none")
        self.assertTrue(payload["data"]["current_fingerprint"])

    def test_add_validates_required_and_file(self) -> None:
        missing = self._invoke("add", name="", subject="外科学", root_path=str(self.pdf))
        self.assertEqual(missing["error"]["code"], "bad_payload")
        bad_file = self._invoke("add", name="甲", subject="乙", root_path=str(self.tmp.name))
        self.assertEqual(bad_file["error"]["code"], "bad_payload")
        self.assertIn("PDF", bad_file["error"]["message"])

    def test_index_state_stale_and_compatible(self) -> None:
        library_id = self._add_library()
        with DatabaseManager(self.db_path) as database:
            file_id = database.upsert_file_metadata(
                library_id, str(self.pdf.resolve()), self.pdf.name, 10, 0, "hash")
            database.conn.execute("UPDATE textbook_files SET status='ready' WHERE id=?", (file_id,))
            database.set_file_index_profile(file_id, {"model": "old"}, "old-fingerprint")
        rows = self._invoke("list")["data"]["libraries"]
        self.assertEqual(rows[0]["index_state"], "stale")
        current = index_fingerprint(build_index_profile(runtime_config()))
        with DatabaseManager(self.db_path) as database:
            database.set_file_index_profile(file_id, build_index_profile(runtime_config()), current)
        rows = self._invoke("list")["data"]["libraries"]
        self.assertEqual(rows[0]["index_state"], "compatible")

    def test_update_and_remove(self) -> None:
        library_id = self._add_library()
        updated = self._invoke("update", library_id=library_id, name="外科学（第11版）",
                               subject="外科学", root_path=str(self.pdf), version="第11版",
                               page_offset=40)
        self.assertEqual(updated["data"], {"updated": True})
        row = self._invoke("list")["data"]["libraries"][0]
        self.assertEqual(row["name"], "外科学（第11版）")
        self.assertEqual(row["page_offset"], 40)
        removed = self._invoke("remove", library_id=library_id)
        self.assertEqual(removed["data"], {"removed": True})
        self.assertEqual(self._invoke("list")["data"]["libraries"], [])
        missing = self._invoke("remove", library_id=99999)
        self.assertEqual(missing["error"]["code"], "not_found")

    def test_infer_page_offset_without_index_returns_none(self) -> None:
        library_id = self._add_library()
        payload = self._invoke("infer_page_offset", library_id=library_id)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["data"]["offset"])

    def test_cloud_notice_roundtrip(self) -> None:
        self.assertEqual(self._invoke("cloud_notice")["data"], {"accepted": False})
        self.assertEqual(self._invoke("accept_cloud_notice")["data"], {"accepted": True})
        self.assertEqual(self._invoke("cloud_notice")["data"], {"accepted": True})


from ui_bridge.tags import TagsBridge

TAG_SAMPLE = {
    "id": "t1", "subject": "外科学", "type": "A1",
    "question": "题干", "options": ["A. 甲", "B. 乙"], "answer": "A",
}


class TagsBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        os.environ["MEDEXPLAIN_DATABASE_PATH"] = self.db_path
        self.bridge = TagsBridge(self.db_path)
        with DatabaseManager(self.db_path) as database:
            # 三道题共用"休克"使其晋升正式；"感染"只出现一次保持候选
            self.set_id = database.create_question_set("外科", "a.json", "json", [
                {**TAG_SAMPLE, "id": "t1", "tags": ["休克", "感染"], "question": "题干一"},
                {**TAG_SAMPLE, "id": "t2", "tags": ["休克"], "question": "题干二"},
                {**TAG_SAMPLE, "id": "t3", "tags": ["休克"], "question": "题干三"},
            ])

    def tearDown(self) -> None:
        os.environ.pop("MEDEXPLAIN_DATABASE_PATH", None)
        self.tmp.cleanup()

    def _invoke(self, method: str, **params) -> dict:
        return json.loads(self.bridge.invoke(method, json.dumps(params)))

    def _tags(self, **params) -> list[dict]:
        payload = self._invoke("list", set_id=self.set_id, **params)
        self.assertTrue(payload["ok"], payload)
        return payload["data"]["tags"]

    def test_list_requires_set(self) -> None:
        payload = self._invoke("list")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "bad_payload")

    def test_list_with_status_filter_and_search(self) -> None:
        tags = {row["label"]: row for row in self._tags()}
        self.assertEqual(tags["休克"]["status"], "active")
        self.assertEqual(tags["休克"]["usage_count"], 3)
        self.assertEqual(tags["感染"]["status"], "candidate")
        active = self._tags(status="active")
        self.assertEqual([row["label"] for row in active], ["休克"])
        searched = self._tags(search="感")
        self.assertEqual([row["label"] for row in searched], ["感染"])

    def test_subjects(self) -> None:
        payload = self._invoke("subjects", set_id=self.set_id)
        self.assertEqual(payload["data"], {"subjects": ["外科学"]})

    def test_rename_rewrites_question_tags(self) -> None:
        tag = next(row for row in self._tags() if row["label"] == "感染")
        payload = self._invoke("rename", tag_id=tag["id"], label="细菌感染")
        self.assertEqual(payload["data"], {"updated": True})
        labels = [row["label"] for row in self._tags()]
        self.assertIn("细菌感染", labels)
        with DatabaseManager(self.db_path) as database:
            row = database.list_questions_global(search="题干一")[0]
        self.assertNotIn("感染", row["tags"])
        missing = self._invoke("rename", tag_id=99999, label="甲乙")
        self.assertEqual(missing["error"]["code"], "not_found")

    def test_activate_below_threshold_is_bad_state(self) -> None:
        tag = next(row for row in self._tags() if row["label"] == "细菌感染" or row["label"] == "感染")
        payload = self._invoke("activate", tag_id=tag["id"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "bad_state")
        self.assertIn("3 道题", payload["error"]["message"])

    def test_merge_replaces_and_marks_merged(self) -> None:
        tags = {row["label"]: row for row in self._tags()}
        payload = self._invoke("merge", source_id=tags["感染"]["id"], target_id=tags["休克"]["id"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["changed"], 1)
        after = {row["label"]: row for row in self._tags(status="merged")}
        self.assertEqual(after["感染"]["merged_into"], "休克")
        wrong = self._invoke("merge", source_id=tags["休克"]["id"], target_id=tags["休克"]["id"])
        self.assertEqual(wrong["error"]["code"], "bad_payload")

    def test_search_matches_alias_of_merged_tag(self) -> None:
        tags = {row["label"]: row for row in self._tags()}
        merged = self._invoke("merge", source_id=tags["感染"]["id"], target_id=tags["休克"]["id"])
        self.assertTrue(merged["ok"], merged)
        # merge_question_tags 不把源标签名写入目标 aliases，夹具直接补写别名
        with DatabaseManager(self.db_path) as database:
            database.conn.execute(
                "UPDATE question_tag_catalog SET aliases_json=? WHERE id=?",
                (json.dumps(["感染"], ensure_ascii=False), tags["休克"]["id"]),
            )
            database.conn.commit()
        # status="active" 排除已合并的源标签行（其自身 label 也会命中搜索词）
        searched = self._tags(search="感染", status="active")
        self.assertEqual([row["label"] for row in searched], ["休克"])

    def test_delete_requires_exact_label(self) -> None:
        tag = next(row for row in self._tags() if row["label"] == "感染")
        wrong = self._invoke("delete", tag_id=tag["id"], confirm_label="不对")
        self.assertEqual(wrong["error"]["code"], "bad_payload")
        payload = self._invoke("delete", tag_id=tag["id"], confirm_label="感染")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["deleted"], "感染")
        self.assertNotIn("感染", [row["label"] for row in self._tags()])

    def test_merge_target_must_be_active(self) -> None:
        tags = {row["label"]: row for row in self._tags()}
        # candidate 目标被拒：合并到"感染"会绕过 ≥3 题门槛
        bad = self._invoke("merge", source_id=tags["休克"]["id"], target_id=tags["感染"]["id"])
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["code"], "bad_state")
        # 目标不存在
        missing = self._invoke("merge", source_id=tags["休克"]["id"], target_id=99999)
        self.assertEqual(missing["error"]["code"], "not_found")

    def test_delete_non_numeric_id_is_bad_payload(self) -> None:
        payload = self._invoke("delete", tag_id="abc", confirm_label="感染")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "bad_payload")
        self.assertIn("无效的标签 ID", payload["error"]["message"])

    def test_merge_rejects_non_numeric_target_id(self) -> None:
        payload = self._invoke("merge", source_id=1, target_id="abc")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "bad_payload")

    def test_delete_rejects_non_numeric_id(self) -> None:
        payload = self._invoke("delete", tag_id="abc", confirm_label="休克")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "bad_payload")


from ui_bridge.exports import ExportsBridge

EXPORT_SAMPLE = {
    "id": "e1", "subject": "外科学", "type": "A1",
    "question": "题干", "options": ["A. 甲", "B. 乙"], "answer": "A",
    "explanation": "### 考点解析\n\n内容。",
}


class ExportsBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        os.environ["MEDEXPLAIN_DATABASE_PATH"] = self.db_path
        self.bridge = ExportsBridge(self.db_path)
        self.out_dir = str(Path(self.tmp.name) / "out")
        with DatabaseManager(self.db_path) as database:
            self.set_id = database.create_question_set("外科", "a.json", "json", [
                dict(EXPORT_SAMPLE),
                {**EXPORT_SAMPLE, "id": "e2", "question": "第二题"},
            ])
            pk1, pk2 = [row["id"] for row in database.list_questions_global()]
            database.conn.execute(
                "UPDATE imported_questions SET pipeline_status='generated', generation_mode='textbook',"
                " explanation=? WHERE id=?", ("### 考点解析\n\n内容。", pk1))
            database.conn.execute(
                "UPDATE imported_questions SET pipeline_status='unmatched' WHERE id=?", (pk2,))
            database.conn.commit()
            database.mark_question_viewed(pk1)
            database.review_question(pk1, "approved", "### 考点解析\n\n内容。")

    def tearDown(self) -> None:
        os.environ.pop("MEDEXPLAIN_DATABASE_PATH", None)
        self.tmp.cleanup()

    def _invoke(self, method: str, **params) -> dict:
        return json.loads(self.bridge.invoke(method, json.dumps(params)))

    def test_default_dir(self) -> None:
        payload = self._invoke("default_dir")
        self.assertTrue(payload["ok"])
        self.assertIn("医学题库解析", payload["data"]["path"])

    def test_export_writes_v2_files_and_history(self) -> None:
        payload = self._invoke("export", set_id=self.set_id, output_dir=self.out_dir)
        self.assertTrue(payload["ok"], payload)
        data = payload["data"]
        self.assertNotIn("files", data)
        for key in ("json", "xlsx", "mapping"):
            self.assertTrue(Path(data[key]).exists(), key)
        from question_format_v2 import is_v2
        envelope = json.loads(Path(data["json"]).read_text(encoding="utf-8"))
        self.assertTrue(is_v2(envelope))
        self.assertEqual(envelope["meta"]["questionCount"], 1)
        history = self._invoke("history")["data"]["records"]
        self.assertEqual({row["export_type"] for row in history}, {"json_v2", "xlsx_v2", "mapping"})

    def test_export_split_by_subject(self) -> None:
        payload = self._invoke("export", set_id=self.set_id, output_dir=self.out_dir,
                               split_by_subject=True)
        data = payload["data"]
        self.assertIn("files", data)
        self.assertTrue(all(Path(path).exists() for path in data["files"]))
        self.assertTrue(any("外科学" in path for path in data["files"]))

    def test_export_requires_approved_questions(self) -> None:
        with DatabaseManager(self.db_path) as database:
            empty_set = database.create_question_set("空集", "b.json", "json", [
                {**EXPORT_SAMPLE, "id": "e9"}])
        payload = self._invoke("export", set_id=empty_set, output_dir=self.out_dir)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "bad_state")
        missing = self._invoke("export", set_id="no-such-set", output_dir=self.out_dir)
        self.assertEqual(missing["error"]["code"], "not_found")
        no_set = self._invoke("export", set_id="", output_dir=self.out_dir)
        self.assertEqual(no_set["error"]["code"], "bad_payload")

    def test_export_creates_missing_output_dir(self) -> None:
        nested = str(Path(self.tmp.name) / "out" / "新建" / "嵌套")
        self.assertFalse(Path(nested).exists())
        payload = self._invoke("export", set_id=self.set_id, output_dir=nested)
        self.assertTrue(payload["ok"], payload)
        for key in ("json", "xlsx", "mapping"):
            self.assertTrue(Path(payload["data"][key]).exists(), key)

    def test_export_issues_csv(self) -> None:
        payload = self._invoke("export_issues", set_id=self.set_id, output_dir=self.out_dir)
        self.assertTrue(payload["ok"])
        content = Path(payload["data"]["path"]).read_text(encoding="utf-8-sig")
        self.assertIn("题目ID", content)
        self.assertIn("e2", content)
        self.assertNotIn("e1", content)

    def test_history_filters_by_set(self) -> None:
        self._invoke("export", set_id=self.set_id, output_dir=self.out_dir)
        records = self._invoke("history", set_id=self.set_id)["data"]["records"]
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["set_id"], self.set_id)
        empty = self._invoke("history", set_id="no-such-set")["data"]["records"]
        self.assertEqual(empty, [])

    def test_open_folder(self) -> None:
        from unittest import mock
        target = str(Path(self.tmp.name) / "新目录")
        with mock.patch("ui_bridge.exports.os.startfile") as startfile:
            payload = self._invoke("open_folder", path=target)
        self.assertTrue(payload["ok"])
        self.assertTrue(Path(target).is_dir())
        startfile.assert_called_once()

    def test_history_rejects_non_numeric_limit(self) -> None:
        payload = self._invoke("history", limit="abc")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "bad_payload")


if __name__ == "__main__":
    unittest.main()
