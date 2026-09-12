"""Shared application services for scanning, retrieval, generation, review and export."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from db_manager import DatabaseManager
from importers import (
    load_excel, load_json, normalize_formatted_rows, normalize_question,
    parse_common_question_text, parse_medical_question_collection_docx,
    parse_pharmacology_xlsx,
)
from memory_guard import MemoryGuard
from pdf_parser import PDFParser
from generation_executor import (
    AdaptiveConcurrencyController, ExplanationResult, FatalGenerationError,
    generate_general_with_retry, generate_with_retry, route_generation,
)
from llm_client import select_best_evidence
from question_format_v2 import make_envelope, normalize_question_v2, normalize_tags, write_xlsx_v2
from app_paths import backup_dir
from index_profile import build_index_profile, index_fingerprint
from reranker_client import DashScopeReranker
from retrieval_quality import hybrid_score


GENERAL_DISCLAIMER = "【声明：以下解析未在所选教材中找到达到阈值的直接依据，仅基于通用医学知识生成，请以权威教材复核。】"


_SUBJECT_GROUPS = (
    {"药理学", "临床药理学", "药物治疗学", "内科学"},
    {"病理学", "病理生理学", "内科学", "诊断学"},
    {"生理学", "病理生理学", "内科学"},
    {"外科学", "解剖学", "影像诊断学", "诊断学"},
    {"妇产科学", "儿科学", "内科学", "诊断学"},
)

_DEFAULT_SUBJECT_ALIASES = {
    "外基": "外科学",
    "外科": "外科学",
    "外科学基础": "外科学",
}


def normalize_subject_name(value: object) -> str:
    """Normalize a subject label without changing its learner-facing stored value."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def canonical_subject(value: object, config: Optional[dict] = None) -> str:
    """Resolve configured subject aliases for retrieval only.

    Alias chains are supported with a small cycle guard so a malformed local
    configuration cannot hang a generation worker.
    """
    aliases = {
        normalize_subject_name(source): normalize_subject_name(target)
        for source, target in _DEFAULT_SUBJECT_ALIASES.items()
    }
    configured = (config or {}).get("retrieval", {}).get("subject_aliases", {})
    if isinstance(configured, dict):
        for source, target in configured.items():
            source_name = normalize_subject_name(source)
            target_name = normalize_subject_name(target)
            if source_name and target_name:
                aliases[source_name] = target_name
    current = normalize_subject_name(value)
    visited: set[str] = set()
    for _ in range(8):
        if not current or current in visited or current not in aliases:
            break
        visited.add(current)
        current = aliases[current]
    return current


def extract_case_clues(question: dict) -> list[tuple[str, str]]:
    """Extract a few local, auditable case clues without an extra model request."""
    raw = question.get("raw") if isinstance(question.get("raw"), dict) else {}
    text = " ".join(filter(None, [str(raw.get("caseInfo") or ""), str(raw.get("question") or ""), str(question.get("prompt_text") or "")]))
    segments = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"[。；;！？!?\n]", text) if part.strip()]
    categories = (
        ("检验影像", r"(?:检查|检验|化验|血|尿|CT|MRI|超声|X线|提示|显示|升高|降低|阳性|阴性)"),
        ("病程体征", r"(?:患者|病人|主诉|病史|病程|查体|体征|疼痛|发热|咳|呕|腹泻|水肿|意识)"),
        ("既往用药", r"(?:既往|患有|服用|给予|用药|治疗|剂量|过敏|手术史)"),
    )
    clues: list[tuple[str, str]] = []
    for label, pattern in categories:
        candidates = [part for part in segments if re.search(pattern, part, re.I)]
        if candidates:
            clues.append((label, max(candidates, key=lambda item: min(len(item), 180))[:220]))
    return clues[:3]


def retrieval_queries(question: dict) -> list[tuple[str, str]]:
    raw = question.get("raw") if isinstance(question.get("raw"), dict) else {}
    qtype = str(raw.get("type") or "").upper()
    prompt = str(question.get("prompt_text") or "")
    clues = extract_case_clues(question)
    # Respect an explicit imported type.  A1 distractors often contain words
    # such as “检查/升高/降低”; treating any such word as a case clue doubled
    # retrieval work and produced misleading clue labels.  Only infer a case
    # from clues when the source did not provide a type.
    is_case = qtype in {"A2", "A3", "A4"} or (not qtype and bool(clues))
    stem = str(raw.get("question") or prompt)[:500]
    answer = str(raw.get("answer") or "")
    options = " ".join(str(item) for item in (raw.get("options") or []) if re.match(rf"^[{re.escape(answer)}][.、．:]", str(item), re.I))
    # ``prompt_text`` contains presentation metadata (question type, layout and
    # every distractor).  Feeding it to FTS made common words dominate the OR
    # query and turned a single textbook lookup into a multi-second scan.  The
    # raw stem plus the keyed answer keeps the medical concept while avoiding
    # UI-only text; it does not expose the imported explanation to retrieval.
    if not is_case:
        return [("题目核心", f"{stem} {options}".strip())]
    result = [("题目核心", f"{stem} {options}".strip())]
    result.extend((label, f"{value} {options}".strip()) for label, value in clues)
    unique: list[tuple[str, str]] = []
    for label, value in result:
        if value and value not in {item[1] for item in unique}:
            unique.append((label, value))
    return unique[:3]


def classify_evidence_grade(question: dict, result: dict, threshold: float) -> str:
    if not result.get("matched_chunks") or float(result.get("top_score") or 0) < threshold:
        return "C"
    scores = [float(item.get("score") or 0) for item in result.get("matched_chunks") or []]
    strong = bool(scores) and (
        scores[0] >= max(0.70, threshold + 0.10)
        or sum(score >= max(0.65, threshold + 0.05) for score in scores) >= 2
    )
    prompt = str(question.get("prompt_text") or "")
    terms = {
        item for item in re.findall(r"[\u4e00-\u9fff]{2,12}|[A-Za-z][A-Za-z0-9+\-/]{2,20}", prompt)
        if item not in {"患者", "下列", "的是", "正确", "错误", "关于", "答案", "题型"}
    }
    evidence_text = " ".join(str(item.get("text") or item.get("quote") or "") for item in result.get("matched_chunks") or [])
    covered = sum(1 for term in terms if term in evidence_text)
    coverage_ok = covered >= min(2, max(1, len(terms)))
    return "A" if strong and coverage_ok else "B"


def package_has_question_tags(package: dict | None) -> bool:
    """A newly suggested tag is valid output even before catalog promotion.

    New labels intentionally remain in ``suggestedTags`` until three distinct
    questions support them. Treating only catalog-backed ``tags`` as valid made
    every question in a new subject look like a formatting failure.
    """
    if not isinstance(package, dict):
        return False
    return bool(normalize_tags([
        *(package.get("tags") or []),
        *(package.get("suggestedTags") or []),
    ]))


def tokenize_for_fts(text: str) -> list[str]:
    try:
        import jieba
        tokens = [item.strip().lower() for item in jieba.cut(str(text)) if item.strip()]
    except ImportError:
        tokens = re.findall(r"[\u4e00-\u9fff]|[A-Za-z]+|\d+(?:\.\d+)?", str(text).lower())
    return [token for token in tokens if re.search(r"[\w\u4e00-\u9fff]", token)]


_FTS_QUERY_STOPWORDS = {
    "题型", "型题", "单句", "最佳", "选择", "选项", "答案", "正确", "错误",
    "有关", "关于", "下列", "以下", "其中", "描述", "说法", "关系", "的是",
    "一个", "一种", "主要", "可以", "可能", "应该", "应当", "属于", "不属于",
    "a", "b", "c", "d", "e", "f", "的", "了", "于", "与", "和", "或", "及",
    "中", "是", "为", "在", "有", "无", "可", "使", "由", "对", "最", "全",
}


def fts_query(text: str) -> str:
    useful = [token for token in tokenize_for_fts(text) if token not in _FTS_QUERY_STOPWORDS]
    # Sixteen focused terms are enough for the BM25 candidate stage.  Vector
    # reranking still decides the final evidence, so this cap improves latency
    # without changing the configured similarity threshold.
    unique = list(dict.fromkeys(useful))[:16]
    cleaned = [token.replace('"', "") for token in unique]
    return " OR ".join(f'"{token}"' for token in cleaned if token)


def classify_match_confidence(scores: list[float], threshold: float) -> str:
    """Classify retrieval without conflating evidence quality with LLM output validity."""
    ranked = sorted((float(value) for value in scores), reverse=True)
    if not ranked or ranked[0] < float(threshold):
        return "low"
    strong_floor = max(0.70, float(threshold) + 0.10)
    consensus_floor = max(0.65, float(threshold) + 0.05)
    if ranked[0] >= strong_floor or sum(value >= consensus_floor for value in ranked) >= 2:
        return "strong"
    return "standard"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


class CancelledError(RuntimeError):
    pass


class JobControl:
    def __init__(self, database: DatabaseManager, job_id: str):
        self.database = database
        self.job_id = job_id

    def checkpoint(self) -> None:
        while True:
            control = self.state()
            if control == "cancel":
                raise CancelledError("任务已取消")
            if control != "pause":
                return
            self.database.update_job(self.job_id, status="paused", message="已暂停")
            time.sleep(0.5)

    def state(self) -> str:
        job = self.database.get_job(self.job_id)
        return job.get("control", "run") if job else "cancel"

    def report(self, current: int, total: int, message: str) -> None:
        """Update progress without blocking; generation drains in-flight calls first."""
        self.database.update_job(
            self.job_id, status="running", current=current, total=total, message=message,
        )

    def progress(self, current: int, total: int, message: str) -> None:
        self.checkpoint()
        self.database.update_job(self.job_id, status="running", current=current, total=total, message=message)


class TextbookService:
    def __init__(self, database: DatabaseManager, config: dict, embedding_client, ocr_client=None):
        self.database = database
        self.config = config
        self.embedding_client = embedding_client
        self.ocr_client = ocr_client
        retrieval = config["retrieval"]
        docling = config.get("docling", {})
        model_dir = docling.get("model_dir")
        if model_dir and not Path(str(model_dir)).is_absolute():
            model_dir = Path(__file__).resolve().parent / str(model_dir)
        self.parser = PDFParser(
            retrieval["chunk_size"], retrieval["chunk_overlap"], model_dir,
            docling.get("page_batch_size", 8),
        )
        self.memory = MemoryGuard(config["memory_guard"]["max_memory_mb"])

    def scan_library(self, library_id: int, force_ocr: bool = False, control: Optional[JobControl] = None) -> dict:
        library = self.database.get_library(library_id)
        if not library:
            raise ValueError("教材不存在")
        path = Path(library["root_path"])
        # Compatibility for directory records created before the single-PDF model.
        if path.is_dir():
            known = [
                Path(row["absolute_path"]) for row in self.database.conn.execute(
                    "SELECT absolute_path FROM textbook_files WHERE library_id=? ORDER BY id", (library_id,)
                ).fetchall()
                if Path(row["absolute_path"]).suffix.lower() == ".pdf"
            ]
            candidates = [item for item in known if item.is_file()]
            if not candidates:
                candidates = sorted(path.glob("*.pdf"))
            if len(candidates) != 1:
                raise ValueError("该旧记录没有唯一 PDF，请点击“编辑”重新选择一个 PDF 文件")
            path = candidates[0]
            self.database.update_library(
                library_id, library["name"], library["subject"], library["version"],
                str(path), int(library.get("page_offset", 0)),
            )
        if not path.is_file() or path.suffix.lower() != ".pdf":
            raise FileNotFoundError(f"PDF 文件不可访问：{path}")
        if control:
            control.progress(0, 1, f"检查 {path.name}")
        absolute = str(path.resolve())
        stat = path.stat()
        existing = self.database.get_file_by_path(absolute)
        profile = build_index_profile(self.config)
        fingerprint = index_fingerprint(profile)
        is_complete = bool(
            existing
            and existing["status"] == "ready"
            and self.database.file_chunk_count(existing["id"]) > 0
            and existing.get("index_fingerprint") == fingerprint
        )
        if (
            not force_ocr and is_complete
            and existing["file_size"] == stat.st_size
            and existing["modified_ns"] == stat.st_mtime_ns
        ):
            self.database.finish_library_scan(library_id)
            message = f"索引已是最新：{path.name}，{existing['page_count']} 页"
            if control:
                control.progress(1, 1, message)
            return {"files": 1, "changed": 0, "skipped": 1, "message": message}

        digest = sha256_file(path)
        if not force_ocr and is_complete and existing["sha256"] == digest:
            self.database.upsert_file_metadata(
                library_id, absolute, path.name, stat.st_size, stat.st_mtime_ns, digest, existing["status"],
            )
            self.database.finish_library_scan(library_id)
            message = f"索引已是最新：{path.name}，{existing['page_count']} 页"
            if control:
                control.progress(1, 1, message)
            return {"files": 1, "changed": 0, "skipped": 1, "message": message}

        from parser_health import parser_health
        health = parser_health(self.parser.model_dir)
        if health["missing"]:
            raise RuntimeError(health["message"])
        if existing and self.database.file_chunk_count(int(existing["id"])):
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.database.backup_database(backup_dir() / f"before-index-v2-{existing['id']}-{stamp}.db")
        file_id = self.database.upsert_file_metadata(
            library_id, absolute, path.name, stat.st_size, stat.st_mtime_ns, digest,
        )
        try:
            pages = self.parser.extract_pages(
                path, self.ocr_client, force_ocr,
                (lambda current, total: control.progress(current, total, f"Docling 解析 {path.name}：{current}/{total} 页")) if control else None,
            )
            chunks = self.parser.chunks_from_pages(pages)
            if not chunks:
                raise RuntimeError("PDF 没有提取到可索引正文；请检查文件是否损坏或 OCR 配置")
            batch_size = int(self.config["memory_guard"]["batch_process_size"])
            for start in range(0, len(chunks), batch_size):
                if control:
                    control.checkpoint()
                    control.progress(start, len(chunks), f"向量化 {path.name} · {start}/{len(chunks)} 块")
                self.memory.check(f"教材向量化 {path.name}")
                batch = chunks[start:start + batch_size]
                vectors = self.embedding_client.get_embeddings([item["chunk_text"] for item in batch])
                for item, vector in zip(batch, vectors):
                    item["embedding"] = vector
                    item["search_text"] = " ".join(tokenize_for_fts(item["chunk_text"]))
            self.database.replace_file_content(file_id, library_id, pages, chunks)
            self.database.set_file_index_profile(file_id, profile, fingerprint)
            self.database.infer_library_page_offset(library_id)
        except Exception as exc:
            self.database.mark_file_error(file_id, str(exc))
            raise
        self.database.finish_library_scan(library_id)
        message = f"索引完成：{path.name}，{len(pages)} 页，{len(chunks)} 个文本块"
        if control:
            control.progress(len(chunks), len(chunks), message)
        return {"files": 1, "changed": 1, "skipped": 0, "pages": len(pages), "chunks": len(chunks), "message": message}


class RetrievalService:
    def __init__(self, database: DatabaseManager, config: dict):
        self.database = database
        self.config = config

    def retrieve(
        self, subject: str, query_text: str, query_embedding: list[float],
        library_ids: Optional[list[int]] = None,
    ) -> dict:
        settings = self.config["retrieval"]
        query = fts_query(query_text)
        bm25_limit = int(settings["bm25_top_k"])
        selected = list(dict.fromkeys(int(item) for item in (library_ids or []) if int(item) > 0))
        if len(selected) > 1:
            # Explicitly selected textbooks are a trusted course scope. Search each
            # book independently so a large surgery index cannot monopolise the BM25
            # candidate window, but do not also repeat a global query.
            per_book_limit = max(6, min(12, bm25_limit))
            by_id: dict[int, dict] = {}
            for library_id in selected:
                for item in self.database.search_chunks(
                    subject, query, per_book_limit, library_ids=[library_id],
                ):
                    by_id.setdefault(int(item["id"]), item)
            candidates = list(by_id.values())
        else:
            candidates = self.database.search_chunks(
                subject, query, bm25_limit,
                library_ids=selected if library_ids is not None else None,
            )
        candidates = [item for item in candidates if item.get("embedding") is not None]
        if not candidates:
            return {"matched_chunks": [], "top_score": 0.0, "confidence": "low", "is_matched": False}
        query = np.asarray(query_embedding, dtype=np.float32)
        matrix = np.asarray([item["embedding"] for item in candidates], dtype=np.float32)
        denominator = np.linalg.norm(matrix, axis=1) * np.linalg.norm(query)
        similarities = np.divide(matrix @ query, denominator, out=np.zeros(len(candidates)), where=denominator != 0)
        ranked_indices = [int(item) for item in np.argsort(similarities)[::-1]]
        evidence_limit = int(settings["final_top_k"])
        chosen_indices: list[int] = []
        if len(selected) > 1:
            # Diversity is useful only after a passage clears a quality floor. The
            # previous implementation forced one passage from every selected book,
            # which polluted evidence with unrelated 0.3-0.5 matches.
            evidence_limit = max(evidence_limit, min(4, len(selected)))
            support_floor = max(0.55, float(settings["similarity_threshold"]) - 0.05)
            for library_id in selected:
                match = next(
                    (
                        index for index in ranked_indices
                        if int(candidates[index].get("library_id") or 0) == library_id
                        and float(similarities[index]) >= support_floor
                    ),
                    None,
                )
                if match is not None and match not in chosen_indices:
                    chosen_indices.append(match)
            chosen_indices.sort(key=lambda index: float(similarities[index]), reverse=True)
            chosen_indices = chosen_indices[:evidence_limit]
        for index in ranked_indices:
            if index not in chosen_indices and (
                len(selected) <= 1
                or float(similarities[index]) >= max(0.55, float(settings["similarity_threshold"]) - 0.05)
            ):
                chosen_indices.append(index)
            if len(chosen_indices) >= evidence_limit:
                break
        if not chosen_indices and ranked_indices:
            # Preserve the best real candidate for unmatched auditing.
            chosen_indices.append(ranked_indices[0])
        chosen_indices.sort(key=lambda index: float(similarities[index]), reverse=True)
        evidence = []
        for index in chosen_indices[:evidence_limit]:
            item = candidates[index]
            textbook_page = int(item["textbook_page"])
            evidence.append({
                "chunk_id": item["id"], "text": item["chunk_text"],
                "library_id": int(item.get("library_id") or 0),
                "source_file": item["source_file"], "source_path": item["source_path"],
                "textbook": item.get("textbook") or item["source_file"],
                "textbook_version": item.get("textbook_version") or "",
                # source_page is the page printed in the textbook and used in citations.
                # pdf_page is retained solely for opening the correct physical PDF page.
                "source_page": textbook_page if textbook_page > 0 else None,
                "pdf_page": int(item["pdf_page"]),
                "extraction_method": item["extraction_method"],
                "score": float(similarities[index]),
            })
        candidate_evidence = []
        for index in ranked_indices:
            item = candidates[index]
            textbook_page = int(item["textbook_page"])
            candidate_evidence.append({
                "chunk_id": item["id"], "text": item["chunk_text"],
                "library_id": int(item.get("library_id") or 0),
                "source_file": item["source_file"], "source_path": item["source_path"],
                "textbook": item.get("textbook") or item["source_file"],
                "textbook_version": item.get("textbook_version") or "",
                "source_page": textbook_page if textbook_page > 0 else None,
                "pdf_page": int(item["pdf_page"]),
                "extraction_method": item["extraction_method"],
                "score": float(similarities[index]),
                "embedding_score": float(similarities[index]),
            })
        score = evidence[0]["score"] if evidence else 0.0
        threshold = float(settings["similarity_threshold"])
        confidence = classify_match_confidence(
            [float(item.get("score") or 0) for item in evidence], threshold,
        )
        return {
            "matched_chunks": evidence,
            # Full cosine-ranked pool is consumed only by the one-shot cloud
            # reranker. Existing callers continue to see the bounded evidence list.
            "candidate_chunks": candidate_evidence,
            "top_score": score,
            "confidence": confidence,
            "is_matched": confidence != "low",
        }


class GenerationService:
    def __init__(self, database: DatabaseManager, config: dict, embedding_client, llm_client, reranker=None):
        self.database = database
        self.config = config
        self.embedding_client = embedding_client
        self.llm_client = llm_client
        self.retrieval = RetrievalService(database, config)
        rerank_settings = config.get("retrieval", {}).get("rerank", {})
        self.reranker = reranker or DashScopeReranker(
            config.get("dashscope", {}).get("api_key", ""),
            rerank_settings.get("model", "qwen3-rerank"),
            rerank_settings.get("timeout_seconds", 30),
        )
        self.memory = MemoryGuard(config["memory_guard"]["max_memory_mb"])

    @staticmethod
    def _rerank_query(question: dict) -> str:
        """Use only the imported stem and keyed answer, never an old explanation."""
        rows = retrieval_queries(question)
        return rows[0][1] if rows else str(question.get("prompt_text") or "")

    def _finalize_retrieval(self, question: dict, result: dict) -> dict:
        settings = self.config.get("retrieval", {})
        rerank = settings.get("rerank", {})
        candidates = list(result.pop("candidate_chunks", None) or result.get("matched_chunks") or [])
        by_id: dict[object, dict] = {}
        for item in candidates:
            key = item.get("chunk_id") or (item.get("source_file"), item.get("pdf_page"))
            if key not in by_id or float(item.get("score") or 0) > float(by_id[key].get("score") or 0):
                by_id[key] = dict(item)
        candidates = sorted(by_id.values(), key=lambda item: float(item.get("score") or 0), reverse=True)
        limit = max(1, int(rerank.get("candidate_count", 20)))
        candidates = candidates[:limit]
        trace = {
            "enabled": bool(rerank.get("enabled", True)),
            "model": str(rerank.get("model", "qwen3-rerank")),
            "candidateCount": len(candidates), "succeeded": False,
            "degraded": False, "degradedReason": "", "requestId": "", "tokens": 0,
            "scores": [],
        }
        alpha = rerank.get("calibrated_alpha")
        calibrated_threshold = rerank.get("calibrated_threshold")
        calibrated = alpha is not None and calibrated_threshold is not None
        if candidates and trace["enabled"]:
            try:
                response = self.reranker.rerank(
                    self._rerank_query(question), [str(item.get("text") or "") for item in candidates],
                )
                trace.update({
                    "succeeded": True, "model": response.model,
                    "requestId": response.request_id, "tokens": response.tokens,
                    "elapsedMs": response.elapsed_ms, "calibrated": calibrated,
                })
                for index, item in enumerate(candidates):
                    cosine = float(item.get("embedding_score", item.get("score", 0)) or 0)
                    rerank_score = response.scores.get(index, 0.0)
                    combined = hybrid_score(cosine, rerank_score, float(alpha)) if calibrated else None
                    item.update({
                        "embedding_score": cosine, "rerank_score": rerank_score,
                        "combined_score": combined,
                        "score": combined if calibrated else cosine,
                        "_rank_score": combined if calibrated else rerank_score,
                    })
                candidates.sort(key=lambda item: float(item.get("_rank_score") or 0), reverse=True)
                if calibrated:
                    result["required_threshold"] = float(calibrated_threshold)
            except Exception as exc:
                trace.update({"degraded": True, "degradedReason": f"{type(exc).__name__}: {exc}"[:500]})
        elif trace["enabled"] and not candidates:
            trace.update({"degraded": True, "degradedReason": "no_candidates"})

        final_limit = max(1, int(settings.get("final_top_k", 3)))
        chosen: list[dict] = []
        seen_books: set[tuple[str, str]] = set()
        for item in candidates:
            book = (str(item.get("textbook") or item.get("source_file") or ""), str(item.get("textbook_version") or ""))
            if book in seen_books:
                continue
            public_item = {key: value for key, value in item.items() if not key.startswith("_")}
            chosen.append(public_item)
            seen_books.add(book)
            if len(chosen) >= final_limit:
                break
        if not chosen and candidates:
            chosen = [{key: value for key, value in candidates[0].items() if not key.startswith("_")}]
        for item in candidates:
            trace["scores"].append({
                "chunkId": item.get("chunk_id"),
                "embedding": round(float(item.get("embedding_score", item.get("score", 0)) or 0), 8),
                "rerank": round(float(item.get("rerank_score") or 0), 8) if trace["succeeded"] else None,
                "combined": round(float(item.get("combined_score")), 8) if item.get("combined_score") is not None else None,
            })
        result["matched_chunks"] = chosen
        result["top_score"] = float(chosen[0].get("score") or 0) if chosen else 0.0
        threshold = float(result.get("required_threshold") or settings.get("similarity_threshold", 0.5))
        result["confidence"] = classify_match_confidence([float(item.get("score") or 0) for item in chosen], threshold)
        result["is_matched"] = bool(chosen) and result["top_score"] >= threshold
        result["retrieval_trace"] = trace
        return result

    @staticmethod
    def _usable_libraries(libraries: list[dict], selected_ids: Optional[list[int]]) -> list[dict]:
        selected = set(selected_ids or [])
        result = []
        for item in libraries:
            if selected_ids is not None and int(item.get("id") or 0) not in selected:
                continue
            status = item.get("file_status")
            if status is not None and status not in {"ready", "warning"}:
                continue
            file_count = item.get("file_count")
            if file_count is not None and int(file_count or 0) <= 0:
                continue
            result.append(item)
        return result

    def _library_route(
        self, subject: str, libraries: list[dict],
        selected_library_ids: Optional[list[int]],
    ) -> dict:
        raw_subject = normalize_subject_name(subject)
        resolved_subject = canonical_subject(raw_subject, self.config)
        primary_rows = [
            item for item in libraries
            if canonical_subject(item.get("subject"), self.config) == resolved_subject
        ]
        primary_ids = [int(item["id"]) for item in primary_rows if int(item.get("id") or 0) > 0]
        primary_identity = {id(item) for item in primary_rows}
        non_primary = [item for item in libraries if id(item) not in primary_identity]

        if selected_library_ids is not None:
            # A user's explicit textbook selection is authoritative. Other selected
            # disciplines remain eligible, but only through the stricter cross-subject gate.
            secondary_rows = non_primary
        else:
            related: set[str] = set()
            for group in _SUBJECT_GROUPS:
                canonical_group = {canonical_subject(item, self.config) for item in group}
                if resolved_subject in canonical_group:
                    related.update(canonical_group - {resolved_subject})
            configured = self.config.get("retrieval", {}).get("subject_fallback_map", {})
            if isinstance(configured, dict):
                for key in (raw_subject, resolved_subject):
                    values = configured.get(key, [])
                    if isinstance(values, (list, tuple, set)):
                        related.update(canonical_subject(item, self.config) for item in values)
            secondary_rows = [
                item for item in non_primary
                if canonical_subject(item.get("subject"), self.config) in related
            ]
        secondary_ids = [int(item["id"]) for item in secondary_rows if int(item.get("id") or 0) > 0]
        return {
            "raw_subject": raw_subject,
            "resolved_subject": resolved_subject,
            "primary_rows": primary_rows,
            "primary_ids": primary_ids,
            "secondary_rows": secondary_rows,
            "secondary_ids": secondary_ids,
            "has_scope": bool(primary_rows or secondary_rows),
        }

    def retrieve_with_subject_fallback(
        self, question: dict, vector: list[float], libraries: list[dict],
        selected_library_ids: Optional[list[int]], query_vectors: Optional[list[tuple[str, str, list[float]]]] = None,
    ) -> dict:
        """Search locally selected disciplines without a synchronous model-routing request."""
        subject = str(question["subject"])
        route = self._library_route(subject, libraries, selected_library_ids)
        resolved_subject = route["resolved_subject"]
        queries = query_vectors or [("题目核心", question["prompt_text"], vector)]

        def search(ids: list[int] | None) -> dict:
            merged: dict[object, dict] = {}
            for clue, query, query_vector in queries:
                try:
                    found = self.retrieval.retrieve(resolved_subject, query, query_vector, ids)
                except TypeError as exc:
                    if ids is not None or "positional" not in str(exc):
                        raise
                    found = self.retrieval.retrieve(resolved_subject, query, query_vector)
                for item in found.get("candidate_chunks") or found.get("matched_chunks") or []:
                    item = {**item, "score": float(item.get("score") or found.get("top_score") or 0)}
                    key = item.get("chunk_id") or (item.get("source_file"), item.get("pdf_page"))
                    existing = merged.get(key)
                    if existing is None or float(item.get("score") or 0) > float(existing.get("score") or 0):
                        existing = dict(item)
                        merged[key] = existing
                    supported = list(existing.get("supportedClues") or [])
                    if clue not in supported:
                        supported.append(clue)
                    existing["supportedClues"] = supported
            evidence = sorted(merged.values(), key=lambda item: float(item.get("score") or 0), reverse=True)
            top_score = float(evidence[0].get("score") or 0) if evidence else 0.0
            threshold = float(self.config["retrieval"]["similarity_threshold"])
            confidence = classify_match_confidence([float(item.get("score") or 0) for item in evidence], threshold)
            return {
                "matched_chunks": evidence, "candidate_chunks": evidence, "top_score": top_score,
                "confidence": confidence, "is_matched": confidence != "low",
            }

        primary_rows = route["primary_rows"]
        primary_ids = route["primary_ids"]
        if selected_library_ids is not None:
            # A manual selection describes the course's trusted textbook scope, not
            # merely a fallback list. This matters for composite labels such as 外基:
            # physiology, pathology and pharmacology are co-primary sources and must
            # participate even when the surgery textbook already reaches 0.60.
            explicit_ids = [
                int(item["id"]) for item in libraries if int(item.get("id") or 0) > 0
            ]
            combined = search(explicit_ids)
            base_threshold = float(self.config["retrieval"]["similarity_threshold"])
            combined.update({
                "required_threshold": base_threshold,
                "raw_subject": route["raw_subject"],
                "resolved_subject": resolved_subject,
                "searched_library_ids": explicit_ids,
                "cross_subject": False,
                "scope_mode": "explicit_co_primary",
                "is_matched": bool(combined.get("matched_chunks"))
                and float(combined.get("top_score") or 0) >= base_threshold,
            })
            return self._finalize_retrieval(question, combined)
        if primary_ids:
            primary = search(primary_ids)
        elif selected_library_ids is None and primary_rows:
            # Compatibility seam for lightweight test/provider repositories without IDs.
            primary = search(None)
        else:
            primary = {"matched_chunks": [], "top_score": 0.0, "confidence": "low", "is_matched": False}
        base_threshold = float(self.config["retrieval"]["similarity_threshold"])
        primary.update({
            "required_threshold": base_threshold,
            "raw_subject": route["raw_subject"],
            "resolved_subject": resolved_subject,
            "searched_library_ids": list(primary_ids),
            "cross_subject": False,
        })
        if primary["is_matched"]:
            return self._finalize_retrieval(question, primary)

        routed_ids = route["secondary_ids"]
        if not routed_ids:
            return self._finalize_retrieval(question, primary)
        secondary = search(routed_ids)
        cross_threshold = max(0.70, base_threshold + 0.08)
        secondary.update({
            "required_threshold": cross_threshold,
            "raw_subject": route["raw_subject"],
            "resolved_subject": resolved_subject,
            "searched_library_ids": list(dict.fromkeys([*primary_ids, *routed_ids])),
            "cross_subject": True,
            "is_matched": bool(secondary.get("matched_chunks")) and float(secondary.get("top_score") or 0) >= cross_threshold,
        })
        if secondary["is_matched"]:
            return self._finalize_retrieval(question, secondary)
        # Preserve the best real score/evidence for audit even when it is below the
        # applicable gate. The required_threshold field prevents this from being
        # accepted using the lower primary threshold.
        return self._finalize_retrieval(
            question, secondary if secondary["top_score"] > primary["top_score"] else primary,
        )

    def generate_set(
        self, set_id: str, control: Optional[JobControl] = None,
        question_ids: Optional[list[int]] = None,
        library_ids: Optional[list[int]] = None,
        reuse_saved_evidence: bool = False,
        content_types: Optional[set] = None,
    ) -> dict:
        selected = set(content_types) if content_types is not None else {"explanation", "tags"}
        if not selected:
            return {
                "completed": 0, "failed": 0, "generated": 0, "unmatched": 0,
                "peak_concurrency": 0, "note": "未选择生成内容",
            }
        rows = self.database.queued_questions(set_id, question_ids) if "explanation" in selected else []
        job_started = time.monotonic()
        settings = self.config.get("generation", {})
        retrieval_settings = self.config.get("retrieval", {})
        match_threshold = float(retrieval_settings.get("similarity_threshold", 0.5))
        automatic_general_fallback = bool(retrieval_settings.get("generate_fallback", False))
        target_concurrency = min(16, max(1, int(settings.get("concurrency", 8))))
        adaptive = bool(settings.get("adaptive_concurrency", True))
        max_retries = max(1, int(settings.get("max_retries", 3)))
        success_window = max(1, int(settings.get("success_ramp_window", 20)))
        controller = AdaptiveConcurrencyController(target_concurrency, adaptive, success_window)
        pro_gate = threading.BoundedSemaphore(
            min(8, max(1, int(settings.get("pro_concurrency", 2))))
        )
        route_settings = {
            **settings,
            "model": self.config.get("llm", self.config.get("deepseek", {})).get("model", "deepseek-v4-flash"),
            "flash_model": settings.get("flash_model") or self.config.get("llm", self.config.get("deepseek", {})).get("model", "deepseek-v4-flash"),
        }
        stats = {
            "processed": 0, "generated": 0, "unmatched": 0, "failed": 0, "completed": 0,
            "rerankSucceeded": 0, "rerankDegraded": 0, "rerankTokens": 0,
        }
        route_counts = {"flash": 0, "flash_thinking": 0, "pro_thinking": 0}
        subject_routes: dict[str, int] = {}
        searched_library_ids: set[int] = set()
        performance = {"latencies_ms": [], "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "cache_hits": 0, "cache_misses": 0, "error_codes": {}}
        selected_library_ids = (
            list(dict.fromkeys(int(item) for item in library_ids if int(item) > 0))
            if library_ids is not None else None
        )
        libraries = self.database.list_libraries()
        usable_libraries = self._usable_libraries(libraries, selected_library_ids)
        if "docling" in self.config:
            current_fingerprint = index_fingerprint(build_index_profile(self.config))
            usable_libraries = [
                item for item in usable_libraries
                if str(item.get("index_fingerprint") or "") == current_fingerprint
            ]
        retrieval_cache: dict[tuple[str, str], dict] = {}
        counted_reranks: set[str] = set()
        pending: dict[Future, tuple[dict, dict]] = {}
        fatal_error: Optional[BaseException] = None
        automatic_general_ids: list[int] = []
        total = len(rows)
        active_tags: dict[str, list[str]] = {}
        for subject in {row["subject"] for row in rows}:
            try:
                active_tags[subject] = [item["label"] for item in self.database.list_question_tags(set_id, subject, "active")]
            except (AttributeError, TypeError):
                active_tags[subject] = []
        for row in rows:
            row["official_tags"] = active_tags.get(row["subject"], [])
            resolved = canonical_subject(row["subject"], self.config)
            route_label = (
                f"{normalize_subject_name(row['subject'])}→{resolved}"
                if resolved != normalize_subject_name(row["subject"]) else resolved
            )
            subject_routes[route_label] = subject_routes.get(route_label, 0) + 1

        def retrieval_meets_threshold(result: dict) -> bool:
            chunks = result.get("matched_chunks") or []
            required = float(result.get("required_threshold") or match_threshold)
            return bool(chunks) and float(result.get("top_score") or 0) >= required

        def remember_general_fallback(question: dict) -> None:
            if automatic_general_fallback:
                automatic_general_ids.append(int(question["id"]))

        def progress(message: str = "") -> None:
            if not control:
                return
            snapshot = controller.snapshot()
            detail = (
                f"{message} · 并发 {snapshot['active']}/{snapshot['limit']} · "
                f"已生成 {stats['generated']} · 未匹配/缺教材 {stats['unmatched']} · 失败 {stats['failed']}"
            ).strip(" ·")
            control.report(stats["completed"], total, detail)

        def save_future(future: Future) -> None:
            nonlocal fatal_error
            try:
                outcome: ExplanationResult = future.result()
            except FutureCancelledError:
                return
            except FatalGenerationError as exc:
                fatal_error = fatal_error or exc
                return
            except Exception as exc:
                fatal_error = fatal_error or exc
                return
            if not retrieval_meets_threshold(outcome.retrieval):
                self.database.save_generation_failure(
                    outcome.question["id"], "unmatched", "retrieval_below_threshold: 相似度未达到当前匹配阈值",
                    score=outcome.retrieval.get("top_score") or 0,
                    evidence=outcome.retrieval.get("matched_chunks") or [],
                )
                remember_general_fallback(outcome.question)
                stats["unmatched"] += 1
                stats["processed"] += 1
            elif outcome.error:
                attempt_meta = {
                    "errorCode": outcome.error_code or outcome.error_kind,
                    "requestCount": outcome.attempts,
                    "modelFallback": outcome.model_fallback,
                    "httpStatus": outcome.http_status,
                    "route": outcome.route.as_dict() if outcome.route else {},
                    "generationTrace": outcome.generation_trace or [],
                    "partialResponse": outcome.partial_response,
                }
                self.database.save_generation_failure(
                    outcome.question["id"], "error", f"{outcome.error_code or outcome.error_kind}: {outcome.error}",
                    score=outcome.retrieval.get("top_score") or 0,
                    evidence=outcome.retrieval.get("matched_chunks") or [],
                    attempt_meta=attempt_meta,
                )
                stats["failed"] += 1
                code = outcome.error_code or outcome.error_kind or "unknown"
                performance["error_codes"][code] = performance["error_codes"].get(code, 0) + 1
            else:
                package_meta = outcome.package.get("explanationMeta", {}) if outcome.package else {}
                traces = package_meta.get("generationTrace") if isinstance(package_meta.get("generationTrace"), list) else []
                if traces:
                    performance["latencies_ms"].append(sum(max(0, int(item.get("elapsedMs") or 0)) for item in traces if isinstance(item, dict)))
                    performance["prompt_tokens"] += sum(max(0, int(item.get("promptTokens") or 0)) for item in traces if isinstance(item, dict))
                    performance["completion_tokens"] += sum(max(0, int(item.get("completionTokens") or 0)) for item in traces if isinstance(item, dict))
                    performance["reasoning_tokens"] += sum(max(0, int(item.get("reasoningTokens") or 0)) for item in traces if isinstance(item, dict))
                    performance["cache_hits"] += sum(item.get("cacheStatus") == "hit" for item in traces if isinstance(item, dict))
                    performance["cache_misses"] += sum(item.get("cacheStatus") == "miss" for item in traces if isinstance(item, dict))
                package_text = outcome.explanation or ""
                stripped_text = package_text.strip()
                matched_chunks = outcome.retrieval.get("matched_chunks") or []
                package_blocks = outcome.package.get("explanationBlocks", []) if outcome.package else []
                package_sections = {
                    str(item.get("section") or "") for item in package_blocks if isinstance(item, dict)
                }
                needs_manual = bool(
                    not matched_chunks
                    or not stripped_text
                    or bool(outcome.package) and (
                        not package_has_question_tags(outcome.package)
                        or not outcome.package.get("briefExplanation")
                        or not {"analysis", "answerBasis", "pitfalls"}.issubset(package_sections)
                        or not any(
                            isinstance(item, dict) and item.get("section") == "pitfalls" and item.get("type") == "table"
                            for item in package_blocks
                        )
                    )
                    or package_meta.get("reviewWarnings")
                    or "待人工整理" in package_text
                    or "模型输出无法解析" in package_text
                    or (stripped_text.startswith("{") and '"explanationBlocks"' in stripped_text)
                    or (stripped_text.startswith("```json") and '"explanationBlocks"' in stripped_text)
                )
                if needs_manual:
                    self.database.save_generation_failure(
                        outcome.question["id"], "error", "模型输出结构异常，请重新生成",
                        score=outcome.retrieval["top_score"],
                        evidence=outcome.retrieval["matched_chunks"],
                    )
                    stats["failed"] += 1
                    performance["error_codes"]["schema_validation_failed"] = performance["error_codes"].get("schema_validation_failed", 0) + 1
                    stats["processed"] += 1
                    stats["completed"] += 1
                    progress(f"需人工整理 {outcome.question['external_id']}")
                    return
                if outcome.package:
                    grade = str(outcome.retrieval.get("evidence_grade") or "B").upper()
                    is_case = bool(extract_case_clues(outcome.question))
                    package_meta.update({
                        "evidenceGrade": grade,
                        "reasoningType": "case_chain" if is_case else "direct_answer",
                        "mode": "textbook" if grade == "A" else "textbook_reasoning",
                    })
                    if outcome.model_fallback:
                        package_meta["modelFallback"] = True
                    self.database.save_generated_result(
                        outcome.question["id"], "generated", package_meta["mode"],
                        outcome.retrieval["top_score"], outcome.explanation,
                        outcome.retrieval["matched_chunks"], package=outcome.package,
                        write_tags="tags" in selected,
                    )
                else:
                    self.database.save_generated_result(
                        outcome.question["id"], "generated", "textbook",
                        outcome.retrieval["top_score"], outcome.explanation,
                        outcome.retrieval["matched_chunks"],
                    )
                stats["processed"] += 1
                stats["generated"] += 1
            stats["completed"] += 1
            progress(f"已完成 {outcome.question['external_id']}")

        def collect_done(block: bool = False) -> None:
            if not pending:
                return
            done, _ = wait(
                tuple(pending), timeout=None if block else 0.2,
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                pending.pop(future, None)
                save_future(future)

        def cancel_unstarted() -> None:
            for future in list(pending):
                if future.cancel():
                    pending.pop(future, None)

        def drain_all() -> None:
            while pending:
                collect_done(block=True)

        def settle_control() -> None:
            if not control:
                return
            state = control.state()
            if state == "run":
                return
            if state == "cancel":
                cancel_unstarted()
            # Preserve results from requests already sent to the paid provider.
            drain_all()
            if state == "cancel":
                raise CancelledError("任务已取消")
            control.checkpoint()

        def embed_batch(batch: list[dict]) -> dict[tuple[str, str], tuple[list[float] | None, str]]:
            unique: dict[tuple[str, str], str] = {}
            for question in batch:
                cache_key = (question["subject"], question["prompt_text"])
                if cache_key not in retrieval_cache:
                    for _label, query in retrieval_queries(question):
                        unique.setdefault((question["subject"], query), query)
            if not unique:
                return {}
            keys = list(unique)
            try:
                vectors = self.embedding_client.get_embeddings([unique[key] for key in keys])
                if len(vectors) != len(keys):
                    raise RuntimeError("Embedding 返回数量与题目数量不一致")
                return {key: (vector, "") for key, vector in zip(keys, vectors)}
            except Exception:
                # A malformed input must not make the other nine questions fail.
                isolated: dict[tuple[str, str], tuple[list[float] | None, str]] = {}
                for key in keys:
                    try:
                        vector = self.embedding_client.get_embeddings([unique[key]])[0]
                        isolated[key] = (vector, "")
                    except Exception as exc:
                        isolated[key] = (None, str(exc))
                return isolated

        executor = ThreadPoolExecutor(max_workers=target_concurrency, thread_name_prefix="medexplain-llm")
        cursor = 0
        try:
            while cursor < total:
                settle_control()
                if fatal_error:
                    break
                batch = rows[cursor:cursor + 10]
                cursor += len(batch)
                viable: list[dict] = []
                for question in batch:
                    self.memory.check(f"题目 {question['external_id']}")
                    if reuse_saved_evidence:
                        if question.get("evidence"):
                            viable.append(question)
                        else:
                            self.database.save_generated_result(
                                question["id"], "error", "", question.get("match_score") or 0,
                                "", [], "没有可复用的教材证据，请重新选择教材检索",
                            )
                            stats["failed"] += 1
                            stats["completed"] += 1
                            progress(f"缺少已保存证据：{question['external_id']}")
                    elif not usable_libraries:
                        self.database.save_generated_result(
                            question["id"], "no_library", "", 0, "", [],
                            "no_compatible_library: 本次没有选择可用的已索引教材" if selected_library_ids is not None else "no_compatible_library: 没有可用教材库",
                        )
                        stats["unmatched"] += 1
                        stats["completed"] += 1
                        remember_general_fallback(question)
                        progress(f"缺少教材：{question['external_id']}")
                    elif not self._library_route(
                        question["subject"], usable_libraries, selected_library_ids,
                    )["has_scope"]:
                        resolved = canonical_subject(question["subject"], self.config)
                        self.database.save_generated_result(
                            question["id"], "no_library", "", 0, "", [],
                            f"no_compatible_library: 学科 {question['subject']}（检索为 {resolved}）没有兼容教材",
                        )
                        stats["unmatched"] += 1
                        stats["completed"] += 1
                        remember_general_fallback(question)
                        progress(f"无兼容教材：{question['external_id']}")
                    else:
                        viable.append(question)

                embedded = {} if reuse_saved_evidence else embed_batch(viable)
                for question in viable:
                    settle_control()
                    if fatal_error:
                        break
                    key = (question["subject"], question["prompt_text"])
                    result = retrieval_cache.get(key)
                    if result is None:
                        if reuse_saved_evidence:
                            saved = list(question.get("evidence") or [])
                            result = {
                                "matched_chunks": saved,
                                "top_score": float(question.get("match_score") or max(
                                    (float(item.get("score") or 0) for item in saved), default=0,
                                )),
                                "confidence": question.get("match_confidence") or "standard",
                                "is_matched": False,
                            }
                            result["is_matched"] = retrieval_meets_threshold(result)
                        else:
                            query_rows = retrieval_queries(question)
                            query_vectors = []
                            embedding_error = ""
                            for clue, query in query_rows:
                                vector, current_error = embedded.get((question["subject"], query), (None, "Embedding 未返回结果"))
                                if current_error or vector is None:
                                    embedding_error = current_error or "Embedding 未返回结果"
                                    break
                                query_vectors.append((clue, query, vector))
                            if embedding_error or not query_vectors:
                                self.database.save_generated_result(
                                    question["id"], "error", "", 0, "", [], embedding_error,
                                )
                                stats["failed"] += 1
                                stats["completed"] += 1
                                progress(f"向量失败：{question['external_id']}")
                                continue
                            try:
                                result = self.retrieve_with_subject_fallback(
                                    question, query_vectors[0][2], usable_libraries, selected_library_ids,
                                    query_vectors,
                                )
                                searched_library_ids.update(
                                    int(item) for item in result.get("searched_library_ids") or []
                                )
                                retrieval_cache[key] = result
                            except Exception as exc:
                                self.database.save_generated_result(
                                    question["id"], "error", "", 0, "", [], str(exc),
                                )
                                stats["failed"] += 1
                                stats["completed"] += 1
                                progress(f"检索失败：{question['external_id']}")
                                continue
                    if not retrieval_meets_threshold(result):
                        trace = result.get("retrieval_trace") if isinstance(result.get("retrieval_trace"), dict) else {}
                        if trace:
                            if hasattr(self.database, "set_question_extension"):
                                self.database.set_question_extension(question["id"], "retrievalTrace", trace)
                            identity = str(trace.get("requestId") or f"question-{question['id']}")
                            if identity not in counted_reranks:
                                counted_reranks.add(identity)
                                stats["rerankSucceeded"] += int(bool(trace.get("succeeded")))
                                stats["rerankDegraded"] += int(bool(trace.get("degraded")))
                                stats["rerankTokens"] += max(0, int(trace.get("tokens") or 0))
                        self.database.save_generated_result(
                            question["id"], "unmatched", "", result["top_score"], "",
                            result["matched_chunks"], "retrieval_below_threshold: 相似度未达到阈值",
                        )
                        stats["unmatched"] += 1
                        stats["processed"] += 1
                        stats["completed"] += 1
                        remember_general_fallback(question)
                        progress(f"未匹配：{question['external_id']}")
                        continue
                    trace = result.get("retrieval_trace") if isinstance(result.get("retrieval_trace"), dict) else {}
                    if trace:
                        if hasattr(self.database, "set_question_extension"):
                            self.database.set_question_extension(question["id"], "retrievalTrace", trace)
                        identity = str(trace.get("requestId") or f"question-{question['id']}")
                        if identity not in counted_reranks:
                            counted_reranks.add(identity)
                            stats["rerankSucceeded"] += int(bool(trace.get("succeeded")))
                            stats["rerankDegraded"] += int(bool(trace.get("degraded")))
                            stats["rerankTokens"] += max(0, int(trace.get("tokens") or 0))
                    result["evidence_grade"] = classify_evidence_grade(question, result, match_threshold)
                    result = {**result, "matched_chunks": select_best_evidence(
                        question["prompt_text"], result.get("matched_chunks") or [],
                    )}
                    result["matched_chunks"] = [
                        {**item, "evidence_grade": result["evidence_grade"]}
                        for item in result["matched_chunks"]
                    ]
                    while (
                        len(pending) >= max(2, controller.snapshot()["limit"] * 2)
                        and not fatal_error
                    ):
                        collect_done()
                        settle_control()
                    if fatal_error:
                        break
                    route = route_generation(question, result, route_settings)
                    route_key = "pro_thinking" if route.model != route.fallback_model else "flash_thinking" if route.thinking else "flash"
                    route_counts[route_key] += 1
                    future = executor.submit(
                        generate_with_retry, self.llm_client, question, result, controller,
                        max_retries, route=route, pro_gate=pro_gate,
                    )
                    pending[future] = (question, result)
                    progress(f"生成中：{question['external_id']}")

                # Persist already completed requests while the next embedding batch is prepared.
                collect_done()

            if fatal_error:
                cancel_unstarted()
            drain_all()
            if fatal_error:
                raise fatal_error
        except CancelledError:
            cancel_unstarted()
            drain_all()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        study_points_stats = {"updated": 0, "failed": 0}
        if "studyPoints" in selected:
            missing = self.database.list_missing_study_point_questions(set_id, include_approved=True)
            if question_ids:
                wanted = {int(item) for item in question_ids}
                missing = [row for row in missing if int(row["id"]) in wanted]
            for offset in range(0, len(missing), 20):
                if control:
                    control.checkpoint()
                batch = missing[offset:offset + 20]
                try:
                    generated = self.llm_client.generate_study_point_batch(batch)
                except Exception:
                    generated = {}
                for row in batch:
                    points = generated.get(str(row["id"]))
                    if not points:
                        try:
                            points = self.llm_client.generate_study_point_batch([row]).get(str(row["id"]))
                        except Exception:
                            points = None
                    if points:
                        try:
                            self.database.apply_study_point_backfill(row["id"], points)
                            study_points_stats["updated"] += 1
                        except Exception:
                            study_points_stats["failed"] += 1
                    else:
                        study_points_stats["failed"] += 1

        snapshot = controller.snapshot()
        automatic_result = {
            "generated": 0, "failed": 0, "peak_concurrency": 0, "throttle_events": 0,
        }
        if automatic_general_fallback and automatic_general_ids:
            progress(f"自动通识生成：{len(automatic_general_ids)} 题")
            automatic_result = self.generate_general_batch(
                set_id, control, list(dict.fromkeys(automatic_general_ids)),
                write_tags="tags" in selected,
            )
            stats["generated"] += int(automatic_result["generated"])
            stats["failed"] += int(automatic_result["failed"])
            stats["unmatched"] = max(0, stats["unmatched"] - int(automatic_result["generated"]))
        progress("任务完成")
        elapsed_seconds = max(0.001, time.monotonic() - job_started)
        ordered_latency = sorted(performance.pop("latencies_ms"))
        percentile = lambda ratio: ordered_latency[min(len(ordered_latency) - 1, max(0, round((len(ordered_latency) - 1) * ratio)))] if ordered_latency else 0
        performance.update({
            "elapsed_seconds": round(elapsed_seconds, 3),
            "questions_per_minute": round(stats["generated"] * 60 / elapsed_seconds, 2),
            "p50_ms": percentile(0.50), "p95_ms": percentile(0.95),
            "structured_failure_rate": round(stats["failed"] / max(1, stats["generated"] + stats["failed"]), 4),
        })
        result = {
            "processed": stats["processed"],
            "generated": stats["generated"],
            "unmatched": stats["unmatched"],
            "failed": stats["failed"],
            "auto_general_generated": int(automatic_result["generated"]),
            "peak_concurrency": max(snapshot["peak"], int(automatic_result["peak_concurrency"])),
            "throttle_events": snapshot["throttle_events"] + int(automatic_result["throttle_events"]),
            "route_counts": route_counts,
            "subject_routes": subject_routes,
            "searched_library_count": len(searched_library_ids),
            "performance": performance,
            "studyPoints": study_points_stats,
            "rerankSucceeded": stats["rerankSucceeded"],
            "rerankDegraded": stats["rerankDegraded"],
            "rerankTokens": stats["rerankTokens"],
        }
        if "tags" in selected and "explanation" not in selected:
            result["note"] = "标签需随解析一起生成"
        return result

    def generate_general(self, question_pk: int) -> str:
        question = self.database.get_imported_question(question_pk)
        if not question or question["pipeline_status"] not in {"unmatched", "no_library"}:
            raise ValueError("只有未匹配题目可以授权通识生成")
        package = self.llm_client.generate_fallback_package(question["prompt_text"]) if hasattr(self.llm_client,"generate_fallback_package") else None
        generated = package["explanation"] if package else self.llm_client.generate_fallback_explanation(question["prompt_text"])
        explanation = generated if generated.startswith(GENERAL_DISCLAIMER) else f"{GENERAL_DISCLAIMER}\n\n{generated}"
        if package:
            from question_format_v2 import normalize_blocks, blocks_to_markdown
            package.setdefault("explanationMeta", {}).update({
                "mode": "general_knowledge", "evidenceGrade": "C",
                "reasoningType": "case_chain" if extract_case_clues(question) else "direct_answer",
            })
            package["explanationBlocks"] = [{"blockId":"b00-disclaimer","section":"analysis","type":"callout","title":"来源声明","tone":"warning","text":GENERAL_DISCLAIMER},*package["explanationBlocks"]]
            package["explanation"] = blocks_to_markdown(package["explanationBlocks"])
            if package.get("explanationMeta", {}).get("reviewWarnings") or not package_has_question_tags(package) or not package.get("briefExplanation"):
                self.database.save_generated_result(
                    question_pk, question["pipeline_status"], "", question["match_score"], package["explanation"],
                    question["evidence"], "通识解析缺少可用题目标签或一句话简析，请重新生成", package=package,
                )
                raise ValueError("通识解析不完整，题目已保留在未匹配区，请重新生成")
            self.database.save_generated_result(question_pk,"generated","general_knowledge",question["match_score"],package["explanation"],question["evidence"],package=package)
            explanation=package["explanation"]
        else:
            self.database.save_generated_result(question_pk,"generated","general_knowledge",question["match_score"],explanation,question["evidence"])
        return explanation

    def generate_general_batch(
        self, set_id: str, control: Optional[JobControl] = None,
        question_ids: Optional[list[int]] = None,
        write_tags: bool = True,
    ) -> dict:
        """Generate concise authorized explanations concurrently for unmatched questions."""
        rows = self.database.general_generation_questions(set_id, question_ids)
        settings = self.config.get("generation", {})
        target = min(16, max(1, int(settings.get("concurrency", 8))))
        controller = AdaptiveConcurrencyController(
            target, bool(settings.get("adaptive_concurrency", True)),
            max(1, int(settings.get("success_ramp_window", 20))),
        )
        max_retries = max(1, int(settings.get("max_retries", 3)))
        pending: dict[Future, dict] = {}
        next_index = 0
        completed = generated = failed = 0
        fatal_error: Optional[BaseException] = None
        total = len(rows)

        def report(message: str) -> None:
            if control:
                snapshot = controller.snapshot()
                control.report(
                    completed, total,
                    f"{message} · 并发 {snapshot['active']}/{snapshot['limit']} · 已生成 {generated} · 失败 {failed}",
                )

        def persist(outcome: ExplanationResult) -> None:
            nonlocal completed, generated, failed, fatal_error
            try:
                if outcome.error:
                    self.database.save_generation_failure(
                        outcome.question["id"], outcome.question.get("pipeline_status", "unmatched"),
                        f"{outcome.error_code or outcome.error_kind}: {outcome.error}",
                        score=outcome.question.get("match_score") or 0,
                        evidence=outcome.question.get("evidence") or [],
                        attempt_meta={
                            "errorCode": outcome.error_code or outcome.error_kind,
                            "requestCount": outcome.attempts,
                            "httpStatus": outcome.http_status,
                        },
                    )
                    failed += 1
                else:
                    package = outcome.package
                    if not package or package.get("explanationMeta", {}).get("reviewWarnings") or not package_has_question_tags(package) or not package.get("briefExplanation"):
                        self.database.save_generation_failure(
                            outcome.question["id"], outcome.question.get("pipeline_status", "unmatched"),
                            "通识解析缺少可用题目标签或一句话简析，请重新生成",
                            score=outcome.question.get("match_score") or 0,
                            evidence=outcome.question.get("evidence") or [],
                        )
                        failed += 1
                    else:
                        from question_format_v2 import blocks_to_markdown
                        package.setdefault("explanationMeta", {}).update({
                            "mode": "general_knowledge", "evidenceGrade": "C",
                            "reasoningType": "case_chain" if extract_case_clues(outcome.question) else "direct_answer",
                        })
                        package["explanationBlocks"] = [
                            {"blockId":"b00-disclaimer","section":"analysis","type":"callout",
                             "title":"来源声明","tone":"warning","text":GENERAL_DISCLAIMER},
                            *package["explanationBlocks"],
                        ]
                        package["explanation"] = blocks_to_markdown(package["explanationBlocks"])
                        self.database.save_generated_result(
                            outcome.question["id"], "generated", "general_knowledge",
                            outcome.question.get("match_score") or 0, package["explanation"],
                            outcome.question.get("evidence") or [], package=package,
                            write_tags=write_tags,
                        )
                        generated += 1
            except Exception as exc:
                failed += 1
                fatal_error = fatal_error or exc
            completed += 1
            report(f"已处理 {outcome.question['external_id']}")

        def consume(future: Future) -> None:
            nonlocal fatal_error
            if future.cancelled():
                return
            try:
                outcome = future.result()
            except FatalGenerationError as exc:
                fatal_error = fatal_error or exc
            except Exception as exc:
                fatal_error = fatal_error or exc
            else:
                persist(outcome)

        executor = ThreadPoolExecutor(max_workers=target, thread_name_prefix="general")
        try:
            while next_index < total or pending:
                state = control.state() if control else "run"
                if state == "cancel":
                    for future in pending:
                        future.cancel()
                    if pending:
                        done, _ = wait(pending, return_when=FIRST_COMPLETED)
                        for future in done:
                            pending.pop(future)
                            consume(future)
                        continue
                    raise CancelledError("任务已取消")
                if state == "pause" and pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        pending.pop(future)
                        consume(future)
                    continue
                if state == "pause" and not pending:
                    control.checkpoint()
                    continue
                limit = max(1, controller.snapshot()["limit"] * 2)
                while next_index < total and len(pending) < limit:
                    question = rows[next_index]; next_index += 1
                    future = executor.submit(
                        generate_general_with_retry, self.llm_client, question, controller, max_retries,
                    )
                    pending[future] = question
                if pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        pending.pop(future)
                        consume(future)
                if fatal_error:
                    for future in pending:
                        future.cancel()
                    raise fatal_error
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        snapshot = controller.snapshot()
        report("批量通识生成完成")
        return {"processed":completed,"generated":generated,"failed":failed,
                "peak_concurrency":snapshot["peak"],"throttle_events":snapshot["throttle_events"]}


class TagBackfillService:
    """Fill only missing tags/briefs while preserving explanations and review states."""

    def __init__(self, database: DatabaseManager, llm_client):
        self.database = database
        self.llm_client = llm_client

    def estimate(self, set_id: str, include_approved: bool = True) -> dict:
        rows = self.database.list_missing_tag_questions(set_id, include_approved)
        repairable = sum(bool(row.get("repairable")) for row in rows)
        approved = sum(row.get("review_status") == "approved" for row in rows)
        return {
            "total": len(rows), "repairable": repairable,
            "requires_regeneration": len(rows) - repairable,
            "approved": approved, "estimated_requests": (repairable + 19) // 20,
        }

    def run(
        self, set_id: str, control: Optional[JobControl] = None,
        include_approved: bool = True,
    ) -> dict:
        all_rows = self.database.list_missing_tag_questions(set_id, include_approved)
        rows = [row for row in all_rows if row.get("repairable")]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.database.backup_database(backup_dir() / f"before-tag-backfill-{stamp}.db")
        stats = {
            "processed": 0, "updated": 0, "failed": 0,
            "requires_regeneration": len(all_rows) - len(rows), "backup": backup,
        }
        official_cache: dict[str, list[str]] = {}
        changed_subjects: set[str] = set()
        for subject in sorted({str(row.get("subject") or "") for row in rows}):
            official_cache[subject] = [
                item["label"] for item in self.database.list_question_tags(set_id, subject, "active")
            ]

        def report(message: str) -> None:
            if control:
                control.progress(
                    stats["processed"], len(rows),
                    f"{message} · 已补齐 {stats['updated']} · 失败 {stats['failed']} · 需重生成 {stats['requires_regeneration']}",
                )

        for offset in range(0, len(rows), 20):
            if control:
                control.checkpoint()
            batch = rows[offset:offset + 20]
            catalog = {subject: official_cache.get(subject, []) for subject in {row["subject"] for row in batch}}
            try:
                generated = self.llm_client.generate_tag_batch(batch, catalog)
            except Exception:
                generated = {}
            for row in batch:
                key = str(row["id"])
                result = generated.get(key)
                if not result:
                    try:
                        result = self.llm_client.generate_tag_batch(
                            [row], {row["subject"]: official_cache.get(row["subject"], [])},
                        ).get(key)
                    except Exception:
                        result = None
                if result and result.get("tags"):
                    try:
                        self.database.apply_tag_backfill(
                            row["id"], result["tags"], result.get("briefExplanation"),
                            sync_catalog=False,
                        )
                        changed_subjects.add(str(row.get("subject") or ""))
                        stats["updated"] += 1
                    except Exception:
                        stats["failed"] += 1
                else:
                    stats["failed"] += 1
                stats["processed"] += 1
                report(f"处理 {row['external_id']}")
            for subject in changed_subjects:
                self.database.sync_question_tags(set_id, subject)
            changed_subjects.clear()
        report("标签补齐完成")
        return stats


class StudyPointBackfillService:
    """Fill review-grade study points while preserving explanations and review states."""

    def __init__(self, database: DatabaseManager, llm_client):
        self.database = database
        self.llm_client = llm_client

    def estimate(self, set_id: str, include_approved: bool = True) -> dict:
        rows = self.database.list_missing_study_point_questions(set_id, include_approved)
        return {
            "total": len(rows),
            "approved": sum(row.get("review_status") == "approved" for row in rows),
            "estimated_requests": (len(rows) + 19) // 20,
        }

    def run(
        self, set_id: str, control: Optional[JobControl] = None,
        include_approved: bool = True,
    ) -> dict:
        rows = self.database.list_missing_study_point_questions(set_id, include_approved)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.database.backup_database(backup_dir() / f"before-study-point-backfill-{stamp}.db")
        stats = {"processed": 0, "updated": 0, "failed": 0, "backup": backup}

        def report(message: str) -> None:
            if control:
                control.progress(
                    stats["processed"], len(rows),
                    f"{message} · 已补齐 {stats['updated']} · 失败 {stats['failed']}",
                )

        for offset in range(0, len(rows), 20):
            if control:
                control.checkpoint()
            batch = rows[offset:offset + 20]
            try:
                generated = self.llm_client.generate_study_point_batch(batch)
            except Exception:
                generated = {}
            for row in batch:
                key = str(row["id"])
                result = generated.get(key)
                if not result:
                    try:
                        result = self.llm_client.generate_study_point_batch([row]).get(key)
                    except Exception:
                        result = None
                if result:
                    try:
                        self.database.apply_study_point_backfill(row["id"], result)
                        stats["updated"] += 1
                    except Exception:
                        stats["failed"] += 1
                else:
                    stats["failed"] += 1
                stats["processed"] += 1
                report(f"处理 {row['external_id']}")
        report("考点补齐完成")
        return stats


class MemoryCardBackfillService:
    """Generate the standalone memory-card layer without changing legacy analysis fields."""

    def __init__(self, database: DatabaseManager, llm_client):
        self.database = database
        self.llm_client = llm_client

    def estimate(self, set_id: str, include_approved: bool = True) -> dict:
        rows = self.database.list_missing_memory_card_questions(set_id, include_approved)
        return {
            "total": len(rows),
            "approved": sum(row.get("review_status") == "approved" for row in rows),
            "estimated_requests": (len(rows) + 19) // 20,
        }

    def run(
        self, set_id: str, control: Optional[JobControl] = None,
        include_approved: bool = True,
    ) -> dict:
        rows = self.database.list_missing_memory_card_questions(set_id, include_approved)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.database.backup_database(backup_dir() / f"before-memory-card-backfill-{stamp}.db")
        stats = {"processed": 0, "updated": 0, "failed": 0, "backup": backup}

        def report(message: str) -> None:
            if control:
                control.progress(
                    stats["processed"], len(rows),
                    f"{message} · 已生成 {stats['updated']} · 失败 {stats['failed']}",
                )

        for offset in range(0, len(rows), 20):
            if control:
                control.checkpoint()
            batch = rows[offset:offset + 20]
            try:
                generated = self.llm_client.generate_memory_card_batch(batch)
            except Exception:
                generated = {}
            for row in batch:
                key = str(row["id"])
                result = generated.get(key)
                if not result:
                    try:
                        result = self.llm_client.generate_memory_card_batch([row]).get(key)
                    except Exception:
                        result = None
                if result:
                    try:
                        self.database.apply_memory_card_backfill(
                            row["id"], result,
                            generator=getattr(self.llm_client, "model", "analysis-tool"),
                        )
                        stats["updated"] += 1
                    except Exception:
                        stats["failed"] += 1
                else:
                    stats["failed"] += 1
                stats["processed"] += 1
                report(f"处理 {row['external_id']}")
        report("独立背诵知识卡生成完成")
        return stats


class V2UpgradeService:
    """Restructure existing explanations in place while preserving review state."""
    def __init__(self,database:DatabaseManager,config:dict,llm_client):self.database=database;self.config=config;self.llm_client=llm_client

    def estimate(self,set_id:str)->dict:
        rows=self.database.v2_upgrade_questions(set_id);eligible=sum(bool(row["evidence"] or str(row["explanation"] or "").strip()) for row in rows)
        return {"questions":len(rows),"estimated_requests":eligible,"skipped":len(rows)-eligible}

    def run(self,set_id:str,control:Optional[JobControl]=None)->dict:
        rows=self.database.v2_upgrade_questions(set_id);stamp=datetime.now().strftime("%Y%m%d-%H%M%S");backup=self.database.backup_database(backup_dir()/f"before-v2-upgrade-{stamp}.db")
        fingerprint=index_fingerprint(build_index_profile(self.config));threshold=float(self.config["retrieval"]["similarity_threshold"])
        stats={"updated":0,"textbook":0,"restructured":0,"skipped":0,"failed":0,"backup":backup}
        for index,row in enumerate(rows,1):
            if control:control.checkpoint();control.progress(index-1,len(rows),f"升级 {row['external_id']} · 已完成 {stats['updated']}")
            try:
                official=[item["label"] for item in self.database.list_question_tags(set_id,row["subject"],"active")]
                compatible=bool(self.database.compatible_library_files(row["subject"],fingerprint))
                if compatible and row["evidence"] and float(row["match_score"] or 0)>=threshold:
                    package=self.llm_client.generate_explanation_package(row["prompt_text"],row["evidence"],official);stats["textbook"]+=1
                elif str(row["explanation"] or "").strip():
                    package=self.llm_client.restructure_explanation_package(row["explanation"],official);stats["restructured"]+=1
                else:stats["skipped"]+=1;continue
                package.setdefault("explanationMeta",{})["model"]=getattr(self.llm_client,"model","")
                self.database.replace_v2_upgrade_result(row["id"],package);stats["updated"]+=1
            except CancelledError:raise
            except Exception:stats["failed"]+=1
        if control:control.progress(len(rows),len(rows),f"升级完成 · 更新 {stats['updated']} · 跳过 {stats['skipped']} · 失败 {stats['failed']}")
        return stats


class QuestionImportService:
    def __init__(self, database: DatabaseManager):
        self.database = database

    def import_file(self, path: str | Path, name: str, mapping: Optional[dict[str, str]] = None) -> str:
        source = Path(path)
        if source.suffix.lower() == ".json":
            questions = load_json(source)
            source_type = "json"
        elif source.suffix.lower() in {".xlsx", ".xlsm"}:
            questions = load_excel(source, mapping or {})
            source_type = "excel"
        else:
            raise ValueError("仅支持 JSON、XLSX 和 XLSM 文件")
        return self.database.create_question_set(name or source.stem, str(source.resolve()), source_type, questions)


class QuestionFormattingService:
    """Local-first raw question organization with optional DeepSeek fallback."""

    def __init__(self, llm_client=None):
        self.llm_client = llm_client

    @staticmethod
    def _chunks(text: str, max_chars: int = 8000) -> list[str]:
        if len(text) <= max_chars:
            return [text]
        lines = text.splitlines()
        chunks: list[str] = []
        current: list[str] = []
        size = 0
        for line in lines:
            if current and size + len(line) + 1 > max_chars:
                chunks.append("\n".join(current))
                current = []
                size = 0
            current.append(line)
            size += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        return chunks

    @staticmethod
    def validation_messages(rows: list[dict]) -> list[list[str]]:
        return [normalize_question(row, index)[1] for index, row in enumerate(rows, start=1)]

    def organize_file(
        self, path: str | Path, default_subject: str = "",
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> dict:
        """Organize a supported structured source without flattening it to a huge string."""
        source = Path(path)
        if source.suffix.lower() in {".xlsx", ".xlsm"}:
            parsed = parse_pharmacology_xlsx(source, progress)
            if parsed is None:
                raise ValueError("该 Excel 不符合药理学平铺题库格式，请使用普通 Excel 字段映射导入")
            rows, stats = parsed
            method = "pharmacology-xlsx"
        else:
            rows, stats = parse_medical_question_collection_docx(path, default_subject, progress)
            method = "structured-docx"
        rows = normalize_formatted_rows(rows, stats.get("subject") or default_subject)
        return {
            "rows": rows,
            "method": method,
            "warnings": self.validation_messages(rows),
            "stats": stats,
        }

    def organize(
        self, raw_text: str, default_subject: str = "", use_ai: bool = True,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> dict:
        from selective_organize import organize
        return organize(raw_text, default_subject, self.llm_client if use_ai else None, progress)


class ExportService:
    def __init__(self, database: DatabaseManager):
        self.database = database

    @staticmethod
    def _atomic_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
            os.replace(temp_name, path)
        finally:
            Path(temp_name).unlink(missing_ok=True)

    def export(self, set_id: str, output_dir: str | Path, split_by_subject: bool = False) -> dict[str, object]:
        set_row = next((item for item in self.database.list_question_sets() if item["id"] == set_id), None)
        if not set_row:
            raise ValueError("题目集不存在")
        rows = self.database.approved_questions(set_id)
        if not rows:
            raise ValueError("该题目集还没有已批准题目")
        safe_name = re.sub(r"[<>:\"/\\|?*]+", "_", set_row["name"]).strip() or "题目集"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        json_path = Path(output_dir) / f"{safe_name}-{stamp}-v2.json"
        xlsx_path = Path(output_dir) / f"{safe_name}-{stamp}-v2.xlsx"
        map_path = Path(output_dir) / f"{safe_name}-{stamp}-解析映射.json"
        full_rows = []
        mapping = {}
        banks = {str(json.loads(row["raw_json"]).get("bank") or "").strip() for row in rows} - {""}
        if len(banks) > 1:
            raise ValueError("一个 v2 文件只能包含一个题库；当前题目集混有多个 bank")
        bank = next(iter(banks), "school")
        for row in rows:
            raw = json.loads(row["raw_json"])
            evidence = json.loads(row["evidence_json"] or "[]")
            raw["bank"] = raw.get("bank") or bank
            raw["explanation"] = row["explanation"]
            raw["explanationMeta"] = {**(raw.get("explanationMeta") or {}),
                "mode": row["generation_mode"], "score": row["match_score"],
                "evidence": evidence, "reviewedAt": row["reviewed_at"],}
            raw = normalize_question_v2(raw)
            full_rows.append(raw)
            mapping[row["external_id"]] = {
                "explanation": raw["explanation"], "mode": row["generation_mode"],
                "score": row["match_score"], "evidence": raw["explanationMeta"]["evidence"],
            }
        if split_by_subject:
            stamp_files: list[str] = []
            by_subject: dict[str, list[dict]] = {}
            for row in full_rows:
                by_subject.setdefault(str(row.get("subject") or "未分类"), []).append(row)
            for subject in sorted(by_subject):
                safe_subject = re.sub(r"[<>:\"/\\|?*]+", "_", subject).strip() or "未分类"
                subject_rows = by_subject[subject]
                catalog = self.database.list_question_tags(set_id, subject)
                envelope = make_envelope(subject_rows, name=f"{set_row['name']}-{subject}", bank=bank, source="med-explain", tag_catalog=[{
                    "subject":item["subject"], "label":item["label"], "aliases":item.get("aliases",[]),
                    "status":item["status"], "usageCount":item["usage_count"], "mergedInto":item.get("merged_into","")
                } for item in catalog])
                json_path = Path(output_dir) / f"{safe_name}-{safe_subject}-{stamp}-v2.json"
                xlsx_path = Path(output_dir) / f"{safe_name}-{safe_subject}-{stamp}-v2.xlsx"
                self._atomic_json(json_path, envelope)
                xlsx_path.parent.mkdir(parents=True, exist_ok=True)
                temp_xlsx = xlsx_path.with_suffix(".tmp.xlsx")
                write_xlsx_v2(temp_xlsx, envelope); os.replace(temp_xlsx, xlsx_path)
                self.database.add_export_record(set_id, "json_v2", str(json_path), len(subject_rows))
                self.database.add_export_record(set_id, "xlsx_v2", str(xlsx_path), len(subject_rows))
                stamp_files.extend([str(json_path), str(xlsx_path)])
            self._atomic_json(map_path, mapping)
            self.database.add_export_record(set_id, "mapping", str(map_path), len(full_rows))
            return {"files": stamp_files, "mapping": str(map_path)}
        catalog = self.database.list_question_tags(set_id)
        envelope = make_envelope(full_rows, name=set_row["name"], bank=bank, source="med-explain", tag_catalog=[{
            "subject":item["subject"], "label":item["label"], "aliases":item.get("aliases",[]),
            "status":item["status"], "usageCount":item["usage_count"], "mergedInto":item.get("merged_into","")
        } for item in catalog])
        self._atomic_json(json_path, envelope)
        xlsx_path.parent.mkdir(parents=True, exist_ok=True)
        temp_xlsx = xlsx_path.with_suffix(".tmp.xlsx")
        write_xlsx_v2(temp_xlsx, envelope); os.replace(temp_xlsx, xlsx_path)
        self._atomic_json(map_path, mapping)
        self.database.add_export_record(set_id, "json_v2", str(json_path), len(rows))
        self.database.add_export_record(set_id, "xlsx_v2", str(xlsx_path), len(rows))
        self.database.add_export_record(set_id, "mapping", str(map_path), len(rows))
        return {"full": str(json_path), "json":str(json_path), "xlsx":str(xlsx_path), "mapping": str(map_path)}

    def export_issues(self, set_id: str, output_dir: str | Path) -> str:
        rows = self.database.list_imported_questions(set_id, limit=100000)
        issues = [row for row in rows if row["pipeline_status"] in {"unmatched", "no_library", "error"}]
        path = Path(output_dir) / f"问题报告-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["题目ID", "学科", "处理状态", "匹配分数"])
            for row in issues:
                writer.writerow([row["external_id"], row["subject"], row["pipeline_status"], row["match_score"]])
        return str(path)
