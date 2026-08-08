"""AI vision agent tools - real implementations using GPT-4o vision API."""
from __future__ import annotations
import logging
import os
from typing import Any

import httpx

from . import register_tool

log = logging.getLogger(__name__)

_OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
_MODEL = "gpt-4o"


def _get_api_key() -> str | None:
    return os.environ.get("OPENAI_API_KEY")


async def _call_gpt4o(messages: list[dict[str, Any]]) -> str:
    api_key = _get_api_key()
    if not api_key:
        raise ValueError("OPENAI_API_KEY not set — check the per-tool guard before calling _call_gpt4o")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": _MODEL,
        "messages": messages,
        "max_tokens": 1024,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_OPENAI_CHAT_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def vision_analyze(
    *,
    image_url: str,
    question: str = "Describe what you see in this image in detail.",
    **kwargs: Any,
) -> dict[str, Any]:
    if not _get_api_key():
        return {
            "ok": False,
            "error": "missing_env: OPENAI_API_KEY",
            "hint": "set OPENAI_API_KEY to enable vision tools",
        }
    try:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": question},
                ],
            }
        ]
        analysis = await _call_gpt4o(messages)
        return {"ok": True, "analysis": analysis, "image_url": image_url, "model": _MODEL}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


async def vision_ocr(*, image_url: str, **kwargs: Any) -> dict[str, Any]:
    if not _get_api_key():
        return {
            "ok": False,
            "error": "missing_env: OPENAI_API_KEY",
            "hint": "set OPENAI_API_KEY to enable vision tools",
        }
    try:
        prompt = (
            "Extract ALL text visible in this image exactly as it appears. "
            "Preserve formatting, line breaks, and structure. "
            "Return only the extracted text with no commentary."
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        text = await _call_gpt4o(messages)
        return {"ok": True, "text": text, "image_url": image_url, "model": _MODEL}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


async def vision_compare(
    *,
    image_url_a: str,
    image_url_b: str,
    focus: str = "differences",
    **kwargs: Any,
) -> dict[str, Any]:
    if not _get_api_key():
        return {
            "ok": False,
            "error": "missing_env: OPENAI_API_KEY",
            "hint": "set OPENAI_API_KEY to enable vision tools",
        }
    try:
        prompt = (
            f"Compare these two images. Focus on: {focus}. "
            "List all notable differences and similarities."
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url_a}},
                    {"type": "image_url", "image_url": {"url": image_url_b}},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        comparison = await _call_gpt4o(messages)
        return {
            "ok": True,
            "comparison": comparison,
            "image_url_a": image_url_a,
            "image_url_b": image_url_b,
            "model": _MODEL,
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


VISION_ANALYZE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "vision_analyze",
        "description": "Answer a question about an image using GPT-4o vision.",
        "parameters": {
            "type": "object",
            "properties": {
                "image_url": {
                    "type": "string",
                    "description": "URL of the image to analyze.",
                },
                "question": {
                    "type": "string",
                    "description": "Natural-language question about the image. Defaults to a general description prompt.",
                },
            },
            "required": ["image_url"],
        },
    },
}

VISION_OCR_SCHEMA = {
    "type": "function",
    "function": {
        "name": "vision_ocr",
        "description": "Extract all text from an image using GPT-4o vision OCR.",
        "parameters": {
            "type": "object",
            "properties": {
                "image_url": {
                    "type": "string",
                    "description": "URL of the image to extract text from.",
                },
            },
            "required": ["image_url"],
        },
    },
}

VISION_COMPARE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "vision_compare",
        "description": "Compare two images for similarities and differences using GPT-4o vision.",
        "parameters": {
            "type": "object",
            "properties": {
                "image_url_a": {
                    "type": "string",
                    "description": "URL of the first image.",
                },
                "image_url_b": {
                    "type": "string",
                    "description": "URL of the second image.",
                },
                "focus": {
                    "type": "string",
                    "description": "Aspect to focus on when comparing (e.g. 'differences', 'color palette', 'layout'). Defaults to 'differences'.",
                },
            },
            "required": ["image_url_a", "image_url_b"],
        },
    },
}


def register() -> None:
    register_tool("vision_analyze", vision_analyze, VISION_ANALYZE_SCHEMA)
    register_tool("vision_ocr", vision_ocr, VISION_OCR_SCHEMA)
    register_tool("vision_compare", vision_compare, VISION_COMPARE_SCHEMA)
