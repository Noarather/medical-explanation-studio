"""Shared JSON protocol for QWebChannel bridge domains."""
from __future__ import annotations

import json
import traceback

from PySide6.QtCore import QObject, Slot


def ok(data) -> str:
    return json.dumps({"ok": True, "data": data}, ensure_ascii=False, default=str)


def err(code: str, message: str) -> str:
    return json.dumps(
        {"ok": False, "error": {"code": code, "message": message}}, ensure_ascii=False
    )


class BridgeBase(QObject):
    """Dispatch invoke(method, params_json) to api_<method>(**params) with uniform error wrapping."""

    @Slot(str, str, result=str)
    def invoke(self, method: str, params_json: str) -> str:
        handler = getattr(self, f"api_{method}", None)
        if handler is None:
            return err("unknown_method", f"未知方法：{method}")
        try:
            params = json.loads(params_json) if params_json else {}
        except json.JSONDecodeError:
            return err("bad_params", "参数不是合法 JSON")
        try:
            return ok(handler(**params))
        except TypeError as exc:
            return err("bad_params", str(exc))
        except Exception as exc:  # noqa: BLE001 - bridge must never raise into WebChannel
            traceback.print_exc()
            message = str(exc)
            code, sep, detail = message.partition(": ")
            if not (sep and code.isidentifier() and detail):
                code, detail = exc.__class__.__name__, message
            return err(code, detail)
