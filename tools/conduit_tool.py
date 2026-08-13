"""Conduit tool - unified web/browser/search/extraction tool backed by conduit-browser."""
from __future__ import annotations
import json
import logging
from typing import Any

from . import register_tool

log = logging.getLogger(__name__)

try:
    from conduit_browser import ConduitBridge, SUPPORTED_ACTIONS
    _CONDUIT_AVAILABLE = True
except ImportError:
    log.warning("conduit-browser not installed; conduit tool will return errors")
    ConduitBridge = None  # type: ignore
    _CONDUIT_AVAILABLE = False


async def conduit_call(action: str, *, _bridge: "ConduitBridge | None" = None, **kwargs: Any) -> dict[str, Any]:
    """Execute a Conduit action. The _bridge is injected by the runtime per-job.

    action: e.g. 'navigate', 'web_search', 'extract_main', 'screenshot'
    kwargs: action-specific arguments per Conduit's docs
    """
    if not _CONDUIT_AVAILABLE:
        return {
            "ok": False,
            "error": "conduit_not_installed",
            "message": "conduit-browser package is not available in this deployment",
        }
    if _bridge is None:
        return {
            "ok": False,
            "error": "no_bridge_in_context",
            "message": "ConduitBridge not provided by runtime",
        }
    if action not in SUPPORTED_ACTIONS:
        return {"ok": False, "error": "unsupported_action", "action": action}
    try:
        # Lazy browser launch: Chromium starts only when a browser action is
        # actually requested, so file/shell/search-only jobs use no browser RAM.
        ensure = getattr(_bridge, "ensure_started", None)
        if ensure is not None:
            await ensure()
        args = {"action": action, **kwargs}
        result_str = await _bridge.execute(args)
        if isinstance(result_str, str):
            try:
                parsed = json.loads(result_str)
                return parsed if isinstance(parsed, dict) else {"ok": True, "result": parsed}
            except json.JSONDecodeError:
                return {"ok": True, "result": result_str}
        return {"ok": True, "result": result_str}
    except Exception as e:
        log.exception("conduit action %s failed", action)
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


CONDUIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "conduit",
        "description": (
            "Restricted browser navigation, search, extraction, screenshots, clicking, "
            "text entry, and accessibility snapshots. Private/local URLs and arbitrary "
            "JavaScript execution are denied."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": sorted(SUPPORTED_ACTIONS),
                    "description": "Action to perform.",
                },
            },
            "required": ["action"],
            "additionalProperties": True,
        },
    },
}


def register() -> None:
    register_tool("conduit", conduit_call, CONDUIT_SCHEMA)
