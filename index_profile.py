"""Deterministic textbook index configuration fingerprints."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


INDEX_ALGORITHM_VERSION = "medexplain-index-v2"


def build_index_profile(config: dict, adopted_legacy: bool = False) -> dict:
    dash = config.get("dashscope", {})
    retrieval = config["retrieval"]
    docling = config.get("docling", {})
    manifest = Path(str(docling.get("model_manifest") or Path(__file__).resolve().parent / "models" / "MODEL-MANIFEST.json"))
    if not manifest.is_absolute():
        manifest = Path(__file__).resolve().parent / manifest
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else "missing"
    profile = {
        "provider": "dashscope",
        "base_url": str(dash.get("base_url", "")).rstrip("/"),
        "model": str(dash.get("model", "unknown")),
        "dimension": int(dash.get("dimension", 0)),
        "chunk_size": int(retrieval["chunk_size"]),
        "chunk_overlap": int(retrieval["chunk_overlap"]),
        "algorithm": INDEX_ALGORITHM_VERSION,
        "parser": "docling",
        "parser_version": "2.117.0",
        "ocr_engine": "rapidocr-onnx-ch",
        "model_manifest_sha256": manifest_hash,
        "chunker": "structured-page-v1",
    }
    if adopted_legacy:
        profile["adoptedLegacy"] = True
    return profile


def index_fingerprint(profile: dict) -> str:
    value = {key: value for key, value in profile.items() if key != "adoptedLegacy"}
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
