"""genesis_call - internal agent-to-agent dispatch for the meta orchestrator."""
from __future__ import annotations
import asyncio
import inspect
import logging
import uuid
from typing import Any

from . import register_tool
from bundle_loader import load_bundle
from runtime.tool_policy import DEFAULT_ALLOWED_RISKS, SLUG_ALLOWED_RISKS

log = logging.getLogger(__name__)

MAX_DELEGATION_DEPTH = 3
DELEGATION_TARGETS: dict[str, frozenset[str]] = {
    "genesis-meta": frozenset({
        "genesis-research", "genesis-finance", "genesis-pricing",
        "genesis-legal", "genesis-analyst", "genesis-content",
        "genesis-marketing", "genesis-seo", "genesis-support",
    }),
}

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


CHILD_HEARTBEAT_INTERVAL_S = 30.0


async def _child_heartbeat(child_job_id: str) -> None:
    """Keep a delegated child's heartbeat fresh while it runs.

    A child job is inserted directly as RUNNING and executes INLINE — nothing else ever
    heartbeats it. Only the parent's worker heartbeat loop runs, and it beats the PARENT's
    job id. So any delegated subtree that outlives the reaper's stale window was guaranteed
    to be marked EXPIRED / stale_heartbeat mid-flight, corrupting the delegation trace that
    GET /agents/jobs/{parent}/trace reconstructs.
    """
    if job_store is None:
        return
    while True:
        await asyncio.sleep(CHILD_HEARTBEAT_INTERVAL_S)
        try:
            job_store.heartbeat(child_job_id)
        except Exception:  # noqa: BLE001
            log.debug("child heartbeat failed for %s", child_job_id, exc_info=True)


async def genesis_call(
    *,
    agent: str,
    task: str,
    params: dict[str, Any] | None = None,
    _runtime: Any = None,
    _parent_job_id: str | None = None,
    _session_id: str | None = None,
    _parent_agent_slug: str | None = None,
    _delegation_chain: tuple[str, ...] = (),
    _delegation_depth: int = 0,
    _remaining_token_budget: int | None = None,
    _remaining_cost_budget_cents: int | None = None,
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

    target_bundle = load_bundle(agent)
    if target_bundle is None:
        return {"ok": False, "error": "delegation_target_not_allowed", "target_agent_slug": agent}
    canonical_target = target_bundle["slug"]
    if canonical_target not in DELEGATION_TARGETS.get(_parent_agent_slug or "", frozenset()):
        return {"ok": False, "error": "delegation_target_not_allowed", "target_agent_slug": agent}
    if _delegation_depth >= MAX_DELEGATION_DEPTH:
        return {"ok": False, "error": "delegation_depth_exceeded", "target_agent_slug": agent}
    if canonical_target in _delegation_chain or canonical_target == _parent_agent_slug:
        return {"ok": False, "error": "delegation_cycle_detected", "target_agent_slug": agent}
    parent_risks = SLUG_ALLOWED_RISKS.get(_parent_agent_slug or "", DEFAULT_ALLOWED_RISKS)

    child_job_id = f"child-{uuid.uuid4().hex[:12]}"
    child_session_id = str(uuid.uuid4())
    _persist_child_start(
        child_job_id=child_job_id, child_session_id=child_session_id, agent=agent,
        task=task, parent_job_id=_parent_job_id, parent_session_id=_session_id,
        parent_slug=_parent_agent_slug, params=params,
    )
    hb_task = asyncio.create_task(_child_heartbeat(child_job_id))
    try:
        result = await _runtime.execute_agent(
            canonical_target, task, params or {}, job_id=child_job_id, session_id=child_session_id,
            parent_job_id=_parent_job_id, parent_session_id=_session_id,
            delegation_chain=(*_delegation_chain, _parent_agent_slug or ""),
            delegation_depth=_delegation_depth + 1,
            delegated_allowed_risks=parent_risks,
            inherited_token_budget=_remaining_token_budget,
            inherited_cost_budget_cents=_remaining_cost_budget_cents,
        )
        child_ok = bool(result.get("ok"))
        # Charge the child's real usage back to the parent so the NEXT sibling inherits a
        # genuinely smaller ceiling. Best-effort: a runtime without the ledger (older or
        # stubbed) must not break delegation.
        try:
            usage = result.get("resource_usage") or {}
            charged = _runtime.record_delegated_spend(
                _parent_job_id, tokens=int(usage.get("total_tokens", 0) or 0)
            )
            if inspect.isawaitable(charged):  # tolerate AsyncMock/awaitable test doubles
                await charged
        except Exception:  # noqa: BLE001
            log.warning("could not record delegated spend for parent %s", _parent_job_id)
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
    finally:
        # Never leak the pump: an orphaned heartbeat would keep a finished child's row
        # looking alive to the reaper forever.
        hb_task.cancel()


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
