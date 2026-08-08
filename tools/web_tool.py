"""Web tools - search, fetch, and screenshot URLs."""
from __future__ import annotations
import base64
import logging
import os
import re
from typing import Any

import ipaddress
import socket as _socket

import httpx

from . import register_tool

log = logging.getLogger(__name__)

_USER_AGENT = "Mozilla/5.0 SwarmSync-Agent/1.0"


def _is_safe_url(url: str) -> bool:
    """Return False if the URL resolves to a private/loopback/link-local address."""
    try:
        from urllib.parse import urlparse
        hostname = urlparse(url).hostname
        if not hostname:
            return False
        # Block obvious literals first
        for blocked in ("localhost", "metadata.google.internal", "169.254.169.254"):
            if hostname.lower() == blocked:
                return False
        # Resolve and check all returned addresses
        addrs = _socket.getaddrinfo(hostname, None)
        for *_, sockaddr in addrs:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return True
    except Exception:
        return False  # On resolution failure, block by default


async def web_search(*, query: str, num_results: int = 5, **kwargs: Any) -> dict[str, Any]:
    try:
        serper_key = os.environ.get("SERPER_API_KEY")
        if serper_key:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
                    json={"q": query, "num": num_results},
                )
                resp.raise_for_status()
                data = resp.json()
                results = []
                for item in data.get("organic", [])[:num_results]:
                    results.append({
                        "title": item.get("title", ""),
                        "url": item.get("link", ""),
                        "snippet": item.get("snippet", ""),
                    })
                return {"ok": True, "results": results, "source": "serper"}

        # DuckDuckGo fallback
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            )
            html = resp.text

        titles = re.findall(r'<a class="result__a"[^>]*>(.*?)</a>', html, re.DOTALL)
        urls = re.findall(r'<a class="result__a"[^>]*href="([^"]+)"', html)
        snippets = re.findall(r'<a class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)

        def _strip(s: str) -> str:
            return re.sub(r"<[^>]+>", "", s).strip()

        results = []
        for i in range(min(num_results, len(titles))):
            results.append({
                "title": _strip(titles[i]) if i < len(titles) else "",
                "url": urls[i] if i < len(urls) else "",
                "snippet": _strip(snippets[i]) if i < len(snippets) else "",
            })

        return {"ok": True, "results": results, "source": "duckduckgo"}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


async def web_fetch(*, url: str, extract_text: bool = True, **kwargs: Any) -> dict[str, Any]:
    try:
        if not _is_safe_url(url):
            return {"ok": False, "error": "ssrf_blocked", "hint": "URL resolves to a private or reserved address"}
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
            raw = resp.text
            content_length = len(resp.content)

        if extract_text:
            text = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s{2,}", " ", text).strip()
            text = text[:8000]
        else:
            text = raw[:8000]

        return {
            "ok": True,
            "url": url,
            "status_code": resp.status_code,
            "text": text,
            "content_length": content_length,
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


async def screenshot_url(*, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        if not _is_safe_url(url):
            return {"ok": False, "error": "ssrf_blocked", "hint": "URL resolves to a private or reserved address"}
        from patchright.async_api import async_playwright  # type: ignore

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url, timeout=30000)
                png_bytes = await page.screenshot(type="png")
            finally:
                await browser.close()

        image_b64 = base64.b64encode(png_bytes).decode("utf-8")
        return {"ok": True, "url": url, "image_b64": image_b64, "format": "png"}
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "hint": "patchright/chromium may not be installed",
        }


WEB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for a query. Uses Serper API if SERPER_API_KEY is set, "
            "otherwise falls back to DuckDuckGo."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query string.",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results to return (default 5).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
}

WEB_FETCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": "Fetch a URL and return its text content (HTML stripped by default).",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL to fetch.",
                },
                "extract_text": {
                    "type": "boolean",
                    "description": "If true (default), strip HTML tags and return plain text.",
                    "default": True,
                },
            },
            "required": ["url"],
        },
    },
}

SCREENSHOT_URL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "screenshot_url",
        "description": "Take a PNG screenshot of a URL using a headless browser. Returns base64-encoded image.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to screenshot.",
                },
            },
            "required": ["url"],
        },
    },
}


def register() -> None:
    register_tool("web_search", web_search, WEB_SEARCH_SCHEMA)
    register_tool("web_fetch", web_fetch, WEB_FETCH_SCHEMA)
    register_tool("screenshot_url", screenshot_url, SCREENSHOT_URL_SCHEMA)
