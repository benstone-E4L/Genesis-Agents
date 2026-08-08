"""Conduit tool - unified web/browser/search/extraction tool backed by conduit-browser."""
from __future__ import annotations
import json
import logging
from typing import Any

from . import register_tool

log = logging.getLogger(__name__)

try:
    from conduit_browser import ConduitBridge  # PyPI package
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
                return {"ok": True, "result": json.loads(result_str)}
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
            "Audited browser automation, multi-engine web search, structured extraction, "
            "screenshots, JS execution, form filling, marketplace adapters (LinkedIn, "
            "Amazon, GitHub, HackerNews, Reddit), YouTube transcripts, and Ed25519-signed "
            "proof bundles. Pass an action name and action-specific args."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "Action to perform. Common: navigate, click, type_text, "
                        "web_search, extract_main, screenshot, eval, scroll, "
                        "marketplace_plan, marketplace_execute_job, youtube_transcript, "
                        "accessibility_snapshot"
                    ),
                },
            },
            "required": ["action"],
            "additionalProperties": True,
        },
    },
}


def register() -> None:
    register_tool("conduit", conduit_call, CONDUIT_SCHEMA)
