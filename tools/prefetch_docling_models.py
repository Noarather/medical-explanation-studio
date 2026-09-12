"""Download all v1.7 offline parser assets and emit a SHA-256/size manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(ROOT / "models" / "docling"))
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)

    from docling.utils.model_downloader import download_models
    from docling.models.stages.ocr.rapid_ocr_model import RapidOcrModel
    download_models(
        output_dir=output, progress=True, with_layout=True, with_tableformer=True,
        with_code_formula=True, with_picture_classifier=False, with_rapidocr=False,
    )
    RapidOcrModel.download_models(
        backend="onnxruntime", lang="chinese",
        local_dir=output / RapidOcrModel._model_repo_folder,
        force=False, progress=True,
    )

    files = []
    for path in sorted(item for item in (ROOT / "models").rglob("*") if item.is_file() and item.name != "MODEL-MANIFEST.json"):
        files.append({
            "path": path.relative_to(ROOT).as_posix(), "bytes": path.stat().st_size,
            "sha256": sha256(path),
        })
    family_assets = {}
    family_patterns = {
        "layout": ("layout",), "tableformer": ("table",),
        "code-formula": ("codeformula", "code_formula", "code-formula"),
        "rapidocr-onnx-ch": ("rapidocr", "rapid_ocr"),
    }
    for family, patterns in family_patterns.items():
        matched = [item for item in files if any(pattern in item["path"].casefold() for pattern in patterns)]
        family_assets[family] = {"files": len(matched), "bytes": sum(item["bytes"] for item in matched)}
    license_files = [item for item in files if "license" in Path(item["path"]).name.casefold()]
    manifest = {
        "formatVersion": 1, "generated": True, "doclingVersion": "2.117.0",
        "families": ["layout", "tableformer", "code-formula", "rapidocr-onnx-ch"],
        "totalBytes": sum(item["bytes"] for item in files), "files": files,
        "familyAssets": family_assets, "licenseFiles": license_files,
        "licenses": [
            {"component": "Docling", "license": "MIT", "source": "https://github.com/docling-project/docling"},
            {"component": "RapidOCR", "license": "Apache-2.0", "source": "https://github.com/RapidAI/RapidOCR"},
        ],
    }
    (ROOT / "models" / "MODEL-MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps({"files": len(files), "totalBytes": manifest["totalBytes"]}))


if __name__ == "__main__":
    main()
