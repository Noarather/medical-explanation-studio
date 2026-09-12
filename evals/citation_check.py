"""Explicit offline audit of candidate PDF references; never changes review status."""
from pathlib import Path


def valid_page(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def verify_pdf_citations(rows):
    import fitz
    counts, issues = {}, []
    checked = skipped = 0
    for row in rows:
        for candidate in row.get("candidates", []):
            path = candidate.get("source_path")
            if not path:
                skipped += 1
                continue
            checked += 1
            reason = ""
            page = candidate.get("pdf_page")
            source = Path(path)
            if not valid_page(page):
                reason = "invalid_pdf_page"
            elif not source.is_file() or source.suffix.lower() != ".pdf":
                reason = "pdf_not_found"
            else:
                try:
                    key = str(source.resolve())
                    if key not in counts:
                        with fitz.open(source) as doc:
                            counts[key] = doc.page_count
                    if page > counts[key]:
                        reason = "pdf_page_out_of_range"
                except Exception:
                    reason = "pdf_unreadable"
            if reason:
                issues.append({"id": row.get("id"), "chunk_id": candidate.get("chunk_id"), "reason": reason})
    return {"checked": checked, "skippedMissingPath": skipped, "errors": len(issues), "issues": issues,
            "status": "completed" if checked else "not_verified"}
