"""Text-layer extraction with page-by-page Qwen OCR and durable service checkpoints."""
from __future__ import annotations
import gc
import re
import tempfile
from pathlib import Path
from typing import Callable, Iterator, Optional
import fitz
from memory_guard import MemoryGuard, MemoryLimitExceeded, MemoryState

PARSER_VERSION = "cloud-text-v1"
CHUNKER_VERSION = "structured-page-v1"

def normalize_text(value: str) -> str:
    value = str(value or "").replace("\u00a0", " ").replace("\u3000", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()

class PDFParser:
    def __init__(self, chunk_size=500, chunk_overlap=50, model_dir=None, page_batch_size=8, memory_guard=None):
        if chunk_size <= 0 or chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError("chunk_size must be positive and overlap must be smaller")
        self.chunk_size, self.chunk_overlap = int(chunk_size), int(chunk_overlap)
        self.page_batch_size = max(1, int(page_batch_size))
        self.memory_guard = memory_guard
        # Compatibility only; never instantiate a local inference engine.
        self.model_dir = None
        self._converter = self._rapidocr = None

    def release_resources(self):
        self._converter = self._rapidocr = None
        gc.collect()

    @staticmethod
    def _render_page(page, clip=None):
        # Limit raster size even for unusually large physical pages.
        rect = clip or page.rect
        scale = min(200/72, 2800/max(rect.width, rect.height))
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            path = handle.name
        try:
            pixmap.save(path)
        except BaseException:
            Path(path).unlink(missing_ok=True)
            raise
        return path

    @staticmethod
    def needs_ocr(page, text):
        compact = re.sub(r"\s+", "", text)
        invalid = sum(c == "\ufffd" or ord(c) < 32 for c in compact)
        images = page.get_image_info()
        image_area = sum(abs(fitz.Rect(i["bbox"]).get_area()) for i in images)
        image_dominates = image_area > page.rect.get_area() * .50
        middle = page.rect.x0 + page.rect.width / 2
        blocks = [b for b in page.get_text("blocks") if len(b) > 6 and b[6] == 0]
        left = [b for b in blocks if b[2] < middle + 8 and len(b[4].strip()) > 30]
        right = [b for b in blocks if b[0] > middle - 8 and len(b[4].strip()) > 30]
        columns = any(min(l[3], r[3]) - max(l[1], r[1]) > 20 for l in left for r in right)
        # A page-wide scan with only a page number/watermark is not a text PDF.
        return columns or (bool(compact) and invalid > len(compact)*.02) or (
            bool(images) and (len(compact) < 80 or (image_dominates and len(compact) < 400))
        ) or (not compact and bool(page.get_drawings()))

    def _extract_page(self, page, number, ocr_client, force_ocr):
        value = normalize_text(page.get_text("text", sort=True))
        trace = {}
        method = "text"
        if force_ocr or self.needs_ocr(page, value):
            if ocr_client is None:
                raise RuntimeError(f"PDF 第 {number} 页需要云端 OCR，请配置 DashScope 密钥后重试")
            path = self._render_page(page)
            try:
                value = normalize_text(ocr_client.recognize_image(path))
                trace = dict(getattr(ocr_client, "last_trace", {}))
                if not value:
                    raise RuntimeError("云端 OCR 返回空内容")
                if value == "[空白页]":
                    value = ""
                method = "qwen_ocr" if value else "blank"
            except (MemoryError, MemoryLimitExceeded):
                raise
            except Exception as exc:
                raise RuntimeError(f"PDF 第 {number} 页 OCR 未完成：{exc}") from None
            finally:
                Path(path).unlink(missing_ok=True)
        return {"page_number": number, "text": value, "extraction_method": method if value else "blank",
                "error_message": "", "blocks": self._markdown_blocks(value), "ocr_trace": trace}

    def iter_page_batches(self, path, ocr_client=None, force_ocr=False, progress=None, *, start_page=1):
        pdf_path = Path(path)
        try:
            with fitz.open(pdf_path) as document:
                if document.needs_pass:
                    raise ValueError("PDF 已加密，请提供可直接读取的文件")
                total = len(document)
                for number in range(max(1, int(start_page)), total+1):
                    if self.memory_guard and self.memory_guard.sample("pdf_parse").state is not MemoryState.NORMAL:
                        self.memory_guard.recover("pdf_parse", self.release_resources, stage="cloud_parse",
                            file_path=str(pdf_path), page_range=(number, number), batch_size=1)
                    # Yield every page so the caller commits before the next paid call.
                    yield [self._extract_page(document[number-1], number, ocr_client, force_ocr)]
                    if progress:
                        progress(number, total)
        finally:
            self.release_resources()

    def extract_pages(self, path, ocr_client=None, force_ocr=False, progress=None):
        return [page for batch in self.iter_page_batches(path, ocr_client, force_ocr, progress) for page in batch]

    def parse_file(self, path):
        return [{"source_file": Path(path).name, "source_page": item["page_number"],
                 "chunk_index": item["chunk_index"], "chunk_text": item["chunk_text"]}
                for item in self.chunks_from_pages(self.extract_pages(path))]

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
                            "parser": page["extraction_method"],
                            "ocrTrace": page.get("ocr_trace", {}),
                            "parserVersion": PARSER_VERSION,
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
