"""Offline-first Docling PDF parsing with page-local structured chunks."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Optional

import fitz


DOCLING_VERSION = "2.117.0"
CHUNKER_VERSION = "structured-page-v1"


def normalize_text(value: str) -> str:
    value = str(value or "").replace("\u00a0", " ").replace("\u3000", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


class PDFParser:
    def __init__(
        self, chunk_size: int = 500, chunk_overlap: int = 50,
        model_dir: str | Path | None = None, page_batch_size: int = 8,
    ):
        if chunk_size <= 0 or chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError("chunk_size must be positive and overlap must be smaller")
        self.chunk_size = int(chunk_size)
        self.chunk_overlap = int(chunk_overlap)
        self.page_batch_size = max(1, int(page_batch_size))
        default_models = Path(__file__).resolve().parent / "models" / "docling"
        environment_model_dir = os.getenv("MEDEXPLAIN_DOCLING_MODELS")
        self._legacy_implicit_model_dir = model_dir is None and not environment_model_dir
        self.model_dir = Path(model_dir or environment_model_dir or default_models)
        self._converter = None
        self._rapidocr = None

    def _rapid_model_paths(self) -> dict[str, str]:
        root = self.model_dir
        paths = {}
        if not root.is_dir():
            return paths
        for key, pattern in (("det_model_path", "*det*.onnx"), ("cls_model_path", "*cls*.onnx"), ("rec_model_path", "*rec*.onnx")):
            match = next(iter(sorted(root.rglob(pattern))), None)
            if match:
                paths[key] = str(match)
        return paths

    def _docling_converter(self):
        if self._converter is not None:
            return self._converter
        # Desktop installs have no compiler toolchain. Keep inference in eager mode
        # on Windows; torch.compile also reads UTF-8 kernel templates as GBK there.
        from docling.datamodel.settings import settings as docling_settings
        if os.name == "nt":
            docling_settings.inference.compile_torch_models = False
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
        from docling.datamodel.vlm_engine_options import TransformersVlmEngineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        options = PdfPipelineOptions()
        if os.name == "nt":
            options.code_formula_options.engine_options = TransformersVlmEngineOptions(compile_model=False)
        options.do_ocr = True
        options.do_table_structure = True
        options.do_formula_enrichment = True
        options.do_picture_description = False
        options.do_chart_extraction = False
        options.generate_picture_images = False
        options.generate_page_images = False
        options.enable_remote_services = False
        options.allow_external_plugins = False
        rapid_options = {"backend": "onnxruntime", "lang": ["ch"], **self._rapid_model_paths()}
        options.ocr_options = RapidOcrOptions(**rapid_options)
        if self.model_dir.is_dir():
            options.artifacts_path = self.model_dir
        self._converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF],
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
        )
        return self._converter

    @staticmethod
    def _docling_page_markdown(document, page_number: int) -> str:
        exporter = getattr(document, "export_to_markdown")
        for kwargs in ({"page_no": page_number}, {"page_number": page_number}):
            try:
                value = exporter(**kwargs)
                if value:
                    return normalize_text(value)
            except TypeError:
                continue
        raise RuntimeError("Installed Docling cannot export a single physical page")

    @staticmethod
    def _bbox(item) -> list[float] | None:
        provenance = list(getattr(item, "prov", None) or [])
        bbox = getattr(provenance[0], "bbox", None) if provenance else None
        if bbox is None:
            return None
        values = [getattr(bbox, key, None) for key in ("l", "t", "r", "b")]
        return [float(value) for value in values] if all(value is not None for value in values) else None

    def _docling_blocks(self, document, page_number: int, local_page: int, batch_count: int) -> list[dict]:
        iterator = getattr(document, "iterate_items", None)
        if not callable(iterator):
            return []
        rows = list(iterator())
        provenance_pages = [
            int(getattr(prov, "page_no", -1))
            for entry in rows
            for prov in (getattr(entry[0] if isinstance(entry, tuple) else entry, "prov", None) or [])
        ]
        zero_based = 0 in provenance_pages
        uses_local_pages = bool(provenance_pages) and max(provenance_pages) <= batch_count
        target_page = local_page if uses_local_pages else page_number
        if zero_based:
            target_page -= 1
        blocks, headings = [], []
        for entry in rows:
            item = entry[0] if isinstance(entry, tuple) else entry
            provenance = list(getattr(item, "prov", None) or [])
            if provenance and not any(int(getattr(prov, "page_no", -1)) == target_page for prov in provenance):
                continue
            label = str(getattr(item, "label", "paragraph")).casefold()
            text = normalize_text(getattr(item, "text", ""))
            kind = "paragraph"
            if "table" in label:
                kind = "table"
                exporter = getattr(item, "export_to_markdown", None)
                if callable(exporter):
                    try: text = normalize_text(exporter(doc=document))
                    except TypeError: text = normalize_text(exporter(document))
            elif "formula" in label:
                kind = "formula"
            elif "section_header" in label or label.endswith("title"):
                if text:
                    headings = [text]
                continue
            if text:
                blocks.append({
                    "text": text, "type": kind, "title_path": list(headings),
                    "bbox": self._bbox(item),
                })
        return blocks

    def _docling_batch(self, path: Path, start_page: int, end_page: int) -> list[dict]:
        result = self._docling_converter().convert(path, page_range=(start_page, end_page))
        document = result.document
        pages = []
        for page_number in range(start_page, end_page + 1):
            blocks = self._docling_blocks(
                document, page_number, page_number - start_page + 1, end_page - start_page + 1,
            )
            text = normalize_text("\n\n".join(item["text"] for item in blocks))
            if not text:
                text = self._docling_page_markdown(document, page_number)
                blocks = self._markdown_blocks(text)
            pages.append({
                "page_number": page_number, "text": text,
                "extraction_method": "docling", "error_message": "",
                "blocks": blocks,
            })
        return pages

    @staticmethod
    def _render_page(page) -> str:
        temp_path = ""
        pixmap = page.get_pixmap(matrix=fitz.Matrix(200 / 72, 200 / 72), alpha=False)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            temp_path = handle.name
        pixmap.save(temp_path)
        return temp_path

    def _rapid_ocr(self, page) -> str:
        if self._rapidocr is None:
            from rapidocr import RapidOCR
            paths = self._rapid_model_paths()
            rapid_params = {
                "Det.model_path": paths.get("det_model_path"),
                "Cls.model_path": paths.get("cls_model_path"),
                "Rec.model_path": paths.get("rec_model_path"),
            }
            rapid_params = {key: value for key, value in rapid_params.items() if value}
            try:
                self._rapidocr = RapidOCR(params=rapid_params)
            except TypeError:
                self._rapidocr = RapidOCR(**paths)
        temp_path = self._render_page(page)
        try:
            output = self._rapidocr(temp_path)
            if hasattr(output, "txts"):
                return normalize_text("\n".join(str(text) for text in (output.txts or [])))
            result, _elapsed = output
            return normalize_text("\n".join(str(row[1]) for row in (result or []) if len(row) > 1))
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def _fallback_page(
        self, page, page_number: int, ocr_client=None, force_ocr: bool = False,
        qwen_method: str = "qwen_ocr_fallback",
    ) -> dict:
        errors = []
        text = "" if force_ocr else normalize_text(page.get_text("text"))
        method = "docling_fallback"
        if len(re.sub(r"\s+", "", text)) < 30:
            try:
                text = self._rapid_ocr(page)
                if not text.strip():
                    raise RuntimeError("本地 OCR 未识别到文本")
                method = "rapidocr_fallback"
            except Exception as exc:
                errors.append(f"rapidocr: {exc}")
                if ocr_client is not None:
                    temp_path = self._render_page(page)
                    try:
                        text = normalize_text(ocr_client.recognize_image(temp_path))
                        method = qwen_method
                    except Exception as qwen_exc:
                        errors.append(f"qwen_ocr: {qwen_exc}")
                    finally:
                        Path(temp_path).unlink(missing_ok=True)
        return {
            "page_number": page_number, "text": text,
            "extraction_method": method if text else "blank",
            "error_message": "; ".join(errors), "blocks": self._markdown_blocks(text),
        }

    def parse_file(self, path: str | Path) -> list[dict]:
        return [
            {"source_file": Path(path).name, "source_page": item["page_number"],
             "chunk_index": item["chunk_index"], "chunk_text": item["chunk_text"]}
            for item in self.chunks_from_pages(self.extract_pages(path))
        ]

    def extract_pages(
        self, path: str | Path, ocr_client=None, force_ocr: bool = False,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> list[dict]:
        pdf_path = Path(path)
        pages: list[dict] = []
        with fitz.open(pdf_path) as document:
            total = len(document)
            for start in range(1, total + 1, self.page_batch_size):
                end = min(total, start + self.page_batch_size - 1)
                try:
                    batch = self._docling_batch(pdf_path, start, end)
                    if force_ocr and not any(item.get("text") for item in batch):
                        raise RuntimeError("Docling OCR returned no text")
                except Exception as exc:
                    batch = []
                    for page_number in range(start, end + 1):
                        item = self._fallback_page(
                            document[page_number - 1], page_number, ocr_client, force_ocr,
                            "ocr" if self._legacy_implicit_model_dir else "qwen_ocr_fallback",
                        )
                        item["error_message"] = f"docling: {exc}; {item.get('error_message', '')}".strip("; ")
                        batch.append(item)
                pages.extend(batch)
                if progress:
                    progress(end, total)
        return pages

    @staticmethod
    def _markdown_blocks(text: str) -> list[dict]:
        blocks, headings, buffer = [], [], []

        def flush(kind: str = "paragraph"):
            value = normalize_text("\n".join(buffer))
            buffer.clear()
            if value:
                blocks.append({"text": value, "type": kind, "title_path": list(headings)})

        lines = str(text or "").splitlines()
        index = 0
        while index < len(lines):
            line = lines[index].strip()
            heading = re.match(r"^(#{1,6})\s+(.+)$", line)
            if heading:
                flush()
                level = len(heading.group(1))
                headings[:] = headings[:level - 1] + [heading.group(2).strip()]
                index += 1
                continue
            if "|" in line and index + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-+", lines[index + 1]):
                flush()
                table = [line, lines[index + 1]]
                index += 2
                while index < len(lines) and "|" in lines[index]:
                    table.append(lines[index].strip())
                    index += 1
                buffer.extend(table)
                flush("table")
                continue
            if re.search(r"\$[^$]+\$|\\\(|\\\[|\\frac|\\sum|\\alpha", line):
                flush()
                buffer.append(line)
                flush("formula")
            elif line:
                buffer.append(line)
            else:
                flush()
            index += 1
        flush()
        return blocks

    def chunks_from_pages(self, pages: list[dict]) -> list[dict]:
        chunks: list[dict] = []
        for page in pages:
            blocks = page.get("blocks") or self._markdown_blocks(page.get("text", ""))
            chunk_index = 0
            groups: list[dict] = []
            paragraph_text: list[str] = []
            paragraph_path: list[str] = []
            paragraph_bbox = None

            def flush_paragraphs() -> None:
                nonlocal paragraph_text, paragraph_path, paragraph_bbox
                if paragraph_text:
                    groups.append({
                        "text": "\n\n".join(paragraph_text), "type": "paragraph",
                        "title_path": paragraph_path, "bbox": paragraph_bbox,
                    })
                paragraph_text, paragraph_path, paragraph_bbox = [], [], None

            for block in blocks:
                kind = block.get("type") or "paragraph"
                title_path = list(block.get("title_path") or [])
                if kind != "paragraph":
                    flush_paragraphs(); groups.append(block); continue
                if paragraph_text and title_path != paragraph_path:
                    flush_paragraphs()
                paragraph_path = title_path
                paragraph_bbox = paragraph_bbox or block.get("bbox")
                paragraph_text.append(str(block.get("text") or ""))
            flush_paragraphs()

            for block in groups:
                for part in self._split(block.get("text", "")):
                    chunks.append({
                        "page_number": page["page_number"], "chunk_index": chunk_index,
                        "chunk_text": part, "extraction_method": page["extraction_method"],
                        "metadata": {
                            "titlePath": block.get("title_path") or [],
                            "blockType": block.get("type") or "paragraph",
                            "bbox": block.get("bbox"),
                            "parser": "docling" if page["extraction_method"] == "docling" else page["extraction_method"],
                            "parserVersion": DOCLING_VERSION,
                            "chunkerVersion": CHUNKER_VERSION,
                        },
                    })
                    chunk_index += 1
        return chunks

    def parse_directory(self, directory: str | Path) -> list[dict]:
        pdf_files = sorted(Path(directory).rglob("*.pdf"))
        if not pdf_files:
            raise FileNotFoundError(f"No PDF files found under {directory}")
        chunks: list[dict] = []
        for pdf_file in pdf_files:
            chunks.extend(self.parse_file(pdf_file))
        return chunks

    def _split(self, text: str) -> list[str]:
        step = self.chunk_size - self.chunk_overlap
        chunks = []
        for start in range(0, len(text), step):
            chunk = text[start:start + self.chunk_size].strip()
            if chunk:
                chunks.append(chunk)
            if start + self.chunk_size >= len(text):
                break
        return chunks
