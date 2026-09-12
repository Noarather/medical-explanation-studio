"""Application-owned Windows paths for the standalone desktop program."""

from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "MedExplainStudio"


def data_dir() -> Path:
    root = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    path = root / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    override = os.getenv("MEDEXPLAIN_DATABASE_PATH", "").strip()
    return Path(override).expanduser().resolve() if override else data_dir() / "med_explain.db"


def log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def temp_dir() -> Path:
    path = data_dir() / "temp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_dir() -> Path:
    path = data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_export_dir() -> Path:
    return Path.home() / "Documents" / "医学题库解析"
