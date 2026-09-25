"""Cancellable textbook metadata and printed-page inspection. Cloud OCR for scanned margins."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

import fitz

SUBJECTS = sorted({"病理生理学", "病理学", "生理学", "系统解剖学", "局部解剖学", "人体解剖学",
    "组织学与胚胎学", "生物化学与分子生物学", "生物化学", "医学免疫学", "医学微生物学",
    "人体寄生虫学", "药理学", "诊断学", "内科学", "外科学", "妇产科学", "儿科学", "神经病学",
    "精神病学", "传染病学", "眼科学", "耳鼻咽喉头颈外科学", "口腔科学", "皮肤性病学",
    "医学影像学", "预防医学", "流行病学", "卫生统计学", "医学遗传学", "急诊医学", "康复医学"}, key=len, reverse=True)
NUMBER = re.compile(r"^[\s·•—–\-]*(?:第\s*)?(\d{1,4})(?:\s*页)?[\s·•—–\-]*$")


def file_identity(path):
    path = Path(path)
    before = path.stat()
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("文件读取期间发生变化，请重新识别")
    return {"size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": digest}


def compact(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def edition(text):
    text = compact(text)
    match = re.search(r"第([0-9一二三四五六七八九十百]+)版", text)
    if not match:  # Some title pages store the large edition numeral after '第 版'.
        match = re.search(r"第版(\d{1,2})(?!\d)", text)
    if not match:
        return ""
    value = match.group(1)
    if not value.isdigit():
        digits = {c: n for n, c in enumerate("零一二三四五六七八九")}
        if "十" in value:
            left, right = value.split("十", 1)
            number = (digits.get(left, 1) if left else 1) * 10 + digits.get(right, 0)
        else:
            number = digits.get(value, 0)
        value = str(number) if number else value
    return f"第{value}版"


def infer_metadata(filename, title="", front_text=""):
    stem = Path(filename).stem.strip()
    def subject(value):
        value = compact(value)
        return next((name for name in SUBJECTS if name in value), "")
    filename_subject, content_subject = subject(stem), subject(title) or subject(front_text)
    chosen = filename_subject or content_subject
    filename_version, content_version = edition(stem), edition(front_text) or edition(title)
    version = filename_version or content_version
    warnings = []
    if filename_subject and content_subject and filename_subject != content_subject:
        warnings.append("文件名与书内学科信息不一致，请核对")
    if filename_version and content_version and filename_version != content_version:
        warnings.append("文件名与书内版次不一致，请核对")
    # Do not discard meaningful title qualifiers such as '学习指导' or '实验'.
    name = stem if filename_subject else (title.strip() if subject(title) else chosen or stem)
    if version and not edition(name):
        name = f"{name}（{version}）"
    if not chosen:
        warnings.append("未可靠识别学科，请手动填写")
    if not version:
        warnings.append("未识别版次，可手动填写或留空")
    return {"name": name, "subject": chosen, "version": version, "warnings": warnings,
            "metadata_source": "文件名 + 书内文字交叉检查" if filename_subject and content_subject else
            "文件名" if filename_subject else "PDF 书名／前置页" if content_subject else "文件名（待确认）"}


def margin_numbers(page):
    """Only isolated numeric lines in header/footer bands; ignore body/table numbers."""
    height = page.cropbox.height
    found = set()
    for block in page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT)["blocks"]:
        for line in block.get("lines", []):
            _x0, y0, _x1, y1 = line["bbox"]
            if not (y1 <= height * .10 or y0 >= height * .90):
                continue
            text = "".join(span["text"] for span in line["spans"])
            match = NUMBER.fullmatch(unicodedata.normalize("NFKC", text))
            if match and 0 < int(match[1]) <= 20000:
                found.add(int(match[1]))
    return sorted(found)


def build_calibration(observations, page_count):
    """Keep corroborated runs, interpolate short gaps, never extrapolate across resets."""
    per_page = {}
    for item in observations:
        per_page.setdefault(int(item["pdf_page"]), []).append(item)
    points = []
    for pdf_page, rows in sorted(per_page.items()):
        values = {int(row["printed_page"]) for row in rows}
        if len(values) == 1 and 1 <= pdf_page <= page_count:
            points.append({**rows[0], "pdf_page": pdf_page, "printed_page": values.pop()})
    runs = []
    for point in points:
        offset = point["pdf_page"] - point["printed_page"]
        if runs and offset == runs[-1][0] and point["pdf_page"] - runs[-1][1][-1]["pdf_page"] <= 12:
            runs[-1][1].append(point)
        else:
            runs.append((offset, [point]))
    segments, mapping, anchors = [], {}, []
    for offset, rows in runs:
        if len(rows) < 2:
            continue
        start, end = rows[0]["pdf_page"], rows[-1]["pdf_page"]
        # Two observations must be adjacent; longer spans require >=3 observations.
        if len(rows) == 2 and end - start != 1:
            continue
        segments.append({"pdf_start": start, "pdf_end": end, "printed_start": start-offset,
                         "printed_end": end-offset, "offset": offset, "support": len(rows),
                         "confidence": "high" if len(rows) >= 3 else "medium"})
        for number in range(start, end + 1):
            # Ambiguous observations are not filled by interpolation.
            if number not in per_page or len({r["printed_page"] for r in per_page[number]}) == 1:
                mapping[str(number)] = number - offset
        anchors.extend(rows)
    dominant = max(segments, key=lambda x: x["support"], default=None)
    return {"algorithm": "printed-margins-v1", "page_count": page_count,
            "status": "recognized" if dominant else "unresolved",
            "offset": dominant["offset"] if dominant else None,
            "segments": segments, "mapping": mapping, "anchors": anchors,
            "mapped_pages": len(mapping), "unknown_pages": page_count - len(mapping),
            "message": "按已验证编号区间校准；区间外只保留 PDF 页码，不猜测印刷页" if dominant else
            "没有足够连续页码，请手动指定 PDF 页和课本页的对应关系"}


class TextbookInspector:
    def __init__(self, ocr_client=None):
        self._ocr = ocr_client

    def _ocr_text(self, page, margins=False):
        from pdf_parser import PDFParser
        if self._ocr is None:
            raise ValueError("未配置云端 OCR")
        rects = [page.rect]
        if margins:
            r = page.rect
            rects = [fitz.Rect(r.x0, r.y0, r.x1, r.y0+r.height*.12),
                     fitz.Rect(r.x0, r.y1-r.height*.12, r.x1, r.y1)]
        lines = []
        for rect in rects:
            path = PDFParser._render_page(page, clip=rect)
            try:
                lines.append(self._ocr.recognize_image(path, plain_text=True))
            finally:
                Path(path).unlink(missing_ok=True)
        return "\n".join(lines).strip()

    def inspect(self, path, progress=lambda stage, current, total: None):
        source = Path(path).resolve()
        if source.suffix.lower() != ".pdf" or not source.is_file():
            raise ValueError("请选择可访问的 PDF 文件")
        identity = file_identity(source)
        warnings, observations, scan_pages, front = [], [], [], []
        with fitz.open(source) as doc:
            if doc.needs_pass:
                raise ValueError("PDF 已加密，请先提供可直接打开的副本")
            total = len(doc)
            if not 0 < total <= 20000:
                raise ValueError("PDF 页数为空或超过 20000 页，请拆分后导入")
            for i, page in enumerate(doc):
                progress("读取页码", i+1, total)
                try:
                    text = page.get_text()
                    if i < 5:
                        front.append(text[:8000])
                    numbers = margin_numbers(page)
                    observations.extend({"pdf_page": i+1, "printed_page": n, "method": "pdf_text"} for n in numbers)
                    if len(text.strip()) < 20 and page.get_images():
                        scan_pages.append(i)
                except Exception:
                    warnings.append(f"PDF 第 {i+1} 页无法读取，请核对")
            metadata = infer_metadata(source.name, (doc.metadata or {}).get("title", ""), "\n".join(front))
            # Bounded cloud OCR: adjacent triples distributed through scanned pages.
            # No unobserved long gap is assigned a guessed page offset.
            sampled = scan_pages
            if len(sampled) > 60:
                picks = {round(j*(len(sampled)-3)/19)+k for j in range(20) for k in range(3)}
                sampled = [scan_pages[i] for i in sorted(picks)]
                warnings.append("扫描页较多，本次云端 OCR 抽样最多 60 页；未确认区间请手动校准")
            ocr_failed = False
            for j, i in enumerate(sampled):
                progress("云端 OCR 校准", j+1, len(sampled))
                try:
                    full = i < 3 and (not metadata["subject"] or not metadata["version"])
                    text = self._ocr_text(doc[i], margins=not full)
                    if full:
                        front.append(text)
                        text = self._ocr_text(doc[i], margins=True)
                    for line in text.splitlines():
                        match = NUMBER.fullmatch(unicodedata.normalize("NFKC", line))
                        if match and int(match[1]) > 0:
                            observations.append({"pdf_page": i+1, "printed_page": int(match[1]), "method": "cloud_ocr"})
                except Exception:
                    ocr_failed = True
                    break  # A missing/broken engine should not be retried for every page.
            if ocr_failed:
                warnings.append("云端 OCR 未完成或未配置；保留已识别文字，请手动核对页码")
            metadata = infer_metadata(source.name, (doc.metadata or {}).get("title", ""), "\n".join(front))
        if file_identity(source) != identity:
            raise ValueError("PDF 已变化，请重新识别")
        calibration = build_calibration(observations, total)
        return {**metadata, "root_path": str(source), "file_name": source.name, "page_count": total,
                "identity": identity, "calibration": calibration,
                "warnings": metadata["warnings"] + warnings, "error": ""}
