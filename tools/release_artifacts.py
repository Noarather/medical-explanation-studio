"""Release provenance and read-only-source database backup. No implicit publishing."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree


def sha256(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def source_files(root):
    # Explicit source areas: never recurse into user textbooks, data or release bundles.
    root = Path(root).resolve()
    files = {p for p in root.iterdir() if p.is_file() and p.suffix.lower() in
             {".py", ".md", ".ps1", ".spec", ".iss", ".ini"}}
    for name in (".gitignore", ".gitattributes", "LICENSE", "NOTICE", "config.yaml", "requirements.txt", "requirements-eval.txt", "version_info.txt",
                 "frontend/package.json", "frontend/package-lock.json", "frontend/index.html", "models/MODEL-MANIFEST.json"):
        if (root / name).is_file():
            files.add(root / name)
    files.update(p for p in (root / "frontend").glob("*") if p.is_file() and p.suffix in {".ts", ".js", ".json", ".cjs"})
    for folder in ("ui_bridge", "tools", "docs", "evals", "assets", "schema", "frontend/src", "frontend/public"):
        for path in (root / folder).rglob("*"):
            if path.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(root).parts):
                if path.suffix.lower() in {".py", ".md", ".json", ".jsonl", ".ts", ".js", ".vue", ".css", ".svg", ".ico", ".html"}:
                    files.add(path)
    for path in files:
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            raise ValueError("Source symlink outside release scope")
        # Reject obvious credentials before writing any archive, never echo their values.
        if path.suffix not in {".ico"}:
            if re.search(rb"sk-[A-Za-z0-9_-]{24,}", path.read_bytes()):
                raise ValueError(f"Possible credential in source: {path.relative_to(root)}")
    return sorted(files)


def snapshot(root, output):
    root, output = Path(root).resolve(), Path(output)
    files = source_files(root)
    manifest = {}
    with zipfile.ZipFile(output / "SOURCE.zip", "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            content = path.read_bytes()
            name = path.relative_to(root).as_posix()
            manifest[name] = hashlib.sha256(content).hexdigest()
            archive.writestr(name, content)
    write_json(output / "SOURCE-MANIFEST.json", manifest)
    return manifest


def verify(root, manifest):
    root = Path(root).resolve()
    for name, expected in manifest.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or sha256(path) != expected:
            raise ValueError(f"Manifest mismatch: {name}")


def backup(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file() or source == destination:
        raise ValueError("A real source database and a separate backup path are required")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Reserve an exclusive new backup, never overwrite a previous backup.
    with destination.open("xb"):
        pass
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Database backup integrity check failed")
    return {"backupPath": str(destination), "integrity": "ok", "sha256": sha256(destination)}


def test_counts(xml_path, frontend_path):
    cases = list(ElementTree.parse(xml_path).getroot().iter("testcase"))
    failures = sum(c.find("failure") is not None or c.find("error") is not None for c in cases)
    skipped = sum(c.find("skipped") is not None for c in cases)
    front = json.loads(Path(frontend_path).read_text(encoding="utf-8"))
    if not cases or failures or not front.get("success") or front.get("numFailedTests"):
        raise ValueError("Release tests did not pass")
    return {"pythonPassed": len(cases) - skipped, "pythonSkipped": skipped,
            "frontendPassed": front["numPassedTests"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["snapshot", "verify-source", "record", "backup", "verify-app"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--release", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--signed", action="store_true")
    args = parser.parse_args()
    if args.action == "backup":
        print(json.dumps(backup(args.source, args.destination), ensure_ascii=False))
        return
    release = args.release.resolve()
    if args.action == "snapshot":
        snapshot(args.root, release)
    elif args.action == "verify-source":
        verify(args.root, json.loads((release / "SOURCE-MANIFEST.json").read_text(encoding="utf-8")))
    elif args.action == "verify-app":
        verify(args.source, json.loads((release / "APP-MANIFEST.json").read_text(encoding="utf-8")))
    elif args.action == "record":
        verify(args.root, json.loads((release / "SOURCE-MANIFEST.json").read_text(encoding="utf-8")))
        tests = test_counts(release / "checks/python.xml", release / "checks/frontend.json")
        for name in ("parser.json", "ui.json"):
            report = json.loads((release / "checks" / name).read_text(encoding="utf-8"))
            if not report.get("ok"):
                raise ValueError(f"Packaged check failed: {name}")
        app = release / "portable/MedExplainStudio"
        models = json.loads((args.root / "models/MODEL-MANIFEST.json").read_text(encoding="utf-8"))
        verify(app / "_internal", {item["path"]: item["sha256"] for item in models["files"]})
        manifest = {p.relative_to(app).as_posix(): sha256(p) for p in app.rglob("*") if p.is_file()}
        write_json(release / "APP-MANIFEST.json", manifest)
        artifacts = [{"name": p.name, "bytes": p.stat().st_size, "sha256": sha256(p)}
                     for p in sorted((release / "installer").iterdir()) if p.suffix in {".exe", ".bin"}]
        if not any(p["name"].endswith(".bin") for p in artifacts):
            raise ValueError("Missing split installer data")
        info = json.loads((release / "_build-info.json").read_text(encoding="utf-8-sig"))
        write_json(release / "BUILD.json", {**info, "tests": tests, "artifacts": artifacts,
            "signed": args.signed, "liveProviderApiTested": False, "localInstallStatus": "pending",
            "applicationSourceUnchangedDuringBuild": True, "sourceFileCount": len(source_files(args.root)),
            "modelManifest": "models/MODEL-MANIFEST.json"})
        write_json(release / "MODEL-MANIFEST.json", models)
        with (release / "installer/SHA256.txt").open("x", encoding="ascii") as handle:
            handle.write("".join(f"{item['sha256']}  {item['name']}\n" for item in artifacts))


if __name__ == "__main__":
    main()
