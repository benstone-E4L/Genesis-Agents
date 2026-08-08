"""Durable store — Postgres-backed sessions, events, job relationships, artifacts.

These tables (genesis_agent_sessions, genesis_agent_events,
genesis_job_relationships, genesis_artifacts) are owned by the SwarmSync.AI
Prisma schema and created by its migration `20260626000000_genesis_real_agent_runtime`.
Genesis writes/reads rows directly via psycopg, reusing job_store's connection.

CRITICAL: every WRITE is fire-and-forget on a single background thread. The
agent runtime and worker run on an asyncio event loop; doing blocking psycopg
writes inline would stall the loop and starve the per-job heartbeat coroutine
(observed as stale-heartbeat job expiry with zero events). Writes are submitted
to a 1-worker executor (ordered FIFO) so callers return immediately and the loop
keeps running. Reads are synchronous (used only by HTTP endpoints).

All operations are best-effort: if DATABASE_URL is unset, the tables do not yet
exist (pre-migration), or any DB error occurs, the call logs and degrades
silently so the runtime never breaks on a persistence failure.
"""
from __future__ import annotations
import concurrent.futures
import json
import logging
from typing import Any

from job_store import _conn, _database_url, _gen_id

log = logging.getLogger(__name__)

# Cache "tables missing" so we stop hammering the DB with doomed queries within
# a process once we learn the migration hasn't been applied yet.
_TABLES_MISSING = False

# Single-worker, ordered background executor for all writes. FIFO ordering means
# a session_create submitted before its session_finish runs first.
_WRITE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="durable-write"
)


def enabled() -> bool:
    return bool(_database_url()) and not _TABLES_MISSING


def _note_missing(exc: Exception) -> None:
    global _TABLES_MISSING
    msg = str(exc).lower()
    if "does not exist" in msg or "undefinedtable" in type(exc).__name__.lower():
        _TABLES_MISSING = True
        log.warning("durable_store: genesis_* runtime tables missing — "
                    "falling back to file/in-memory until migration is applied")


def _submit_write(fn) -> None:
    """Run a DB write on the background thread. Never blocks the caller/loop."""
    if not enabled():
        return
    try:
        _WRITE_EXECUTOR.submit(fn)
    except Exception:  # noqa: BLE001
        log.debug("durable write submit failed", exc_info=True)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def session_create(
    *,
    session_id: str,
    job_id: str,
    agent_slug: str,
    parent_job_id: str | None = None,
    parent_session_id: str | None = None,
    workspace_root: str | None = None,
    status: str = "ACTIVE",
) -> str:
    """Insert a durable session record (background). Idempotent on session_id."""
    def _do():
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO genesis_agent_sessions
                      (id, "jobId", "parentJobId", "parentSessionId", "agentSlug",
                       status, "workspaceRoot", "artifactUris", "startedAt",
                       "createdAt", "updatedAt")
                    VALUES (%s, %s, %s, %s, %s, %s, %s, '{}', NOW(), NOW(), NOW())
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (session_id, job_id, parent_job_id, parent_session_id,
                     agent_slug, status, workspace_root),
                )
                conn.commit()
        except Exception as e:  # noqa: BLE001
            _note_missing(e)
            log.debug("session_create failed job=%s", job_id, exc_info=True)
    _submit_write(_do)
    return session_id


def session_finish(
    session_id: str,
    *,
    status: str,
    trace: dict[str, Any] | None = None,
    artifact_uris: list[str] | None = None,
    error: str | None = None,
) -> None:
    def _do():
        set_clauses = ['status = %s', '"finishedAt" = NOW()', '"updatedAt" = NOW()']
        params: list[Any] = [status]
        if trace is not None:
            set_clauses.append('"traceJson" = %s::jsonb')
            params.append(json.dumps(trace, default=str))
        if artifact_uris is not None:
            set_clauses.append('"artifactUris" = %s')
            params.append(artifact_uris)
        if error is not None:
            set_clauses.append('error = %s')
            params.append(error)
        params.append(session_id)
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute(
                    f'UPDATE genesis_agent_sessions SET {", ".join(set_clauses)} WHERE id = %s',
                    params,
                )
                conn.commit()
        except Exception as e:  # noqa: BLE001
            _note_missing(e)
            log.debug("session_finish failed session=%s", session_id, exc_info=True)
    _submit_write(_do)


def session_get(session_id: str) -> dict[str, Any] | None:
    if not enabled():
        return None
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM genesis_agent_sessions WHERE id = %s", (session_id,)
            )
            return cur.fetchone()
    except Exception as e:  # noqa: BLE001
        _note_missing(e)
        log.warning("session_get failed session=%s", session_id, exc_info=True)
        return None


def sessions_by_job(job_id: str) -> list[dict[str, Any]]:
    if not enabled():
        return []
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                'SELECT * FROM genesis_agent_sessions WHERE "jobId" = %s ORDER BY "startedAt" ASC',
                (job_id,),
            )
            return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        _note_missing(e)
        return []


# ---------------------------------------------------------------------------
# Events (durable mirror of runtime/observability JSONL)
# ---------------------------------------------------------------------------

def event_insert(
    job_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    session_id: str | None = None,
) -> None:
    def _do():
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO genesis_agent_events
                      (id, "jobId", "sessionId", "eventType", payload, "createdAt")
                    VALUES (%s, %s, %s, %s, %s::jsonb, NOW())
                    """,
                    (_gen_id(), job_id, session_id, event_type,
                     json.dumps(payload or {}, default=str)),
                )
                conn.commit()
        except Exception as e:  # noqa: BLE001
            _note_missing(e)
            log.debug("event_insert failed job=%s event=%s", job_id, event_type, exc_info=True)
    _submit_write(_do)


def events_get(job_id: str) -> list[dict[str, Any]]:
    if not enabled():
        return []
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT "eventType" AS event_type, payload, "createdAt", "sessionId"
                FROM genesis_agent_events
                WHERE "jobId" = %s ORDER BY "createdAt" ASC, id ASC
                """,
                (job_id,),
            )
            rows = cur.fetchall()
            out = []
            for r in rows:
                ev = {"event_type": r["event_type"], "job_id": job_id,
                      "ts": r["createdAt"].isoformat() if r.get("createdAt") else None,
                      "session_id": r.get("sessionId")}
                if r.get("payload"):
                    ev.update(r["payload"])
                out.append(ev)
            return out
    except Exception as e:  # noqa: BLE001
        _note_missing(e)
        return []


# ---------------------------------------------------------------------------
# Job relationships (parent -> child delegation)
# ---------------------------------------------------------------------------

def relationship_create(
    *,
    parent_job_id: str,
    child_job_id: str,
    parent_session_id: str | None = None,
    child_session_id: str | None = None,
    parent_agent_slug: str | None = None,
    child_agent_slug: str | None = None,
    status: str = "DISPATCHED",
) -> None:
    def _do():
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO genesis_job_relationships
                      (id, "parentJobId", "childJobId", "parentSessionId", "childSessionId",
                       "parentAgentSlug", "childAgentSlug", "delegationStatus",
                       "createdAt", "updatedAt")
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    """,
                    (_gen_id(), parent_job_id, child_job_id, parent_session_id, child_session_id,
                     parent_agent_slug, child_agent_slug, status),
                )
                conn.commit()
        except Exception as e:  # noqa: BLE001
            _note_missing(e)
            log.debug("relationship_create failed parent=%s child=%s", parent_job_id, child_job_id, exc_info=True)
    _submit_write(_do)


def relationship_update(child_job_id: str, *, status: str) -> None:
    def _do():
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute(
                    'UPDATE genesis_job_relationships SET "delegationStatus" = %s, "updatedAt" = NOW() WHERE "childJobId" = %s',
                    (status, child_job_id),
                )
                conn.commit()
        except Exception as e:  # noqa: BLE001
            _note_missing(e)
            log.debug("relationship_update failed child=%s", child_job_id, exc_info=True)
    _submit_write(_do)


def relationships_by_parent(parent_job_id: str) -> list[dict[str, Any]]:
    if not enabled():
        return []
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                'SELECT * FROM genesis_job_relationships WHERE "parentJobId" = %s ORDER BY "createdAt" ASC',
                (parent_job_id,),
            )
            return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        _note_missing(e)
        return []


# ---------------------------------------------------------------------------
# Artifacts (per-file metadata)
# ---------------------------------------------------------------------------

def artifact_record(
    *,
    job_id: str,
    path: str,
    filename: str,
    session_id: str | None = None,
    agent_slug: str | None = None,
    mime_type: str | None = None,
    size_bytes: int | None = None,
    sha256: str | None = None,
    storage_backend: str | None = None,
    uri: str | None = None,
    signed_url: str | None = None,
) -> None:
    def _do():
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO genesis_artifacts
                      (id, "jobId", "sessionId", "agentSlug", path, filename, "mimeType",
                       "sizeBytes", sha256, "storageBackend", uri, "signedUrl", "createdAt")
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    """,
                    (_gen_id(), job_id, session_id, agent_slug, path, filename, mime_type,
                     size_bytes, sha256, storage_backend, uri, signed_url),
                )
                conn.commit()
        except Exception as e:  # noqa: BLE001
            _note_missing(e)
            log.debug("artifact_record failed job=%s file=%s", job_id, filename, exc_info=True)
    _submit_write(_do)


def artifacts_by_job(job_id: str) -> list[dict[str, Any]]:
    if not enabled():
        return []
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                'SELECT * FROM genesis_artifacts WHERE "jobId" = %s ORDER BY "createdAt" ASC',
                (job_id,),
            )
            return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        _note_missing(e)
        return []
