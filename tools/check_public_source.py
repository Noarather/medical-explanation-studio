"""Conservative tracked-file gate for public source releases; prints locations, never values."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


BLOCKED_DIRS = {"data", "output", "exports", "backups", "kaoyan-import", "work", "release", "build",
                "dist", "node_modules", ".venv", ".venv-build", ".codex", ".agents", ".superpowers"}
BLOCKED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pdf", ".xlsx", ".xlsm", ".docx", ".pem", ".key",
                    ".pfx", ".p12", ".exe", ".bin", ".onnx", ".pt", ".safetensors", ".zip", ".7z", ".log"}
PATTERNS = {
    "provider-token": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{25,}|github_pat_[A-Za-z0-9_]{25,}|AKIA[A-Z0-9]{16})\b"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "credential-in-url": re.compile(r"https?://[^\s/:]+:[^\s/@]+@"),
}


def candidates(root):
    result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                            cwd=root, capture_output=True)
    if result.returncode:
        raise ValueError("Run this check in an initialized Git repository")
    return sorted(set(path for path in result.stdout.decode("utf-8").split("\0") if path))


def inspect(root, names):
    findings = []
    for name in names:
        relative = Path(name)
        path = root / relative
        def issue(kind, line=None):
            findings.append({"path": name, "category": kind, **({"line": line} if line else {})})
        if (set(relative.parts) & BLOCKED_DIRS or relative.suffix.lower() in BLOCKED_SUFFIXES or
                relative.name == "HANDOFF.md" or relative.name.startswith(".env") and relative.name != ".env.example" or
                relative.parts[0] == "models" and name != "models/MODEL-MANIFEST.json"):
            issue("private-data-or-artifact")
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            issue("symlink-or-external-path")
            continue
        if not path.is_file():
            issue("missing-file")
            continue
        if path.stat().st_size > 2_000_000:
            issue("unexpected-large-file")
            continue
        if relative.suffix.lower() == ".ico":
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            for kind, pattern in PATTERNS.items():
                if pattern.search(line):
                    issue(kind, number)
        if name == "config.yaml":
            for number, line in enumerate(text.splitlines(), 1):
                if re.match(r"\s*api_key\s*:", line):
                    value = line.split(":", 1)[1].strip().strip('"\'')
                    if value and not re.fullmatch(r"\$\{[A-Z0-9_]+\}", value):
                        issue("non-placeholder-config-key", number)
    return findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    files = candidates(root)
    findings = inspect(root, files)
    print(json.dumps({"files_checked": len(files), "ok": not findings, "findings": findings}, ensure_ascii=False, indent=2))
    raise SystemExit(1 if findings else 0)


if __name__ == "__main__":
    main()
