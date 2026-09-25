"""Synthetic-only offline tests. Real textbooks never become committed fixtures."""
import json
import time
from pathlib import Path

import fitz
import pytest

from batch_tasks import BatchCancelled
from db_manager import DatabaseManager
from textbook_probe import TextbookInspector, build_calibration, infer_metadata, margin_numbers
from ui_bridge.library import LibraryBridge


def make_pdf(path, numbers=(8, 9, 1, 2, 3, 4, 1, 2)):
    with fitz.open() as doc:
        for i, number in enumerate(numbers):
            page = doc.new_page()
            page.insert_text((60, 100), "Synthetic textbook content")
            page.insert_text((60, 820), str(number))
            if i == 0:
                page.insert_text((60, 150), "病理生理学 第10版", fontname="china-s")
        doc.save(path)
    return str(path)


def wait(bridge, token):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = bridge.api_inspect_status(token)
        if result["status"] != "running":
            assert result["status"] == "completed", result
            return result["result"]
        time.sleep(.01)
    raise AssertionError("Timed out")


def form(row, **changes):
    return {k: row[k] for k in ("index", "name", "subject", "version")} | {"mode": "auto"} | changes


@pytest.mark.parametrize("name,subject,version", [
    ("病理生理学（第10版）.pdf", "病理生理学", "第10版"),
    ("病理学（第十版）.PDF", "病理学", "第10版"),
    ("生物化学与分子生物学 第九版.pdf", "生物化学与分子生物学", "第9版"),
    ("外科学学习指导第十一版.pdf", "外科学", "第11版"),
])
def test_metadata_longest_subject_and_editions(name, subject, version):
    result = infer_metadata(name)
    assert result["subject"] == subject
    assert result["version"] == version
    if "学习指导" in name:
        assert "学习指导" in result["name"]


def test_metadata_uses_frontmatter_and_flags_conflict():
    result = infer_metadata("scan_001.pdf", front_text="病理生理学\n第\n版\n10\n主编")
    assert result["name"] == "病理生理学（第10版）"
    conflict = infer_metadata("病理学第9版.pdf", front_text="病理生理学第10版")
    assert len(conflict["warnings"]) == 2
    assert infer_metadata("unknown.pdf")["subject"] == ""


def test_real_pdf_text_preflight_does_not_need_an_index(tmp_path):
    result = TextbookInspector().inspect(make_pdf(tmp_path / "扫描件.pdf"))
    assert result["subject"] == "病理生理学"
    assert result["version"] == "第10版"
    assert result["calibration"]["offset"] == 2
    assert len(result["calibration"]["segments"]) == 3
    assert result["calibration"]["mapping"]["7"] == 1


def test_body_digits_and_chapter_numbers_are_not_page_numbers():
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((70, 400), "123")
        page.insert_text((70, 40), "Chapter 1")
        page.insert_text((70, 820), "8")
        assert margin_numbers(page) == [8]


def test_calibration_abstains_on_isolated_digits_conflicts_and_long_gaps():
    def obs(p, n): return {"pdf_page": p, "printed_page": n, "method": "test"}
    assert build_calibration([obs(2, 1), obs(8, 7)], 10)["status"] == "unresolved"
    assert build_calibration([obs(2, 1), obs(3, 1), obs(4, 1)], 10)["mapping"] == {}
    result = build_calibration([obs(2, 1), obs(3, 2), obs(4, 3), obs(4, 4), obs(5, 4), obs(6, 5)], 10)
    assert "4" not in result["mapping"]
    assert "1" not in result["mapping"] and "10" not in result["mapping"]


def test_cancel_and_encrypted_or_broken_file(tmp_path):
    path = make_pdf(tmp_path / "内科学.pdf")
    def cancel(*args): raise BatchCancelled("stop")
    with pytest.raises(BatchCancelled):
        TextbookInspector().inspect(path, cancel)
    broken = tmp_path / "坏书.pdf"
    broken.write_bytes(b"not a PDF")
    with pytest.raises(Exception):
        TextbookInspector().inspect(broken)
    locked = tmp_path / "locked.pdf"
    with fitz.open(path) as doc:
        doc.save(locked, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="password")
    with pytest.raises(ValueError, match="加密"):
        TextbookInspector().inspect(locked)


def test_batch_partial_success_duplicate_and_retry(tmp_path):
    bridge = LibraryBridge(str(tmp_path / "local.db"))
    good = make_pdf(tmp_path / "病理生理学第10版.pdf")
    bad = tmp_path / "坏书.pdf"
    bad.write_bytes(b"broken")
    token = bridge.api_inspect_start([good, str(bad), good])["token"]
    rows = wait(bridge, token)["items"]
    assert not rows[0]["error"] and rows[1]["error"] and rows[2]["error"]
    with DatabaseManager(bridge._database_path) as db:
        assert db.list_libraries() == []  # preview is not import
    response = wait(bridge, bridge.api_commit_batch(token, [form(rows[0]), form(rows[1])])["token"])
    assert response["imported"] == 1
    assert response["results"][1]["status"] == "error"
    retry = wait(bridge, bridge.api_commit_batch(token, [form(rows[0])])["token"])
    assert retry["results"][0]["status"] == "duplicate"
    with DatabaseManager(bridge._database_path) as db:
        assert len(db.list_libraries()) == 1
        assert len(list(db.conn.execute("SELECT * FROM library_page_map"))) == 8
        assert not list(db.conn.execute("SELECT * FROM jobs"))  # never consumes APIs


def test_changed_file_requires_new_inspection(tmp_path):
    bridge = LibraryBridge(str(tmp_path / "local.db"))
    path = make_pdf(tmp_path / "外科学.pdf")
    token = bridge.api_inspect_start([path])["token"]
    row = wait(bridge, token)["items"][0]
    with Path(path).open("ab") as handle: handle.write(b"\nchanged")
    result = wait(bridge, bridge.api_commit_batch(token, [form(row)])["token"])
    assert result["imported"] == 0
    assert "变化" in result["results"][0]["message"]


def test_mapping_retrieval_persistence_metadata_edits_and_manual_override(tmp_path):
    path = make_pdf(tmp_path / "病理生理学.pdf", (1, 2, 3, 1, 2, 3, 99))
    result = TextbookInspector().inspect(path)
    db_path = str(tmp_path / "local.db")
    with DatabaseManager(db_path) as db:
        lid = db.add_library("教材", "病理生理学", path)
        fid = db.upsert_file_metadata(lid, path, "book.pdf", 1, 1, "hash")
        pages = [{"page_number": p, "text": "test", "extraction_method": "text"} for p in (4, 7)]
        chunks = [{"page_number": p, "chunk_index": 0, "chunk_text": "test", "search_text": "test", "embedding": [1.0]} for p in (4, 7)]
        db.replace_file_content(fid, lid, pages, chunks)
        db.apply_library_calibration(lid, result["calibration"])
        assert {r["pdf_page"]: r["textbook_page"] for r in db.search_chunks("病理生理学", "test")} == {4: 1, 7: 0}
        db.update_library(lid, "新名称", "病理生理学", "第10版", path, 0)
        assert db.get_library(lid)["calibration_json"] != "{}"
        assert db.infer_library_page_offset(lid, force=True) == 0
    with DatabaseManager(db_path) as db:
        assert db.get_library(lid)["calibration_json"] != "{}"
        db.set_library_page_offset(lid, 3)
        assert db.get_library(lid)["calibration_json"] == "{}"
        assert not list(db.conn.execute("SELECT * FROM library_page_map"))


def test_history_keeps_pdf_locator_and_prose_and_requests_review(tmp_path):
    path = make_pdf(tmp_path / "内科学.pdf")
    with DatabaseManager(str(tmp_path / "db.sqlite")) as db:
        lid = db.add_library("内科学", "内科学", path)
        db.upsert_file_metadata(lid, path, "book.pdf", 1, 1, "hash")
        sid = db.create_question_set("synthetic", "synthetic", "json", [{"id":"1", "subject":"内科学", "question":"测试", "options":["A. 甲", "B. 乙"], "answer":"A"}])
        question = db.list_imported_questions(sid)[0]
        db.save_generated_result(question["id"], "generated", "textbook", .9, "课本第3页",
            [{"source_path":path, "pdf_page":3, "source_page":3}])
        db.conn.execute("UPDATE imported_questions SET review_status='approved'")
        db.conn.commit()
        assert db.apply_library_calibration(lid, TextbookInspector().inspect(path)["calibration"]) == 1
        after = db.get_imported_question(question["id"])
        assert after["evidence"][0]["pdf_page"] == 3
        assert after["evidence"][0]["source_page"] == 1
        assert after["explanation"] == "课本第3页"
        assert after["review_status"] == "pending"
        assert after["raw"]["explanationMeta"]["reviewWarnings"]


def test_manual_anchors_reject_fractional_and_import_valid(tmp_path):
    bridge = LibraryBridge(str(tmp_path / "local.db"))
    token = bridge.api_inspect_start([make_pdf(tmp_path / "内科学.pdf")])["token"]
    row = wait(bridge, token)["items"][0]
    invalid = wait(bridge, bridge.api_commit_batch(token, [form(row, mode="manual", pdf_anchor=1.5, textbook_anchor=1)])["token"])
    assert invalid["imported"] == 0
    valid = wait(bridge, bridge.api_commit_batch(token, [form(row, mode="manual", pdf_anchor=3, textbook_anchor=1)])["token"])
    assert valid["imported"] == 1
    with DatabaseManager(bridge._database_path) as db:
        assert db.list_libraries()[0]["page_offset"] == 2


def test_local_ocr_scanned_page_fallback_and_empty_output(tmp_path, monkeypatch):
    from types import SimpleNamespace
    image_doc = fitz.open()
    image_page = image_doc.new_page()
    image_page.insert_text((50, 800), "1")
    png = image_page.get_pixmap().tobytes("png")
    image_doc.close()
    path = tmp_path / "病理生理学第10版.pdf"
    with fitz.open() as doc:
        for _ in range(3):
            page = doc.new_page()
            page.insert_image(page.rect, stream=png)
        doc.save(path)
    inspector = TextbookInspector()
    calls = []
    def ocr(page, margins=False):
        calls.append(margins)
        return str(page.number + 1)
    monkeypatch.setattr(inspector, "_ocr_text", ocr)
    result = inspector.inspect(path)
    assert result["calibration"]["mapping"] == {"1":1,"2":2,"3":3}
    assert all(calls) and all(a["method"] == "local_ocr" for a in result["calibration"]["anchors"])
    empty = TextbookInspector()
    monkeypatch.setattr(empty, "_engine", lambda: lambda _: SimpleNamespace(txts=None, scores=None))
    with fitz.open(path) as doc:
        assert empty._ocr_text(doc[0], margins=True) == ""


def test_existing_calibration_preserves_metadata_and_rejects_changed_index(tmp_path):
    path = make_pdf(tmp_path / "病理生理学第10版.pdf")
    bridge = LibraryBridge(str(tmp_path / "local.db"))
    with DatabaseManager(bridge._database_path) as db:
        lid = db.add_library("人工书名", "病理生理学", path, "保留版次", 24)
    token = bridge.api_inspect_start([path])["token"]
    row = wait(bridge, token)["items"][0]
    result = wait(bridge, bridge.api_commit_batch(token, [form(row)], library_id=lid)["token"])
    assert result["results"][0]["status"] == "calibrated"
    with DatabaseManager(bridge._database_path) as db:
        assert db.get_library(lid)["name"] == "人工书名"
        assert db.get_library(lid)["version"] == "保留版次"
        before = db.get_library(lid)["calibration_json"]
        db.upsert_file_metadata(lid, path, "book.pdf", 1, 1, "different-file")
    task = bridge.api_commit_batch(token, [form(row)], library_id=lid)["token"]
    deadline = time.monotonic() + 10
    while bridge.api_inspect_status(task)["status"] == "running" and time.monotonic() < deadline:
        time.sleep(.01)
    state = bridge.api_inspect_status(task)
    assert state["status"] == "failed" and "重建索引" in state["message"]
    with DatabaseManager(bridge._database_path) as db:
        assert db.get_library(lid)["calibration_json"] == before


def test_current_calibration_does_not_rewrite_previous_pdf_history(tmp_path):
    old_path = make_pdf(tmp_path / "旧文件.pdf")
    path = make_pdf(tmp_path / "当前文件.pdf")
    with DatabaseManager(str(tmp_path / "local.db")) as db:
        lid = db.add_library("当前教材", "病理生理学", path)
        db.upsert_file_metadata(lid, old_path, "old.pdf", 1, 1, "old-hash")
        db.upsert_file_metadata(lid, path, "current.pdf", 1, 1, "current-hash")
        sid = db.create_question_set("synthetic", "synthetic", "json", [{"id":"old", "subject":"病理生理学", "question":"测试", "options":["A. 甲", "B. 乙"], "answer":"A"}])
        question = db.list_imported_questions(sid)[0]
        db.save_generated_result(question["id"], "generated", "textbook", .9, "旧文件第3页",
            [{"source_path":old_path, "pdf_page":3, "source_page":3}])
        assert db.apply_library_calibration(lid, TextbookInspector().inspect(path)["calibration"]) == 0
        assert db.get_imported_question(question["id"])["evidence"][0]["source_page"] == 3
