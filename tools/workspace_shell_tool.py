"""workspace_shell — controlled shell execution inside a job workspace.

Runs commands inside /tmp/jobs/{job_id}/workspace/ with strict constraints:
- cwd must be under _job_dir
- blocked dangerous command prefixes
- timeout enforced
- output capped at 64KB
- env vars scrubbed (no inherited secrets, no leaking AWS_*/GITHUB_* etc.)
"""
from __future__ import annotations
import asyncio
import logging
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from . import register_tool

log = logging.getLogger(__name__)

_MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB
_MAX_TIMEOUT_S = 60
_DEFAULT_TIMEOUT_S = 30

# Commands that are never allowed regardless of context
_BLOCKED_PREFIXES = (
    "rm -rf /",
    "rm -rf ~",
    "dd ",
    "mkfs",
    "fdisk",
    "shutdown",
    "reboot",
    "halt",
    "kill -9 1",
    "pkill",
    "killall",
    ":(){ :|:& };:",  # fork bomb
    "curl | bash",
    "wget | bash",
    "curl | sh",
    "wget | sh",
    "> /dev/sda",
    "chmod 777 /",
    "chown root",
)

# Regex: pipe-to-shell pattern. Catches `curl URL | bash` and similar where
# a URL or args appear between the pipe and the interpreter.
_PIPE_TO_SHELL_RE = re.compile(
    r'\|\s*(bash|sh|python3?|perl|ruby|node)\b',
    re.IGNORECASE,
)

# Regex: suspicious values that look like known API-key prefixes (for env_extra values)
_SECRET_VALUE_RE = re.compile(
    r'(sk_live|sk_test|AKIA[0-9A-Z]{16}|ghp_|glpat-|xoxp-|xoxb-)',
    re.IGNORECASE,
)

# Env var name patterns to redact from inherited environment
_REDACT_PATTERNS = re.compile(
    r"(token|secret|key|password|api_key|apikey|bearer|auth|private|credential|pwd)",
    re.IGNORECASE,
)

_SAFE_ENV_PASSTHROUGH = {"PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TERM"}


def _build_safe_env(base_dir: Path) -> dict[str, str]:
    """Build a minimal environment with no secret leakage."""
    safe: dict[str, str] = {}
    for key in _SAFE_ENV_PASSTHROUGH:
        val = os.environ.get(key)
        if val:
            safe[key] = val
    safe["HOME"] = str(base_dir)
    safe["TMPDIR"] = str(base_dir / "tmp")
    return safe


def _is_blocked(cmd: str) -> bool:
    stripped = cmd.strip().lower()
    for prefix in _BLOCKED_PREFIXES:
        if stripped.startswith(prefix.lower()) or prefix.lower() in stripped:
            return True
    # Catch `curl URL | bash` and similar where a URL appears between | and interpreter.
    if _PIPE_TO_SHELL_RE.search(stripped):
        return True
    return False


def _assert_within_workspace(cwd: Path, job_dir: Path) -> None:
    """Raise ValueError if cwd escapes the job workspace."""
    try:
        cwd.resolve().relative_to(job_dir.resolve())
    except ValueError:
        raise ValueError(
            f"cwd '{cwd}' is outside the job workspace '{job_dir}'. "
            "Commands must run within the job workspace."
        )


async def workspace_shell(
    *,
    command: str,
    cwd: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT_S,
    env_extra: dict[str, str] | None = None,
    _job_dir: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a shell command inside the job sandbox.

    Delegates to runtime.sandbox_manager.run_in_sandbox(), which provides real
    kernel isolation via bubblewrap when available (no network, no filesystem
    outside the workspace) and a hardened process sandbox otherwise. The
    response includes the active isolation tier.
    """
    if _job_dir is None:
        return {
            "ok": False,
            "error": "no_job_dir",
            "message": "workspace_shell requires a job context (_job_dir). Use this tool only within an active job.",
        }

    try:
        from runtime.sandbox_manager import run_in_sandbox
    except Exception as exc:  # noqa: BLE001
        log.exception("sandbox_manager unavailable")
        return {"ok": False, "error": "sandbox_unavailable", "message": str(exc)}

    job_id = _job_dir.name
    return await asyncio.to_thread(
        run_in_sandbox,
        job_id,
        command,
        timeout_s=timeout,
        env=env_extra,
        cwd=cwd,
        job_dir=_job_dir,
    )


WORKSPACE_SHELL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_shell",
        "description": (
            f"Run a shell command inside the job workspace (/tmp/jobs/{{job_id}}/workspace/). "
            f"Max timeout: {_MAX_TIMEOUT_S}s. Max output: 64KB. "
            "Commands that escape the workspace, delete system files, or expose secrets are blocked. "
            "Use for: running tests, lint, build commands, npm install, pytest, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute (bash syntax).",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory relative to workspace root (default: workspace root).",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Timeout in seconds (max {_MAX_TIMEOUT_S}, default {_DEFAULT_TIMEOUT_S}).",
                    "default": _DEFAULT_TIMEOUT_S,
                },
                "env_extra": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Additional non-secret environment variables to set.",
                },
            },
            "required": ["command"],
        },
    },
}


def register() -> None:
    register_tool("workspace_shell", workspace_shell, WORKSPACE_SHELL_SCHEMA)
