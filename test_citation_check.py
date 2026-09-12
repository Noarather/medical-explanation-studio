import fitz
from evals.citation_check import valid_page, verify_pdf_citations


def test_physical_page_validation():
    assert valid_page(1)
    assert not any(valid_page(value) for value in [None, True, 0, -1, 1.2, "1"])


def test_real_pdf_bounds_missing_file_and_unverified_path(tmp_path):
    path = tmp_path / "sample.pdf"
    with fitz.open() as doc:
        doc.new_page(); doc.save(path)
    candidates = [{"chunk_id": "ok", "source_path": str(path), "pdf_page": 1},
        {"chunk_id": "bounds", "source_path": str(path), "pdf_page": 2},
        {"chunk_id": "missing", "source_path": str(tmp_path / "missing.pdf"), "pdf_page": 1},
        {"chunk_id": "unknown", "pdf_page": 1}]
    report = verify_pdf_citations([{"id": "q", "candidates": candidates}])
    assert (report["checked"], report["errors"], report["skippedMissingPath"]) == (3, 2, 1)
    assert verify_pdf_citations([{"candidates": [{}]}])["status"] == "not_verified"
