"""Public build identity; never includes settings, credentials or user paths."""
import json
from pathlib import Path

VERSION = "2.2.3"


def build_info():
    try:
        data = json.loads((Path(__file__).resolve().parent / "_build-info.json").read_text(encoding="utf-8-sig"))
        return {key: str(data.get(key, "")) for key in ("version", "buildId", "builtAt", "revision")}
    except (OSError, ValueError, AttributeError):
        return {"version": VERSION, "buildId": "source", "builtAt": "源码运行", "revision": ""}
