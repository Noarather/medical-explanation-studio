"""Keep successful local extraction; send only unresolved source blocks to the model."""
import re
from importers import normalize_formatted_rows, normalize_question, parse_common_question_text


def organize(raw_text, default_subject, client=None, progress=None):
    text = str(raw_text).replace("\r\n", "\n").strip()
    if not text:
        raise ValueError("没有可整理的原始文本")
    starts = list(re.finditer(r"(?m)^\s*(?:第\s*)?\d{1,6}\s*(?:题|[.．、\)])\s*", text))
    prefix = text[:starts[0].start()].strip() if starts else ""
    pieces = [text[m.start():starts[i + 1].start() if i + 1 < len(starts) else len(text)]
              for i, m in enumerate(starts)] if starts else [text]
    collected, counts = [], {"local_count": 0, "ai_count": 0, "failed_count": 0}
    for index, piece in enumerate(pieces):
        if progress:
            progress(index, len(pieces))
        local, warnings = parse_common_question_text(piece, default_subject)
        rows = normalize_formatted_rows(local, default_subject)
        structural_errors = [error for row in rows for error in normalize_question(row)[1]
                             if "subject" not in error]
        issues = []
        if not warnings and not structural_errors and not prefix:
            counts["local_count"] += len(rows)
        elif client is not None and len(prefix) + len(piece) <= 16000:
            try:
                candidates = normalize_formatted_rows(client.organize_questions(
                    (prefix + "\n" + piece).strip(), default_subject), default_subject)
                if not candidates:
                    raise ValueError("模型未返回题目")
                rows = candidates
                counts["ai_count"] += len(rows)
                # AI formatting must not silently solve a source without an answer.
                if not re.search(r"(?:答案|正确选项|answer)\s*[:：=]\s*\S", piece, re.I):
                    issues.append("原文未识别到答案标记，请核对答案来源")
            except Exception as exc:
                issues.append(f"此题块 AI 整理失败：{exc}")
                counts["failed_count"] += 1
        else:
            counts["local_count"] += len(rows)
            if client is not None and len(prefix) + len(piece) > 16000:
                issues.append("题块超过 16000 字，请拆分后再整理")
        if prefix:
            issues.append("题目之前有共享文本，请核对病例或答案表归属")
        for row in rows:
            extensions = dict(row.get("extensions") or {})
            extensions["sourceBlock"] = {"number": index + 1, "text": piece, "context": prefix}
            if issues:
                extensions["importIssues"] = list(extensions.get("importIssues") or []) + issues
            row["extensions"] = extensions
        collected.extend(rows)
    rows = normalize_formatted_rows(collected, default_subject)
    if progress:
        progress(len(pieces), len(pieces))
    method = "hybrid" if counts["ai_count"] and counts["local_count"] else getattr(client, "provider", "deepseek") if counts["ai_count"] else "local"
    return {"rows": rows, "method": method, "stats": counts,
            "warnings": [normalize_question(row, i + 1)[1] for i, row in enumerate(rows)]}
