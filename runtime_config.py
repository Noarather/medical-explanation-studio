"""Resolve YAML defaults, Windows credentials and user-editable runtime settings."""

from __future__ import annotations

from main import ROOT, resolved_config


def runtime_config(config_path: str | None = None, database_override: str | None = None) -> dict:
    return resolved_config(config_path or str(ROOT / "config.yaml"), database_override)
