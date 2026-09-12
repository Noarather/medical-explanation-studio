"""Fast, offline checks and readable messages for parser dependency failures."""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

PARSER_MODULES = ("docling", "rapidocr", "onnxruntime")


def parser_health(model_dir=None) -> dict:
    missing = []
    for name in PARSER_MODULES:
        try:
            available = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError, AttributeError):
            available = False
        if not available:
            missing.append(name)
    root = Path(model_dir or os.getenv("MEDEXPLAIN_DOCLING_MODELS") or Path(__file__).resolve().parent / "models" / "docling")
    models = root.is_dir() and all(next(root.rglob(pattern), None) for pattern in ("*det*.onnx", "*rec*.onnx", "*cls*.onnx"))
    packaged = bool(getattr(sys, "frozen", False))
    hint = ("请使用最新版安装包覆盖安装，并确认 EXE 与全部 BIN 在同一目录。" if packaged
            else "请通过 run_desktop.ps1 启动，它会补装 requirements.txt 中的解析依赖。")
    issues = (["缺少解析依赖：" + "、".join(missing)] if missing else [])
    if not models:
        issues.append("未找到完整的本地 OCR 模型，请检查 models/docling 目录")
    return {"ok": not issues, "missing": missing, "models_available": bool(models),
            "message": "；".join(issues) + ("。" + hint if issues else "") if issues else "解析依赖及本地 OCR 模型文件可用。此检查不替代实际 PDF 解析。"}


def index_error_summary(error: str) -> str:
    missing = list(dict.fromkeys(re.findall(r"No module named ['\"]([^'\"]+)['\"]", error or "")))
    if missing:
        return "上次索引时缺少解析依赖（" + "、".join(missing) + "），部分页面可能未正确提取。请先检查解析环境，再点击“重试异常索引”。"
    return "；".join(dict.fromkeys(part.strip() for part in re.split(r";\s*", error or "") if part.strip()))[:600]


def parser_smoke_check(report_path: str) -> None:
    """Packaging probe: parse synthetic content using bundled models, with no APIs."""
    import json
    import tempfile
    import fitz
    from pdf_parser import PDFParser
    report = {"ok": False, "utf8_mode": bool(sys.flags.utf8_mode)}
    try:
        with tempfile.TemporaryDirectory(prefix="medexplain-parser-check-") as folder:
            pdf = Path(folder) / "synthetic.pdf"
            with fitz.open() as doc:
                doc.new_page().insert_text((72, 72), "Clinical medicine textbook\nPhysical examination and diagnosis\nPatient assessment and treatment", fontsize=18)
                doc.save(pdf)
            parser = PDFParser(model_dir=Path(__file__).resolve().parent / "models" / "docling")
            pages = parser.extract_pages(pdf)
            with fitz.open(pdf) as doc:
                text = parser._rapid_ocr(doc[0])
            report.update(pages=len(pages), method=pages[0]["extraction_method"],
                          error=pages[0]["error_message"], characters=len(pages[0]["text"]), ocr_characters=len(text))
            if report["method"] != "docling" or report["error"] or report["characters"] < 30 or len(text) < 30:
                raise RuntimeError("本地解析或 OCR 实际检查失败")
            report["ok"] = True
    except Exception as exc:
        report["failure"] = str(exc)
        raise
    finally:
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
