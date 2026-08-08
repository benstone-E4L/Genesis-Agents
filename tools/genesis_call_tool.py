"""genesis_call - internal agent-to-agent dispatch for the meta orchestrator."""
from __future__ import annotations
import logging
import uuid
from typing import Any

from . import register_tool

log = logging.getLogger(__name__)

# Durable persistence is best-effort; delegation must work even without a DB.
try:
    import job_store
except Exception:  # noqa: BLE001
    job_store = None  # type: ignore
try:
    import durable_store
except Exception:  # noqa: BLE001
    durable_store = None  # type: ignore


def _persist_child_start(*, child_job_id, child_session_id, agent, task,
                         parent_job_id, parent_session_id, parent_slug, params):
    """Create first-class child job + relationship rows. Best-effort."""
    if job_store is not None and parent_job_id:
        try:
            job_store.create_child_job(
                child_job_id=child_job_id, agent_slug=agent, prompt=task,
                parent_job_id=parent_job_id, params=params or {},
            )
        except Exception:  # noqa: BLE001
            log.debug("create_child_job failed", exc_info=True)
    if durable_store is not None and parent_job_id:
        try:
            durable_store.relationship_create(
                parent_job_id=parent_job_id, child_job_id=child_job_id,
                parent_session_id=parent_session_id, child_session_id=child_session_id,
                parent_agent_slug=parent_slug, child_agent_slug=agent,
                status="DISPATCHED",
            )
        except Exception:  # noqa: BLE001
            log.debug("relationship_create failed", exc_info=True)


def _persist_child_finish(*, child_job_id, child_ok):
    """Finalize child job + relationship status. Best-effort."""
    status = "DELIVERED" if child_ok else "FAILED"
    if job_store is not None:
        try:
            job_store.update_job_status(child_job_id, status)
        except Exception:  # noqa: BLE001
            log.debug("child update_job_status failed", exc_info=True)
    if durable_store is not None:
        try:
            durable_store.relationship_update(child_job_id, status="COMPLETED" if child_ok else "FAILED")
        except Exception:  # noqa: BLE001
            log.debug("relationship_update failed", exc_info=True)


async def genesis_call(
    *,
    agent: str,
    task: str,
    params: dict[str, Any] | None = None,
    _runtime: Any = None,
    _parent_job_id: str | None = None,
    _session_id: str | None = None,
    _parent_agent_slug: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Dispatch to another Genesis agent. _runtime is injected by the caller's agent_runtime instance.

    Child jobs are first-class: a genesis_jobs row (RUNNING), a
    genesis_agent_sessions row (via the child runtime), and a
    genesis_job_relationships row link the child to its parent durably, so
    GET /agents/jobs/{parent}/trace reconstructs the full tree after restart.
    """
    if _runtime is None:
        return {
            "ok": False,
            "error": "no_runtime_in_context",
            "target_agent_slug": agent,
        }

    child_job_id = f"child-{uuid.uuid4().hex[:12]}"
    child_session_id = str(uuid.uuid4())
    _persist_child_start(
        child_job_id=child_job_id, child_session_id=child_session_id, agent=agent,
        task=task, parent_job_id=_parent_job_id, parent_session_id=_session_id,
        parent_slug=_parent_agent_slug, params=params,
    )
    try:
        result = await _runtime.execute_agent(
            agent, task, params or {}, job_id=child_job_id, session_id=child_session_id,
            parent_job_id=_parent_job_id, parent_session_id=_session_id,
        )
        child_ok = bool(result.get("ok"))
        _persist_child_finish(child_job_id=child_job_id, child_ok=child_ok)
        child_response = str(result.get("response", ""))
        # child_session_id may also appear in the child trace if hardening is active
        resolved_child_session_id = (
            (result.get("trace") or {}).get("session_id") or child_session_id
        )
        return {
            "ok": True,
            "target_agent_slug": agent,
            "child_job_id": child_job_id,
            "child_session_id": resolved_child_session_id,
            "parent_session_id": _session_id,
            "child_ok": child_ok,
            "child_response_summary": child_response[:500],
            "child_trace": result.get("trace", {}),
            # backwards-compat aliases
            "agent": agent,
            "result": result,
        }
    except Exception as e:
        log.exception("genesis_call to %s failed", agent)
        _persist_child_finish(child_job_id=child_job_id, child_ok=False)
        return {
            "ok": False,
            "target_agent_slug": agent,
            "child_job_id": child_job_id,
            "child_session_id": child_session_id,
            "parent_session_id": _session_id,
            "error": type(e).__name__,
            "message": str(e),
            "agent": agent,
        }


GENESIS_CALL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "genesis_call",
        "description": "Dispatch a task to another Genesis agent by slug. Used by orchestrator agents to delegate specialist work.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Target agent slug, e.g. 'genesis-research', 'genesis-builder', 'genesis-qa'"},
                "task": {"type": "string", "description": "Plain-text task for that agent"},
                "params": {"type": "object", "additionalProperties": True, "description": "Optional extra parameters for the agent"},
            },
            "required": ["agent", "task"],
        },
    },
}


def register() -> None:
    register_tool("genesis_call", genesis_call, GENESIS_CALL_SCHEMA)
