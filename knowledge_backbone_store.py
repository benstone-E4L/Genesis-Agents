"""CHUNK_4_RETRIEVAL — backend-selection layer for the `knowledge_backbone` second retrieval
backend (SPEC-e4l-drive-knowledge-integration.md).

Deliberately does NOT implement a real `kb-mcp-prod` MCP client or a real direct-Postgres
client yet. The spec's Open Question 3 (does `kb-mcp-prod` already expose a query interface
Genesis can call directly, or does this need a direct read-only Postgres connection to
`knowledge_backbone` instead?) is not resolved — see `.ralph/progress.md`'s CHUNK_1_CONFIRM
entry and `SPEC-e4l-drive-knowledge-integration.md` §14. Wiring whichever real client Question 3
selects is CHUNK_6_VERIFY's job, once that answer lands.

Until then, `query_chunks()` always raises — never returns fixture/mock rows — so this module
can never be mistaken for a live connection by `retrieval_route.py` or any caller. The route
catches the raise and reports `partial: true` (see retrieval_route.py's `_query_knowledge_backbone`),
exactly the same degrade pattern `retrieval_store.py` already uses for the vault backend when
`ASSISTANT_PG_DATABASE_URL` is unset.

Tests exercise the fan-out/dedup/permission-filter logic in `retrieval_route.py` by
monkeypatching this module's `query_chunks` with in-memory fixture rows — the same pattern
`test_retrieval_route.py` already uses for `retrieval_store.query_chunks`. That is the only
place a "mock backend" exists in this workstream; it is never wired into production code paths.
"""
from __future__ import annotations

import os


def _mcp_endpoint() -> str:
    """Read at call time (not import time), matching retrieval_store.py's env-read convention."""
    return (os.getenv("KNOWLEDGE_BACKBONE_MCP_ENDPOINT") or "").strip()


def _database_url() -> str:
    return (os.getenv("KNOWLEDGE_BACKBONE_DATABASE_URL") or "").strip()


def query_chunks(query: str, *, top_k: int = 8) -> list[dict]:
    """Always raises today — see module docstring. The specific exception type/message differs
    by which env var is set, so a future CHUNK_6 implementation can replace either branch in
    isolation without touching the other, once Open Question 3 resolves which one is real."""
    if _mcp_endpoint():
        raise NotImplementedError(
            "KNOWLEDGE_BACKBONE_MCP_ENDPOINT is set but the kb-mcp-prod client is not yet "
            "implemented - deferred to CHUNK_6_VERIFY pending Open Question 3 (kb-mcp-prod's "
            "query/auth surface, per SPEC-e4l-drive-knowledge-integration.md paragraph 14)."
        )
    if _database_url():
        raise NotImplementedError(
            "KNOWLEDGE_BACKBONE_DATABASE_URL is set but the direct-Postgres client is not yet "
            "implemented - deferred to CHUNK_6_VERIFY pending Open Question 3 confirming this "
            "path over kb-mcp-prod."
        )
    raise RuntimeError(
        "knowledge_backbone backend not configured (neither KNOWLEDGE_BACKBONE_MCP_ENDPOINT nor "
        "KNOWLEDGE_BACKBONE_DATABASE_URL is set) - Open Question 3 unresolved, see "
        "SPEC-e4l-drive-knowledge-integration.md paragraph 14."
    )
