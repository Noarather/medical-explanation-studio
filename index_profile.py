"""Deterministic textbook index configuration fingerprints."""

from __future__ import annotations

import hashlib
import json
from ocr_client import DEFAULT_OCR_MODEL, OCR_PROMPT_VERSION, effective_ocr_model
from pathlib import Path


INDEX_ALGORITHM_VERSION = "medexplain-index-v2"


def build_index_profile(config: dict, adopted_legacy: bool = False) -> dict:
    dash = config.get("dashscope", {})
    retrieval = config["retrieval"]
    profile = {
        "provider": "dashscope",
        "base_url": str(dash.get("base_url", "")).rstrip("/"),
        "model": str(dash.get("model", "unknown")),
        "dimension": int(dash.get("dimension", 0)),
        "chunk_size": int(retrieval["chunk_size"]),
        "chunk_overlap": int(retrieval["chunk_overlap"]),
        "algorithm": INDEX_ALGORITHM_VERSION,
        "parser": "cloud-text",
        "parser_version": "cloud-text-v1",
        "ocr_engine": effective_ocr_model(str(dash.get("ocr_model", DEFAULT_OCR_MODEL))),
        "ocr_prompt": OCR_PROMPT_VERSION,
        "chunker": "structured-page-v1",
    }
    if adopted_legacy:
        profile["adoptedLegacy"] = True
    return profile


def index_fingerprint(profile: dict) -> str:
    value = {key: value for key, value in profile.items() if key != "adoptedLegacy"}
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def retrieval_compatible(row: dict, profile: dict) -> bool:
    """Parsing provenance may differ; vector space must match exactly."""
    try:
        stored = json.loads(row.get("index_profile_json") or "{}")
        if not stored or row.get("index_fingerprint") != index_fingerprint(stored):
            return False
        keys = ("provider", "base_url", "model", "dimension", "algorithm")
        return all(stored.get(key) == profile.get(key) for key in keys)
    except (TypeError, ValueError, AttributeError):
        return False
