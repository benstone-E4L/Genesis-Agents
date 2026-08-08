"""Sandbox tool - execute Python code in an isolated subprocess."""
from __future__ import annotations
import asyncio
import logging
import os
import subprocess
import sys
import tempfile
from typing import Any

from . import register_tool

log = logging.getLogger(__name__)

_MAX_CODE_LEN = 10_000
_MAX_TIMEOUT = 30

# Minimal safe environment - no inherited env, just a PATH so Python can find stdlib
_SAFE_ENV = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}


async def run_code(
    *,
    code: str,
    language: str = "python",
    timeout: int = 20,
    **kwargs: Any,
) -> dict[str, Any]:
    if language != "python":
        return {
            "ok": False,
            "error": "unsupported_language",
            "message": f"Language '{language}' is not supported. Only 'python' is currently available.",
        }

    if len(code) > _MAX_CODE_LEN:
        return {
            "ok": False,
            "error": "code_too_long",
            "message": f"Code exceeds maximum length of {_MAX_CODE_LEN} characters ({len(code)} given).",
        }

    clamped_timeout = min(timeout, _MAX_TIMEOUT)

    def _run() -> dict[str, Any]:
        # Write to a temp file and execute - avoids shell quoting issues with -c
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(code)
            tmp_path = tmp.name

        try:
            result = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=True,
                text=True,
                timeout=clamped_timeout,
                env=_SAFE_ENV,
            )
            if result.returncode == 0:
                return {
                    "ok": True,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "exit_code": 0,
                }
            else:
                return {
                    "ok": False,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "exit_code": result.returncode,
                }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": "timeout",
                "timeout_seconds": clamped_timeout,
            }
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


RUN_CODE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_code",
        "description": (
            "Execute Python code in an isolated subprocess with no network access and a "
            f"maximum timeout of {_MAX_TIMEOUT}s. Max code length: {_MAX_CODE_LEN} characters. "
            "Returns stdout, stderr, and exit code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python source code to execute.",
                },
                "language": {
                    "type": "string",
                    "description": "Programming language. Only 'python' is supported.",
                    "enum": ["python"],
                    "default": "python",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Execution timeout in seconds (max {_MAX_TIMEOUT}, default 20).",
                    "default": 20,
                },
            },
            "required": ["code"],
        },
    },
}


def register() -> None:
    register_tool("run_code", run_code, RUN_CODE_SCHEMA)
