"""conduit_browser — minimal ConduitBridge shim using patchright.

Provides `from conduit_browser import ConduitBridge`. The upstream
conduit-browser PyPI package (v0.2.1) installs files into site-packages/tools/
with broken relative imports that prevent any import from working. This shim
wraps patchright directly and implements the same interface used by
agent_runtime.py and tools/conduit_tool.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Memory-lean Chromium flags. On a memory-constrained instance, leaking or
# over-provisioning Chromium is the #1 OOM cause. --single-process collapses
# the renderer into one process (big RAM saving); opt out with
# GENESIS_BROWSER_SINGLE_PROCESS=false if a site needs the multi-process model.
def _chromium_args() -> list[str]:
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-features=site-per-process,TranslateUI",
        "--js-flags=--max-old-space-size=256",
        "--no-zygote",
        "--mute-audio",
    ]
    if os.getenv("GENESIS_BROWSER_SINGLE_PROCESS", "true").lower() in {"1", "true", "yes"}:
        args.append("--single-process")
    return args


class ConduitBridge:
    """Minimal browser automation bridge backed by patchright/Chromium.

    Chromium is started LAZILY (on first execute / ensure_started) so jobs that
    never browse pay no browser-memory cost. start()/stop() are idempotent.
    """

    def __init__(
        self,
        session_id: str,
        budget_cents: int = 200,
        data_dir: Path | str | None = None,
    ) -> None:
        self.session_id = session_id
        self.budget_cents = budget_cents
        self.data_dir = Path(data_dir) if data_dir else Path(f"/tmp/conduit/{session_id}")
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None

    def is_started(self) -> bool:
        return self._page is not None

    async def start(self) -> None:
        if self._page is not None:
            return  # idempotent — never launch a second Chromium
        from patchright.async_api import async_playwright  # type: ignore[import]

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=_chromium_args(),
        )
        self._page = await self._browser.new_page()
        log.info("ConduitBridge started session=%s", self.session_id)

    async def ensure_started(self) -> None:
        """Start Chromium on demand (first browser action)."""
        if self._page is None:
            await self.start()

    async def close(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._browser = None
        self._playwright = None
        self._page = None

    # agent_runtime's finally calls bridge.stop(); alias to close() so Chromium
    # is actually torn down (previously stop() did not exist → leaked Chromium).
    async def stop(self) -> None:
        await self.close()

    async def execute(self, args: dict[str, Any]) -> str:
        action = args.get("action", "")
        if self._page is None:
            return json.dumps({"ok": False, "error": "bridge_not_started"})
        try:
            if action == "navigate":
                url = args.get("url", "")
                await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
                return json.dumps({
                    "ok": True,
                    "url": self._page.url,
                    "title": await self._page.title(),
                })
            elif action == "screenshot":
                path = str(self.data_dir / "screenshot.png")
                await self._page.screenshot(path=path)
                return json.dumps({"ok": True, "path": path})
            elif action == "extract_main":
                text = await self._page.inner_text("body")
                return json.dumps({"ok": True, "text": text[:8000]})
            elif action == "eval":
                code = args.get("code", "")
                result = await self._page.evaluate(code)
                return json.dumps({"ok": True, "result": result})
            elif action == "click":
                selector = args.get("selector", "")
                await self._page.click(selector, timeout=10000)
                return json.dumps({"ok": True})
            elif action == "type_text":
                selector = args.get("selector", "")
                text = args.get("text", "")
                await self._page.fill(selector, text)
                return json.dumps({"ok": True})
            elif action == "accessibility_snapshot":
                snapshot = await self._page.accessibility.snapshot()
                return json.dumps({"ok": True, "snapshot": snapshot})
            elif action == "web_search":
                query = args.get("query", "")
                import urllib.parse
                await self._page.goto(
                    f"https://duckduckgo.com/?q={urllib.parse.quote(query)}",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                return json.dumps({"ok": True, "url": self._page.url, "title": await self._page.title()})
            else:
                return json.dumps({"ok": False, "error": f"unsupported_action:{action}"})
        except Exception as e:
            log.exception("ConduitBridge.execute action=%s failed", action)
            return json.dumps({"ok": False, "error": type(e).__name__, "message": str(e)})
