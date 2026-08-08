"""Centralized workspace lifecycle manager for Genesis agent jobs.

Sandbox states: CREATED → ACTIVE → FINALIZING → UPLOADED → CLEANED → FAILED
"""
from __future__ import annotations
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

WORKSPACE_ROOT = os.getenv("GENESIS_WORKSPACE_ROOT", "/tmp/jobs")
RETAIN_FAILED_HOURS = float(os.getenv("GENESIS_WORKSPACE_RETAIN_FAILED_HOURS", "24"))
RETAIN_SUCCESS_HOURS = float(os.getenv("GENESIS_WORKSPACE_RETAIN_SUCCESS_HOURS", "1"))
CLEANUP_ENABLED = os.getenv("GENESIS_WORKSPACE_CLEANUP_ENABLED", "true").lower() in {
    "1", "true", "yes"
}

VALID_STATES = frozenset(
    {"CREATED", "ACTIVE", "FINALIZING", "UPLOADED", "CLEANED", "FAILED"}
)

_SEP = os.sep  # "/" on Linux, "\" on Windows


@dataclass
class Workspace:
    job_id: str
    session_id: str
    path: Path
    status: str = "CREATED"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "session_id": self.session_id,
            "path": str(self.path),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_registry: dict[str, Workspace] = {}


def create_workspace(job_id: str, session_id: Optional[str] = None) -> Workspace:
    """Create and register a workspace for job_id. Idempotent — returns existing if already registered."""
    if job_id in _registry:
        return _registry[job_id]
    if session_id is None:
        session_id = str(uuid.uuid4())
    path = Path(WORKSPACE_ROOT) / job_id
    path.mkdir(parents=True, exist_ok=True)
    ws = Workspace(job_id=job_id, session_id=session_id, path=path, status="CREATED")
    _registry[job_id] = ws
    return ws


def get_workspace(job_id: str) -> Optional[Workspace]:
    """Return the Workspace for job_id, or None if not registered."""
    return _registry.get(job_id)


def set_workspace_status(job_id: str, status: str) -> None:
    """Transition sandbox status. Raises ValueError for unknown states."""
    if status not in VALID_STATES:
        raise ValueError(f"Invalid workspace status: {status!r}. Valid: {VALID_STATES}")
    ws = _registry.get(job_id)
    if ws is not None:
        ws.status = status
        ws.updated_at = time.time()


def assert_inside_workspace(job_id: str, path: str | Path) -> Path:
    """Resolve path and verify it is inside the job workspace.

    Raises PermissionError if the resolved path escapes the workspace root.
    This is a code-level guard — not a kernel/container boundary.
    """
    ws = _registry.get(job_id)
    if ws is None:
        raise ValueError(f"No workspace registered for job_id={job_id!r}")
    resolved = Path(path).resolve()
    workspace_resolved = ws.path.resolve()
    workspace_str = str(workspace_resolved)
    resolved_str = str(resolved)
    # Must be an exact match or a sub-path (with separator to prevent prefix collisions)
    if resolved_str != workspace_str and not resolved_str.startswith(
        workspace_str + _SEP
    ):
        raise PermissionError(
            f"Path escape: {path!r} → {resolved_str!r} is outside workspace "
            f"{workspace_str!r}"
        )
    return resolved


def cleanup_workspace(job_id: str) -> dict:
    """Remove workspace directory and mark as CLEANED.

    Respects GENESIS_WORKSPACE_CLEANUP_ENABLED env var.
    """
    ws = _registry.get(job_id)
    if ws is None:
        return {"ok": False, "error": "workspace_not_found", "job_id": job_id}
    if not CLEANUP_ENABLED:
        return {"ok": True, "skipped": True, "reason": "cleanup_disabled", "job_id": job_id}
    try:
        if ws.path.exists():
            shutil.rmtree(ws.path, ignore_errors=True)
        ws.status = "CLEANED"
        ws.updated_at = time.time()
        return {"ok": True, "job_id": job_id, "path": str(ws.path)}
    except Exception as exc:
        ws.status = "FAILED"
        ws.updated_at = time.time()
        return {"ok": False, "error": str(exc), "job_id": job_id}
