"""Structured event emission for Genesis agent jobs.

Events are written to /tmp/jobs/{job_id}/logs/events.jsonl (one JSON object per line).
The GET /agents/jobs/{job_id}/events endpoint reads them back.
"""
from __future__ import annotations
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

WORKSPACE_ROOT = os.getenv("GENESIS_WORKSPACE_ROOT", "/tmp/jobs")

# Canonical event types
EVT_JOB_CREATED = "job.created"
EVT_AGENT_STARTED = "agent.started"
EVT_LLM_REQUESTED = "llm.requested"
EVT_LLM_RESPONDED = "llm.responded"
EVT_TOOL_CALLED = "tool.called"
EVT_TOOL_BLOCKED = "tool.blocked"
EVT_TOOL_RESULT = "tool.result"
EVT_SUBAGENT_DISPATCHED = "subagent.dispatched"
EVT_SUBAGENT_RETURNED = "subagent.returned"
EVT_JOB_COMPLETED = "job.completed"
EVT_JOB_FAILED = "job.failed"
EVT_SANDBOX_STATUS = "sandbox.status"


def _events_path(job_id: str) -> Path:
    return Path(WORKSPACE_ROOT) / job_id / "logs" / "events.jsonl"


def emit_event(
    job_id: str,
    event_type: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Append a structured event to the job's events.jsonl. Fire-and-forget — never raises."""
    event: dict[str, Any] = {
        "ts": time.time(),
        "job_id": job_id,
        "event_type": event_type,
    }
    if data:
        event.update(data)
    path = _events_path(job_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception:
        log.warning(
            "emit_event failed job=%s event=%s",
            job_id,
            event_type,
            exc_info=True,
        )

    # Durable mirror to Postgres (best-effort; never breaks the JSONL path).
    try:
        import durable_store
        durable_store.event_insert(
            job_id, event_type, data or {},
            session_id=(data or {}).get("session_id"),
        )
    except Exception:  # noqa: BLE001
        pass


def get_events(job_id: str) -> list[dict[str, Any]]:
    """Read all persisted events for a job.

    Prefers the durable Postgres store (survives restart / ephemeral disk);
    falls back to the local JSONL file when the DB is unavailable or empty.
    """
    try:
        import durable_store
        durable = durable_store.events_get(job_id)
        if durable:
            return durable
    except Exception:  # noqa: BLE001
        pass
    path = _events_path(job_id)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception:
        log.warning("get_events failed job=%s", job_id, exc_info=True)
    return events
