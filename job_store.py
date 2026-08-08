"""Job store — Postgres-backed durable job tracking.

Inserts new jobs, updates status, appends events. Read by the worker
and by the gateway's polling endpoint. Database URL from DATABASE_URL env var.
"""
from __future__ import annotations
import json, logging, os, uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

UNSUPPORTED_PSYCOPG_QUERY_PARAMS = {"connection_limit", "pgbouncer"}


def _database_url() -> str:
    """Return a psycopg-compatible Postgres URL for Genesis job storage.

    Supabase pooled URLs commonly include Prisma-specific query params such as
    `pgbouncer=true`. psycopg rejects unknown query params, so strip only the
    options known to be client-incompatible while preserving SSL and other
    connection settings.
    """
    raw = (
        os.getenv("GENESIS_JOB_DATABASE_URL")
        or os.getenv("DIRECT_URL")
        or os.getenv("DATABASE_URL")
        or ""
    )
    if not raw:
        return ""

    parts = urlsplit(raw)
    if not parts.query:
        return raw

    filtered = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in UNSUPPORTED_PSYCOPG_QUERY_PARAMS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(filtered), parts.fragment))


def _conn():
    db_url = _database_url()
    if not db_url:
        raise RuntimeError("DATABASE_URL not configured")
    # connect_timeout bounds how long a blocking connect can stall the caller —
    # without it, pooler saturation could hang the worker's event loop
    # indefinitely (manifesting as stale-heartbeat job expiry).
    return psycopg.connect(
        db_url,
        row_factory=dict_row,
        prepare_threshold=None,
        connect_timeout=int(os.getenv("GENESIS_DB_CONNECT_TIMEOUT_S", "10")),
    )


def _gen_id() -> str:
    # cuid-like (timestamp + random). Prisma uses cuid; we approximate.
    return "c" + uuid.uuid4().hex[:24]


def create_job(
    *,
    agent_slug: str,
    prompt: str,
    params: dict | None = None,
    buyer_wallet_id: str | None = None,
    buyer_client_id: str | None = None,
    price_tier_cents: int | None = None,
    idempotency_key: str | None = None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    escrow_id: str | None = None,
) -> dict[str, Any]:
    job_id = _gen_id()
    with _conn() as conn, conn.cursor() as cur:
        # idempotency check
        if idempotency_key:
            cur.execute(
                "SELECT id, status FROM genesis_jobs WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            existing = cur.fetchone()
            if existing:
                return {"id": existing["id"], "status": existing["status"], "idempotent_hit": True}
        cur.execute(
            """
            INSERT INTO genesis_jobs
              (id, "agentSlug", "buyerWalletId", "buyerClientId", prompt, params,
               status, "priceTierCents", "idempotencyKey", "webhookUrl",
               "webhookSecret", "escrowId", "outputArtifactUris",
               "createdAt", "updatedAt")
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, 'QUEUED', %s, %s, %s, %s, %s, '{}', NOW(), NOW())
            RETURNING id, status, "createdAt"
            """,
            (
                job_id, agent_slug, buyer_wallet_id, buyer_client_id, prompt,
                json.dumps(params or {}), price_tier_cents, idempotency_key,
                webhook_url, webhook_secret, escrow_id,
            ),
        )
        row = cur.fetchone()
        cur.execute(
            """
            INSERT INTO genesis_job_events
              (id, "jobId", "eventType", "toStatus", "createdAt")
            VALUES (%s, %s, 'status_change', 'QUEUED', NOW())
            """,
            (_gen_id(), job_id),
        )
        conn.commit()
        return {"id": row["id"], "status": row["status"], "created_at": row["createdAt"].isoformat(), "idempotent_hit": False}


def create_child_job(
    *,
    child_job_id: str,
    agent_slug: str,
    prompt: str,
    parent_job_id: str,
    params: dict | None = None,
) -> str:
    """Insert a first-class child job for genesis_call delegation.

    Children execute INLINE inside the parent's runtime (not via the QUEUED
    worker), so the row is created directly in RUNNING state — this prevents the
    auto-worker (which only claims QUEUED) from re-running it. The caller
    finalizes status via update_job_status() once the child returns. Idempotent
    on child_job_id.
    """
    merged_params = dict(params or {})
    merged_params["_parent_job_id"] = parent_job_id
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO genesis_jobs
              (id, "agentSlug", prompt, params, status, "outputArtifactUris",
               "startedAt", "createdAt", "updatedAt")
            VALUES (%s, %s, %s, %s::jsonb, 'RUNNING', '{}', NOW(), NOW(), NOW())
            ON CONFLICT (id) DO NOTHING
            """,
            (child_job_id, agent_slug, prompt, json.dumps(merged_params)),
        )
        cur.execute(
            """
            INSERT INTO genesis_job_events
              (id, "jobId", "eventType", "toStatus", "createdAt")
            VALUES (%s, %s, 'status_change', 'RUNNING', NOW())
            """,
            (_gen_id(), child_job_id),
        )
        conn.commit()
    return child_job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM genesis_jobs WHERE id = %s""",
            (job_id,),
        )
        return cur.fetchone()


def update_job_status(
    job_id: str,
    new_status: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
    result_summary: str | None = None,
    output_artifact_uris: list[str] | None = None,
) -> bool:
    set_clauses = ['status = %s', '"updatedAt" = NOW()']
    params: list = [new_status]
    if new_status == "RUNNING":
        set_clauses.append('"startedAt" = COALESCE("startedAt", NOW())')
    if new_status in ("DELIVERED", "FAILED", "SETTLED", "REFUNDED", "EXPIRED"):
        set_clauses.append('"completedAt" = NOW()')
    if error_code is not None:
        set_clauses.append('"errorCode" = %s')
        params.append(error_code)
    if error_message is not None:
        set_clauses.append('"errorMessage" = %s')
        params.append(error_message)
    if result_summary is not None:
        set_clauses.append('"resultSummary" = %s')
        params.append(result_summary)
    if output_artifact_uris is not None:
        set_clauses.append('"outputArtifactUris" = %s')
        params.append(output_artifact_uris)
    params.append(job_id)

    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            f'SELECT status FROM genesis_jobs WHERE id = %s',
            (job_id,),
        )
        row = cur.fetchone()
        if not row:
            return False
        from_status = row["status"]
        cur.execute(
            f'UPDATE genesis_jobs SET {", ".join(set_clauses)} WHERE id = %s',
            params,
        )
        cur.execute(
            """
            INSERT INTO genesis_job_events
              (id, "jobId", "eventType", "fromStatus", "toStatus", "createdAt")
            VALUES (%s, %s, 'status_change', %s, %s, NOW())
            """,
            (_gen_id(), job_id, from_status, new_status),
        )
        conn.commit()
        return True


def heartbeat(job_id: str) -> bool:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            'UPDATE genesis_jobs SET "lastHeartbeatAt" = NOW() WHERE id = %s',
            (job_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def claim_job_by_id(job_id: str) -> dict[str, Any] | None:
    """Atomically claim one QUEUED job by id."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE genesis_jobs
            SET status = 'RUNNING',
                "startedAt" = COALESCE("startedAt", NOW()),
                "updatedAt" = NOW()
            WHERE id = %s AND status = 'QUEUED'
            RETURNING *
            """,
            (job_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(
            """
            INSERT INTO genesis_job_events
              (id, "jobId", "eventType", "fromStatus", "toStatus", "createdAt")
            VALUES (%s, %s, 'status_change', 'QUEUED', 'RUNNING', NOW())
            """,
            (_gen_id(), job_id),
        )
        conn.commit()
        return row


def claim_queued_jobs(limit: int = 5) -> list[dict[str, Any]]:
    """Atomically claim QUEUED jobs by transitioning them to RUNNING."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH claimed AS (
              SELECT id FROM genesis_jobs
              WHERE status = 'QUEUED'
              ORDER BY "createdAt" ASC
              LIMIT %s
              FOR UPDATE SKIP LOCKED
            )
            UPDATE genesis_jobs
            SET status = 'RUNNING',
                "startedAt" = COALESCE("startedAt", NOW()),
                "updatedAt" = NOW()
            WHERE id IN (SELECT id FROM claimed)
            RETURNING *
            """,
            (limit,),
        )
        rows = cur.fetchall()
        # Append events
        for r in rows:
            cur.execute(
                """
                INSERT INTO genesis_job_events
                  (id, "jobId", "eventType", "fromStatus", "toStatus", "createdAt")
                VALUES (%s, %s, 'status_change', 'QUEUED', 'RUNNING', NOW())
                """,
                (_gen_id(), r["id"]),
            )
        conn.commit()
        return rows


def expire_stale_running_jobs(stale_minutes: int = 5) -> int:
    """Mark RUNNING jobs without heartbeat in N minutes as EXPIRED."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE genesis_jobs
            SET status = 'EXPIRED',
                "completedAt" = NOW(),
                "updatedAt" = NOW(),
                "errorCode" = 'stale_heartbeat',
                "errorMessage" = 'No heartbeat for {stale_minutes}+ minutes'
            WHERE status = 'RUNNING'
              AND ("lastHeartbeatAt" IS NULL OR "lastHeartbeatAt" < NOW() - INTERVAL '{stale_minutes} minutes')
            RETURNING id
            """,
        )
        expired_ids = [r["id"] for r in cur.fetchall()]
        for jid in expired_ids:
            cur.execute(
                """
                INSERT INTO genesis_job_events
                  (id, "jobId", "eventType", "toStatus", "createdAt")
                VALUES (%s, %s, 'status_change', 'EXPIRED', NOW())
                """,
                (_gen_id(), jid),
            )
        conn.commit()
        return len(expired_ids)
