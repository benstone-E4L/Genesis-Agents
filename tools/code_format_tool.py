"""Code formatter tool - black for Python, prettier for JS/TS/JSON/YAML."""
from __future__ import annotations
import asyncio
import logging
import shutil
from typing import Any

from . import register_tool

log = logging.getLogger(__name__)


_PRETTIER_PARSERS = {
    "javascript": "babel",
    "typescript": "typescript",
    "json": "json",
    "yaml": "yaml",
}


async def code_format(*, content: str, language: str = "python", **kwargs: Any) -> dict[str, Any]:
    if language == "python":
        if not shutil.which("black"):
            return {"ok": False, "error": "black_not_installed", "content": content}
        proc = await asyncio.create_subprocess_exec(
            "black", "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(content.encode("utf-8"))
        if proc.returncode != 0:
            return {
                "ok": False,
                "error": "format_failed",
                "stderr": err.decode("utf-8")[:500],
                "content": content,
            }
        return {"ok": True, "content": out.decode("utf-8")}
    elif language in _PRETTIER_PARSERS:
        if not shutil.which("prettier"):
            return {"ok": False, "error": "prettier_not_installed", "content": content}
        proc = await asyncio.create_subprocess_exec(
            "prettier", "--parser", _PRETTIER_PARSERS[language],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(content.encode("utf-8"))
        if proc.returncode != 0:
            return {
                "ok": False,
                "error": "format_failed",
                "stderr": err.decode("utf-8")[:500],
                "content": content,
            }
        return {"ok": True, "content": out.decode("utf-8")}
    return {"ok": False, "error": "unsupported_language", "language": language}


CODE_FORMAT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "code_format",
        "description": "Format code via black or prettier. Returns formatted content or the original on error.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "language": {
                    "type": "string",
                    "enum": ["python", "javascript", "typescript", "json", "yaml"],
                },
            },
            "required": ["content"],
        },
    },
}


def register() -> None:
    register_tool("code_format", code_format, CODE_FORMAT_SCHEMA)
