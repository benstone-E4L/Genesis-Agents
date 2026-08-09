"""Assistant-tier PG (pgvector/pg_diskann) query layer for the E4L Retrieval Contract route.

Minimum column shape this module expects on the `vault_chunks` table. That table is
provisioned and populated by a different, external workstream (master spec §17 Phase E:
assistant-tier PG server + pgvector/pg_diskann population owned by e4l-work-os/Cato, not
Genesis) and does not exist yet — this is the contract the two workstreams integrate against,
not a migration this repo runs:

    chunk_id        text        canonical {vault-relative-path}#{heading-path}@{chunk-index}
    vault_path      text        vault-relative file path
    heading_path    text        heading anchor within the file
    chunk_index     int         ordinal position of this chunk within the heading section
    content_sha256  text        sha256 of content_text, for staleness/corruption detection
    content_text    text        the chunk's raw text
    embedding       vector      pgvector embedding of content_text
    entity          text        frontmatter `entity` field
    type            text        frontmatter `type` field
    status          text        frontmatter `status` field (active | superseded | ...)
    updated         date        frontmatter `updated` field
    supersedes      text[]      frontmatter `supersedes` field
    indexed_at      timestamptz when this row was last (re)indexed

Follows job_store.py's fail-closed, lazy-connection pattern: _database_url() returns "" when
unset, _conn() raises RuntimeError against an empty URL, and the caller (retrieval_route.py)
is responsible for turning that into a degraded-refusal HTTP 200 rather than letting it become
an unhandled 500 that takes the gateway down.
"""
from __future__ import annotations

import os
from typing import Any

import psycopg
from psycopg.rows import dict_row


def _database_url() -> str:
    """Read the assistant-tier PG connection string at call time (not import time), so a
    Render env-var rotation takes effect without a redeploy — matches job_store.py's pattern."""
    return (os.getenv("ASSISTANT_PG_DATABASE_URL") or "").strip()


def _conn():
    db_url = _database_url()
    if not db_url:
        raise RuntimeError("ASSISTANT_PG_DATABASE_URL not configured")
    return psycopg.connect(
        db_url,
        row_factory=dict_row,
        prepare_threshold=None,
        connect_timeout=int(os.getenv("GENESIS_DB_CONNECT_TIMEOUT_S", "10")),
    )


def query_chunks(
    query: str,
    *,
    top_k: int = 8,
    entity_filter: str | None = None,
    include_superseded: bool = False,
    query_embedding: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Hybrid lexical + vector retrieval against vault_chunks.

    Contract requirements this SQL enforces directly (not as a post-filter):
    - Metadata filters (status, entity) are applied in the WHERE clause BEFORE ranking — a
      filtered-out row can never out-rank an included one because it is never scored at all.
    - Superseded chunks (status='superseded') are excluded unless include_superseded=True.
    - Recency (updated) is used ONLY as a tiebreaker (secondary ORDER BY key), never as the
      primary sort key.
    - No GROUP BY / DISTINCT / dedup of any kind — two status='active' rows that both match the
      same query are both returned as separate entries, never merged or averaged. This is a
      contract requirement (contradiction surfacing), not an oversight to "optimize" later.

    query_embedding is optional. This Genesis-side workstream does not own or configure an
    embedding provider (none is declared anywhere in this repo's env surface, and adding one
    here would be a new external dependency outside this chunk's scope). When omitted, the
    vector-distance term below evaluates to a constant, so ranking degrades gracefully to
    lexical-only (ts_rank) — the SQL still carries the pgvector `<=>` term structurally, so the
    query is hybrid-capable the moment a caller supplies a query embedding, with no route change
    required.
    """
    where_clauses = ["1=1"]
    params: dict[str, Any] = {"query": query, "top_k": top_k}

    if not include_superseded:
        where_clauses.append("status != 'superseded'")
    if entity_filter:
        where_clauses.append("entity = %(entity_filter)s")
        params["entity_filter"] = entity_filter

    if query_embedding is not None:
        vector_term = "(1 - (embedding <=> %(query_embedding)s::vector))"
        params["query_embedding"] = query_embedding
    else:
        vector_term = "0"

    sql = f"""
        SELECT
            chunk_id, vault_path, heading_path, chunk_index, content_sha256, content_text,
            entity, type, status, updated, supersedes, indexed_at,
            (ts_rank(to_tsvector('english', content_text), plainto_tsquery('english', %(query)s))
             + {vector_term}) AS score
        FROM vault_chunks
        WHERE {" AND ".join(where_clauses)}
        ORDER BY score DESC, updated DESC
        LIMIT %(top_k)s
    """
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def index_freshness() -> Any:
    """Max indexed_at across the whole vault_chunks table (None if the table is empty).

    Whole-table max, not just the returned rows — cheaper and the contract explicitly allows
    it ("or the whole table if that's cheaper and still correct").
    """
    sql = "SELECT MAX(indexed_at) AS index_updated_at FROM vault_chunks"
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
        return row["index_updated_at"] if row else None
