"""Fast, offline checks and readable messages for parser dependency failures."""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

PARSER_MODULES = ("fitz", "openai")


def parser_health(model_dir=None) -> dict:
    missing = [name for name in PARSER_MODULES if importlib.util.find_spec(name) is None]
    return {"ok": not missing, "missing": missing, "models_available": False,
            "mode": "cloud-text", "message": "缺少依赖：" + "、".join(missing) if missing else
            "文本提取环境可用；扫描页通过 DashScope 云端 OCR，不需要本地模型。此检查不验证密钥或云端权限。"}


def index_error_summary(error: str) -> str:
    value = error or ""
    if "MEMORY_LIMIT_EXCEEDED" in value:
        rss = re.search(r"(?:^|;\s*)rss=([0-9.]+)MB", value)
        hard = re.search(r"(?:^|;\s*)hard=([0-9.]+)MB", value)
        usage = ""
        if rss and hard:
            usage = f"（实际内存 {rss.group(1)} MB，限制 {hard.group(1)} MB）"
        return (
            "解析已自动缩小到最小单页范围，但内存仍超过限制" + usage + "。"
            "本次重建不会覆盖原有可用索引；请关闭占用内存较高的程序后重试，"
            "或将 PDF 拆分后分别导入。原始页范围和异常保留在下方技术详情中。"
        )
    missing = list(dict.fromkeys(re.findall(r"No module named ['\"]([^'\"]+)['\"]", value)))
    if missing:
        return "上次索引时缺少解析依赖（" + "、".join(missing) + "），部分页面可能未正确提取。请先检查解析环境，再点击“重试异常索引”。"
    if "docling-parse could not load document" in (error or ""):
        return "Docling 主解析器未能加载 PDF，已尝试备用解析；这不等于没有索引。请确认 PDF 可打开，并使用修复版重试异常索引。原始错误保留在下方技术详情中。"
    return "；".join(dict.fromkeys(part.strip() for part in re.split(r";\s*", error or "") if part.strip()))[:600]


def parser_smoke_check(report_path: str) -> None:
    """Packaging probe: parse synthetic content with a mock cloud OCR client, with no APIs."""
    import json
    import tempfile
    import fitz
    from pdf_parser import PDFParser
    report = {"ok": False, "utf8_mode": bool(sys.flags.utf8_mode)}
    try:
        with tempfile.TemporaryDirectory(prefix="medexplain-parser-check-") as folder:
            unicode_folder = Path(folder) / "中文教材路径"
            unicode_folder.mkdir()
            pdf = unicode_folder / "合成教材（第10版）.pdf"
            with fitz.open() as doc:
                doc.new_page().insert_text((72, 72), "Clinical medicine textbook\nPhysical examination and diagnosis\nPatient assessment and treatment", fontsize=18)
                doc.save(pdf)
            parser = PDFParser()
            pages = parser.extract_pages(pdf)
            class StubOCR:
                def recognize_image(self, path):
                    assert Path(path).is_file()
                    return "Synthetic scanned textbook page with enough text for verification."
            scanned = parser.extract_pages(pdf, StubOCR(), force_ocr=True)
            report.update(pages=len(pages), method=pages[0]["extraction_method"],
                          error=pages[0]["error_message"], characters=len(pages[0]["text"]),
                          ocr_characters=len(scanned[0]["text"]), ocr_mode="mock-cloud", live_api=False,
                          unicode_path=True, local_models=False)
            if report["method"] != "text" or report["error"] or report["characters"] < 30 or scanned[0]["extraction_method"] != "qwen_ocr":
                raise RuntimeError("文本提取或模拟云端 OCR 管线检查失败")
            report["ok"] = True
    except Exception as exc:
        report["failure"] = str(exc)
        raise
    finally:
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
