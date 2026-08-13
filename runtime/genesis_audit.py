"""Sole production import path for the Genesis tamper-evident audit chain."""
from __future__ import annotations

import threading
import os
from pathlib import Path
from typing import Any

from audit import AuditLog
from runtime import pg_store

_lock = threading.Lock()
_instance: AuditLog | None = None


def get_audit_log(db_path: Path | None = None) -> AuditLog:
    global _instance
    if db_path is not None:
        log = AuditLog(db_path)
        log.connect()
        return log
    # Postgres needs no path at all — AuditLog resolves the backend itself. The
    # env var is only required when this process is still on the SQLite backend,
    # which is local dev and the test suite. This is what stops
    # GENESIS_AUDIT_DB_PATH from being a production requirement.
    if pg_store.postgres_selected():
        with _lock:
            if _instance is None:
                _instance = AuditLog()
                _instance.connect()
            return _instance
    configured = (os.getenv("GENESIS_AUDIT_DB_PATH") or "").strip()
    if not configured:
        raise RuntimeError(
            "GENESIS_AUDIT_DB_PATH is not configured (or configure "
            "GENESIS_JOB_DATABASE_URL to use the Postgres audit chain)"
        )
    with _lock:
        if _instance is None:
            _instance = AuditLog(Path(configured))
            _instance.connect()
        return _instance


def reset_for_tests() -> None:
    """Drop the cached AuditLog so a test can switch backends."""
    global _instance
    with _lock:
        if _instance is not None:
            try:
                _instance.close()
            except Exception:
                pass
        _instance = None


def append_tool_event(
    *, session_id: str, tool_name: str, inputs: dict[str, Any], outputs: dict[str, Any]
) -> int:
    return get_audit_log().log(
        session_id=session_id,
        action_type="tool_call",
        tool_name=tool_name,
        inputs=inputs,
        outputs=outputs,
        error="" if outputs.get("ok") else str(outputs.get("error", "tool_failed")),
    )


def append_tool_intent(*, session_id: str, tool_name: str, inputs: dict[str, Any]) -> int:
    """Durably record authorization intent before a side-effecting handler runs."""
    return get_audit_log().log(
        session_id=session_id,
        action_type="tool_intent",
        tool_name=tool_name,
        inputs=inputs,
        outputs={"ok": False, "status": "authorized_pending_execution"},
    )
