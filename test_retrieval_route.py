"""Contract-compliance test suite for the E4L Retrieval Contract route (master spec §9).

CHUNK_4_RETRIEVAL builds retrieval_route.py + retrieval_store.py; this suite proves each row of
the Retrieval Contract table is actually implemented, not just "the endpoint returns 200". Runs
without a live Postgres server (the assistant-tier server doesn't exist yet, matching this
repo's own money-path-guards CI discipline of never silently skipping a guard for a missing
dependency): route-level tests monkeypatch retrieval_store.query_chunks/index_freshness;
one dedicated test exercises retrieval_store.py's own SQL construction against a fake
psycopg-shaped connection (no real DB socket opened). A separate, explicitly-marked
integration test activates automatically once ASSISTANT_PG_DATABASE_URL is set.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import main
import retrieval_route
import retrieval_store

client = TestClient(main.app)

# /retrieval/query authenticates a signed, owner-scoped principal token — the
# service-wide gateway key is deliberately NOT sufficient (it carries no
# principal, so knowledge could not be permission-filtered per requester).
_PRINCIPAL_TOKEN_KEY = "unit-test-principal-token-key-0123456789"
_GATEWAY_ONLY_HEADERS = {"x-agent-api-key": "test-gateway-key"}
_AUTH_HEADERS: dict[str, str] = {}


def _issue_test_token(*, scopes=("retrieval.query",), tenant_id="e4l"):
    from runtime.request_auth import Principal, issue_principal_token

    return issue_principal_token(
        Principal(
            principal_id="service:cato",
            tenant_id=tenant_id,
            client_id="cato",
            scopes=frozenset(scopes),
            auth_method="ap2",
            expires_at=0,
        ),
        key=_PRINCIPAL_TOKEN_KEY,
    )


@pytest.fixture(autouse=True)
def _gateway_key(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.delenv("AGENT_GATEWAY_SECRET", raising=False)
    monkeypatch.setenv("GENESIS_PRINCIPAL_TOKEN_KEY", _PRINCIPAL_TOKEN_KEY)
    _AUTH_HEADERS.clear()
    _AUTH_HEADERS.update({"X-Genesis-Principal-Token": _issue_test_token()})
    yield
    _AUTH_HEADERS.clear()


def _row(
    *,
    chunk_id="knowledge/finance/entity-structure.md#the-entity-map@0",
    vault_path="knowledge/finance/entity-structure.md",
    heading_path="the-entity-map",
    chunk_index=0,
    content_sha256="a" * 64,
    content_text="E4L is structured as...",
    entity="E4L",
    type="knowledge",
    status="active",
    updated=None,
    supersedes=None,
    indexed_at=None,
    score=0.9,
):
    return {
        "chunk_id": chunk_id,
        "vault_path": vault_path,
        "heading_path": heading_path,
        "chunk_index": chunk_index,
        "content_sha256": content_sha256,
        "content_text": content_text,
        "entity": entity,
        "type": type,
        "status": status,
        "updated": updated,
        "supersedes": supersedes or [],
        "indexed_at": indexed_at,
        "score": score,
    }


def _mock_store(monkeypatch, rows, *, index_updated_at=None, raise_on_query=None):
    """Patch retrieval_store.query_chunks/index_freshness as used by retrieval_route."""
    captured: dict = {}

    def fake_query_chunks(query, *, top_k=8, entity_filter=None, include_superseded=False, query_embedding=None):
        captured["query"] = query
        captured["top_k"] = top_k
        captured["entity_filter"] = entity_filter
        captured["include_superseded"] = include_superseded
        if raise_on_query is not None:
            raise raise_on_query
        return rows

    def fake_index_freshness():
        if raise_on_query is not None:
            raise raise_on_query
        return index_updated_at

    monkeypatch.setattr(retrieval_store, "query_chunks", fake_query_chunks)
    monkeypatch.setattr(retrieval_store, "index_freshness", fake_index_freshness)
    return captured


# ---------------------------------------------------------------------------
# Contract row: canonical chunk ID format + citation format
# ---------------------------------------------------------------------------

def test_chunk_id_and_citation_format(monkeypatch):
    _mock_store(monkeypatch, [_row()])

    resp = client.post("/retrieval/query", json={"query": "entity structure"}, headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["refusal"] is False
    chunk = body["chunks"][0]
    assert chunk["chunk_id"] == "knowledge/finance/entity-structure.md#the-entity-map@0"
    assert chunk["citation"] == "knowledge/finance/entity-structure.md#the-entity-map"
    assert chunk["content_sha256"] == "a" * 64
    assert chunk["content"] == "E4L is structured as..."


def test_content_is_bounded_and_redacted(monkeypatch):
    secret = "sk-ant-never-return-this-value"
    _mock_store(
        monkeypatch,
        rows=[_row(content_text=f"safe prefix Bearer abcdefghijklmnop {secret} " + "x" * 5000)],
    )
    client = TestClient(main.app)
    resp = client.post("/retrieval/query", json={"query": "entity structure"}, headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    content = resp.json()["chunks"][0]["content"]
    assert len(content) <= 4000
    assert secret not in content
    assert "abcdefghijklmnop" not in content


# ---------------------------------------------------------------------------
# Contract row: superseded-status filtering
# ---------------------------------------------------------------------------

def test_superseded_excluded_by_default(monkeypatch):
    """Default request never asks the store to include superseded rows."""
    captured = _mock_store(monkeypatch, [_row()])

    resp = client.post("/retrieval/query", json={"query": "entity structure"}, headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    assert captured["include_superseded"] is False


def test_superseded_included_when_requested(monkeypatch):
    superseded_row = _row(
        chunk_id="knowledge/finance/old-structure.md#old@0",
        vault_path="knowledge/finance/old-structure.md",
        heading_path="old",
        status="superseded",
        score=0.6,
    )
    captured = _mock_store(monkeypatch, [superseded_row])

    resp = client.post(
        "/retrieval/query",
        json={"query": "entity structure", "include_superseded": True},
        headers=_AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert captured["include_superseded"] is True
    body = resp.json()
    assert body["chunks"][0]["status"] == "superseded"


# ---------------------------------------------------------------------------
# Contract row: contradiction surfacing — two active chunks, never merged
# ---------------------------------------------------------------------------

def test_contradiction_surfacing_returns_both_active_chunks_unmerged(monkeypatch):
    row_a = _row(chunk_id="a.md#x@0", vault_path="a.md", heading_path="x", score=0.9)
    row_b = _row(chunk_id="b.md#y@0", vault_path="b.md", heading_path="y", score=0.85)
    _mock_store(monkeypatch, [row_a, row_b])

    resp = client.post("/retrieval/query", json={"query": "same topic"}, headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["chunks"]) == 2
    ids = {c["chunk_id"] for c in body["chunks"]}
    assert ids == {"a.md#x@0", "b.md#y@0"}


# ---------------------------------------------------------------------------
# Contract row: freshness / staleness flag
# ---------------------------------------------------------------------------

def test_staleness_flag_true_past_threshold(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_STALENESS_THRESHOLD_HOURS", "24")
    old_ts = datetime.now(timezone.utc) - timedelta(hours=48)
    _mock_store(monkeypatch, [_row()], index_updated_at=old_ts)

    resp = client.post("/retrieval/query", json={"query": "entity structure"}, headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["stale"] is True


def test_staleness_flag_false_within_threshold(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_STALENESS_THRESHOLD_HOURS", "24")
    recent_ts = datetime.now(timezone.utc) - timedelta(hours=1)
    _mock_store(monkeypatch, [_row()], index_updated_at=recent_ts)

    resp = client.post("/retrieval/query", json={"query": "entity structure"}, headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["stale"] is False


# ---------------------------------------------------------------------------
# Contract row: hybrid ranking — metadata filter applied before scoring
# ---------------------------------------------------------------------------

def test_metadata_filter_applied_before_ranking(monkeypatch):
    """A row that would out-score everything else, but belongs to a different entity than the
    requested entity_filter, must never appear. Because filtering happens in retrieval_store's
    SQL WHERE clause (not a route-side post-filter), the mocked store here returns only the set
    of rows a correctly-filtered query would return — proving the route never independently
    re-expands or re-ranks past what the store already filtered."""
    included = _row(entity="E4L", score=0.5)
    captured = _mock_store(monkeypatch, [included])  # the excluded higher-scoring row is never
    # even handed to the route — exactly what the real WHERE clause would do.

    resp = client.post(
        "/retrieval/query",
        json={"query": "entity structure", "entity_filter": "E4L"},
        headers=_AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert captured["entity_filter"] == "E4L"
    body = resp.json()
    assert len(body["chunks"]) == 1
    assert body["chunks"][0]["entity"] == "E4L"


def test_recency_is_tiebreaker_not_primary_key_in_store_sql():
    """Unit-level proof against retrieval_store.py's actual SQL construction (no live DB): the
    generated query orders by score before updated, and the superseded/entity filters land in
    the WHERE clause, not a post-filter."""

    class _FakeCursor:
        def __init__(self):
            self.last_sql = None
            self.last_params = None

        def execute(self, sql, params):
            self.last_sql = sql
            self.last_params = params

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeConn:
        def __init__(self, cursor):
            self._cursor = cursor

        def cursor(self):
            return self._cursor

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    cursor = _FakeCursor()
    import retrieval_store as rs

    original_conn = rs._conn
    rs._conn = lambda: _FakeConn(cursor)
    try:
        rs.query_chunks("test query", entity_filter="E4L", include_superseded=False)
    finally:
        rs._conn = original_conn

    sql = cursor.last_sql
    assert "ORDER BY score DESC, updated DESC" in sql
    assert "status != 'superseded'" in sql
    assert "entity = %(entity_filter)s" in sql
    assert cursor.last_params["entity_filter"] == "E4L"

    # include_superseded=True must NOT add the superseded exclusion clause.
    cursor2 = _FakeCursor()
    rs._conn = lambda: _FakeConn(cursor2)
    try:
        rs.query_chunks("test query", include_superseded=True)
    finally:
        rs._conn = original_conn
    assert "status != 'superseded'" not in cursor2.last_sql


# ---------------------------------------------------------------------------
# Contract row: refusal path
# ---------------------------------------------------------------------------

def test_refusal_path_below_threshold_returns_200_empty_chunks(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_MIN_SCORE", "0.35")
    low_score_row = _row(score=0.1)
    _mock_store(monkeypatch, [low_score_row])

    resp = client.post("/retrieval/query", json={"query": "nothing relevant"}, headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["refusal"] is True
    assert body["chunks"] == []
    assert body["reason"] == "no vault answer found above threshold"
    assert body.get("degraded", False) is False


# ---------------------------------------------------------------------------
# Contract row: degraded-connection path + gateway boot safety
# ---------------------------------------------------------------------------

def test_degraded_connection_path_returns_200(monkeypatch):
    _mock_store(monkeypatch, [], raise_on_query=RuntimeError("ASSISTANT_PG_DATABASE_URL not configured"))

    resp = client.post("/retrieval/query", json={"query": "anything"}, headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["refusal"] is True
    assert body["degraded"] is True
    assert body["degraded_reason"] == "assistant-tier PG unavailable"
    assert body["chunks"] == []


def test_health_still_200_when_assistant_pg_unset(monkeypatch):
    monkeypatch.delenv("ASSISTANT_PG_DATABASE_URL", raising=False)
    resp = client.get("/health")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Contract row: auth enforced (same guard as every other /agents/* route)
# ---------------------------------------------------------------------------

def test_auth_required_missing_key_rejected(monkeypatch):
    resp = client.post("/retrieval/query", json={"query": "anything"})
    assert resp.status_code == 401


def test_auth_required_correct_key_accepted(monkeypatch):
    _mock_store(monkeypatch, [_row()])
    resp = client.post("/retrieval/query", json={"query": "anything"}, headers=_AUTH_HEADERS)
    assert resp.status_code == 200


def test_shared_gateway_key_alone_cannot_read_knowledge(monkeypatch):
    """The service-wide key yields no principal, so it must not read knowledge.

    Permission filtering is per-requester (retrieval_route._query_knowledge_backbone).
    A credential every caller shares cannot express "who is asking", so accepting
    it here would return one tenant's chunks to any key holder.
    """
    _mock_store(monkeypatch, [_row()])
    resp = client.post(
        "/retrieval/query", json={"query": "anything"}, headers=_GATEWAY_ONLY_HEADERS
    )
    assert resp.status_code == 401, resp.text


def test_principal_without_retrieval_scope_is_denied(monkeypatch):
    _mock_store(monkeypatch, [_row()])
    token = _issue_test_token(scopes=("agent.invoke",))
    resp = client.post(
        "/retrieval/query",
        json={"query": "anything"},
        headers={"X-Genesis-Principal-Token": token},
    )
    assert resp.status_code == 403, resp.text


def test_forged_principal_token_is_rejected(monkeypatch):
    """A token signed with any other key must never authenticate."""
    from runtime.request_auth import Principal, issue_principal_token

    _mock_store(monkeypatch, [_row()])
    forged = issue_principal_token(
        Principal(
            principal_id="service:attacker",
            tenant_id="e4l",
            client_id="attacker",
            scopes=frozenset({"retrieval.query"}),
            auth_method="ap2",
            expires_at=0,
        ),
        key="an-entirely-different-key-0123456789abcd",
    )
    resp = client.post(
        "/retrieval/query",
        json={"query": "anything"},
        headers={"X-Genesis-Principal-Token": forged},
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Contract row: never calls an LLM
# ---------------------------------------------------------------------------

def test_route_never_calls_an_llm(monkeypatch):
    import main as _main

    def fail_call_llm_router(*args, **kwargs):
        raise AssertionError("retrieval route must never call an LLM")

    monkeypatch.setattr(_main, "call_llm_router", fail_call_llm_router)
    _mock_store(monkeypatch, [_row()])

    resp = client.post("/retrieval/query", json={"query": "anything"}, headers=_AUTH_HEADERS)
    assert resp.status_code == 200


def test_no_llm_client_referenced_in_retrieval_source():
    """Source-level guard: neither module names an LLM client/router call anywhere in its
    text, matching the contract's 'the LLM is not called to guess' requirement."""
    import pathlib

    for name in ("retrieval_route.py", "retrieval_store.py"):
        text = pathlib.Path(name).read_text(encoding="utf-8")
        # Split the docstring-only mention of main.py's helper name from an actual call.
        assert "call_llm_router(" not in text
        assert "httpx.AsyncClient(" not in text


# ---------------------------------------------------------------------------
# Contract row: malformed request
# ---------------------------------------------------------------------------

def test_malformed_request_missing_query_returns_422():
    resp = client.post("/retrieval/query", json={}, headers=_AUTH_HEADERS)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Real-connection integration test — activates once the other workstream's
# assistant-tier PG server is reachable. Skipped today by design.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.getenv("ASSISTANT_PG_DATABASE_URL"),
    reason="ASSISTANT_PG_DATABASE_URL not set — assistant-tier PG server not reachable from this environment",
)
def test_real_connection_integration():
    resp = client.post("/retrieval/query", json={"query": "smoke test"}, headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert "chunks" in body
    assert body.get("degraded", False) is False
