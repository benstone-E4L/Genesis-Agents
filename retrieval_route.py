"""E4L Retrieval Contract route — read-only, vault-grounded hybrid retrieval against the
assistant-tier pgvector/pg_diskann index (master spec §9, Phase E, Genesis-side slice).

NON-NEGOTIABLE (per this workstream's guardrails): this route is a disposable, derived index.
The vault itself remains the only source of truth. Nothing in this module writes to the vault,
and nothing here may be treated by any caller as authoritative over a direct vault read — every
`chunk_id`/`citation` pair exists precisely so a human or agent can verify against the real
vault file, not to be trusted blindly.

This route never calls an LLM. It is retrieval-only; callers do their own synthesis. When
nothing scores above RETRIEVAL_MIN_SCORE, or the assistant-tier PG connection is unset or
unreachable, it returns a structured refusal object with HTTP 200 (never an error status) — a
refusal is a valid, expected answer shape here, not a failure.

Auth: mounted by main.py with `dependencies=[Depends(verify_gateway_key)]` (same GATEWAY_API_KEY
guard every other non-public /agents/* route uses). This module intentionally does not import
from main.py, to avoid a circular import (main.py imports this module to mount it).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel

import retrieval_store

logger = logging.getLogger(__name__)

router = APIRouter()

_REFUSAL_REASON = "no vault answer found above threshold"


def _min_score() -> float:
    """Read at call time (not import time) so an env-var rotation takes effect without a
    redeploy, matching this repo's existing env-read convention (e.g. main.py's _llm_api_key())."""
    return float(os.getenv("RETRIEVAL_MIN_SCORE", "0.35"))


def _staleness_threshold_hours() -> float:
    return float(os.getenv("RETRIEVAL_STALENESS_THRESHOLD_HOURS", "24"))


class RetrievalQueryRequest(BaseModel):
    query: str
    top_k: int = 8
    entity_filter: Optional[str] = None
    include_superseded: bool = False


class RetrievalChunk(BaseModel):
    chunk_id: str
    content_sha256: str
    citation: str
    entity: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None
    updated: Optional[str] = None
    supersedes: list[str] = []
    score: float


class RetrievalQueryResponse(BaseModel):
    chunks: list[RetrievalChunk]
    index_updated_at: Optional[str] = None
    stale: bool = False
    refusal: bool = False
    reason: Optional[str] = None
    degraded: bool = False
    degraded_reason: Optional[str] = None


def _isoformat(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _row_to_chunk(row: dict[str, Any]) -> RetrievalChunk:
    """Map one vault_chunks row to the contract's response shape.

    content_sha256 is trusted exactly as stored — this route never recomputes it from
    content_text. This is a deliberate, documented decision (CHUNK_5_TESTS locks it in): the
    route is a thin reader over a derived index, not the vault's integrity-verification layer;
    hash verification against the live vault file is the caller's job, not this route's, per
    the module docstring's "disposable, derived index" guardrail.
    """
    citation = f"{row['vault_path']}#{row['heading_path']}"
    return RetrievalChunk(
        chunk_id=row["chunk_id"],
        content_sha256=row["content_sha256"],
        citation=citation,
        entity=row.get("entity"),
        type=row.get("type"),
        status=row.get("status"),
        updated=_isoformat(row.get("updated")),
        supersedes=list(row.get("supersedes") or []),
        score=float(row.get("score", 0.0)),
    )


def _compute_staleness(index_updated_at: Any) -> tuple[Optional[str], bool]:
    if index_updated_at is None:
        return None, False
    now = datetime.now(timezone.utc)
    idx_dt = index_updated_at
    if getattr(idx_dt, "tzinfo", None) is None and hasattr(idx_dt, "replace"):
        idx_dt = idx_dt.replace(tzinfo=timezone.utc)
    age_hours = (now - idx_dt).total_seconds() / 3600.0
    return _isoformat(index_updated_at), age_hours > _staleness_threshold_hours()


def _degraded_response(reason: str) -> RetrievalQueryResponse:
    return RetrievalQueryResponse(
        chunks=[],
        index_updated_at=None,
        stale=False,
        refusal=True,
        reason=_REFUSAL_REASON,
        degraded=True,
        degraded_reason=reason,
    )


@router.post("/retrieval/query", response_model=RetrievalQueryResponse)
async def retrieval_query(body: RetrievalQueryRequest) -> RetrievalQueryResponse:
    try:
        rows = retrieval_store.query_chunks(
            body.query,
            top_k=body.top_k,
            entity_filter=body.entity_filter,
            include_superseded=body.include_superseded,
        )
        index_updated_at_raw = retrieval_store.index_freshness()
    except Exception as exc:
        # Connection unset/unreachable, or any other store-layer failure. Never a 500 — the
        # gateway must keep serving /health and /agents/* even when the assistant-tier PG
        # server is unavailable (job_store.py's DATABASE_URL-unset pattern, applied here).
        logger.warning("retrieval_query: assistant-tier PG unavailable: %s", exc)
        return _degraded_response("assistant-tier PG unavailable")

    index_updated_at_str, stale = _compute_staleness(index_updated_at_raw)

    min_score = _min_score()
    scored = [row for row in rows if float(row.get("score", 0.0)) >= min_score]

    if not scored:
        return RetrievalQueryResponse(
            chunks=[],
            index_updated_at=index_updated_at_str,
            stale=stale,
            refusal=True,
            reason=_REFUSAL_REASON,
        )

    return RetrievalQueryResponse(
        chunks=[_row_to_chunk(row) for row in scored],
        index_updated_at=index_updated_at_str,
        stale=stale,
        refusal=False,
    )
