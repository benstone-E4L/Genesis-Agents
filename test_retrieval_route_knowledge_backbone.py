"""CHUNK_4_RETRIEVAL test suite — the knowledge_backbone second-backend extension to
POST /retrieval/query. Companion to test_retrieval_route.py (vault-only contract, unchanged and
unmodified by this file). No live rg-kb-prod connection is ever used or required: route-level
tests monkeypatch knowledge_backbone_store.query_chunks with in-memory fixture rows — the exact
pattern test_retrieval_route.py already uses for retrieval_store.query_chunks. Production code
(knowledge_backbone_store.py) always raises today; these tests prove the route handles that raise
correctly (partial: true) AND prove the fan-out/dedup/permission-filter path works once a real
backend exists, without ever claiming a live connection here.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import knowledge_backbone_store
import main
import retrieval_route
import retrieval_store

client = TestClient(main.app)

# The knowledge_backbone permission filter is per-requester, so the route
# authenticates a signed principal token rather than the service-wide gateway
# key.  `principal_id` below is the identity the permission fixtures grant.
_PRINCIPAL_TOKEN_KEY = "unit-test-principal-token-key-0123456789"
_TEST_PRINCIPAL_ID = "user:ben@e4l.com"
_AUTH_HEADERS: dict[str, str] = {}


def _issue_test_token(principal_id: str = _TEST_PRINCIPAL_ID):
    from runtime.request_auth import Principal, issue_principal_token

    return issue_principal_token(
        Principal(
            principal_id=principal_id,
            tenant_id="e4l",
            client_id="cato",
            scopes=frozenset({"retrieval.query"}),
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


def _vault_row(**overrides):
    row = {
        "chunk_id": "knowledge/finance/entity-structure.md#the-entity-map@0",
        "vault_path": "knowledge/finance/entity-structure.md",
        "heading_path": "the-entity-map",
        "chunk_index": 0,
        "content_sha256": "a" * 64,
        "content_text": "E4L is structured as...",
        "entity": "E4L",
        "type": "knowledge",
        "status": "active",
        "updated": None,
        "supersedes": [],
        "indexed_at": None,
        "score": 0.9,
    }
    row.update(overrides)
    return row


def _kb_row(**overrides):
    row = {
        "chunk_id": "kb://controller/1AbC.../0",
        "text": "...",
        "citation": "vendor-agreement-x.pdf (controller@e4l.com Drive)",
        "source_account": "controller",
        "source_classification": "controller",
        "drive_id": "controller-mydrive",
        "file_id": "1AbC...",
        "original_path": "My Drive/AP/2026/vendor-agreement-x.pdf",
        "modified_at": "2026-07-15T00:00:00Z",
        "content_hash": "9f8e...",
        "permissions_snapshot": {"principals": ["user:ben@e4l.com"], "public": False},
        "score": 0.8,
    }
    row.update(overrides)
    return row


def _mock_vault_store(monkeypatch, rows, *, index_updated_at=None, raise_on_query=None):
    def fake_query_chunks(query, *, top_k=8, entity_filter=None, include_superseded=False, query_embedding=None):
        if raise_on_query is not None:
            raise raise_on_query
        return rows

    def fake_index_freshness():
        if raise_on_query is not None:
            raise raise_on_query
        return index_updated_at

    monkeypatch.setattr(retrieval_store, "query_chunks", fake_query_chunks)
    monkeypatch.setattr(retrieval_store, "index_freshness", fake_index_freshness)


def _mock_kb_backend(monkeypatch, rows=None, *, raise_on_query=None):
    def fake_query_chunks(query, *, top_k=8):
        if raise_on_query is not None:
            raise raise_on_query
        return rows or []

    monkeypatch.setattr(knowledge_backbone_store, "query_chunks", fake_query_chunks)


# ---------------------------------------------------------------------------
# A vault-only question never triggers a knowledge_backbone call at all.
# ---------------------------------------------------------------------------

def test_vault_only_question_never_calls_knowledge_backbone(monkeypatch):
    _mock_vault_store(monkeypatch, [_vault_row()])
    called = {"count": 0}

    def fail_if_called(query, *, top_k=8):
        called["count"] += 1
        raise AssertionError("knowledge_backbone must not be queried for domain_hint='vault'")

    monkeypatch.setattr(knowledge_backbone_store, "query_chunks", fail_if_called)

    resp = client.post(
        "/retrieval/query",
        json={"query": "entity structure", "domain_hint": "vault",
              "requesting_principal": "user:ben@e4l.com"},
        headers=_AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert called["count"] == 0
    body = resp.json()
    assert len(body["chunks"]) == 1
    assert body["chunks"][0]["source"] == "vault"


def test_unauthenticated_request_never_reaches_knowledge_backbone(monkeypatch):
    """Fails closed on identity: no verified principal -> 401 before any backend call.

    Previously the identity came from the request body, so "no principal" was a
    caller choice.  It is now an authentication outcome: the backend is not
    reachable at all without a signed token.
    """
    _mock_vault_store(monkeypatch, [])
    called = {"count": 0}

    def fail_if_called(query, *, top_k=8):
        called["count"] += 1
        raise AssertionError("must not be called for an unauthenticated request")

    monkeypatch.setattr(knowledge_backbone_store, "query_chunks", fail_if_called)

    resp = client.post("/retrieval/query", json={"query": "anything"})
    assert resp.status_code == 401, resp.text
    assert called["count"] == 0


def test_body_requesting_principal_cannot_override_authenticated_identity(monkeypatch):
    """A self-asserted body field must never widen what the token holder may read.

    The chunk below is readable only by user:mallory@e4l.com.  The caller
    authenticates as user:ben@e4l.com and claims to be mallory in the body; the
    permission filter must still use the signed identity and drop the chunk.
    """
    _mock_vault_store(monkeypatch, [])
    kb_row = _kb_row(
        permissions_snapshot={"principals": ["user:mallory@e4l.com"], "public": False}
    )
    _mock_kb_backend(monkeypatch, [kb_row])

    resp = client.post(
        "/retrieval/query",
        json={
            "query": "vendor agreement",
            "requesting_principal": "user:mallory@e4l.com",
        },
        headers=_AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["chunks"] == []


# ---------------------------------------------------------------------------
# A drive-relevant question surfaces knowledge_backbone results; vault contributes nothing.
# ---------------------------------------------------------------------------

def test_drive_only_relevant_question_surfaces_knowledge_backbone_results(monkeypatch):
    _mock_vault_store(monkeypatch, [])  # nothing relevant in the vault
    kb_row = _kb_row(permissions_snapshot={"principals": ["user:ben@e4l.com"], "public": False})
    _mock_kb_backend(monkeypatch, [kb_row])

    resp = client.post(
        "/retrieval/query",
        json={"query": "vendor agreement", "requesting_principal": "user:ben@e4l.com"},
        headers=_AUTH_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["refusal"] is False
    assert len(body["chunks"]) == 1
    chunk = body["chunks"][0]
    assert chunk["source"] == "knowledge_backbone"
    # Every provenance field populated, never null for a real chunk.
    for field in ("source_account", "source_classification", "drive_id", "file_id",
                  "original_path", "modified_at", "content_hash"):
        assert chunk[field] is not None, f"{field} was null"
    assert chunk["source_account"] == "controller"


def test_vault_and_knowledge_backbone_merge_when_both_relevant(monkeypatch):
    _mock_vault_store(monkeypatch, [_vault_row()])
    kb_row = _kb_row(permissions_snapshot={"principals": ["user:ben@e4l.com"], "public": False})
    _mock_kb_backend(monkeypatch, [kb_row])

    resp = client.post(
        "/retrieval/query",
        json={"query": "entity structure", "requesting_principal": "user:ben@e4l.com"},
        headers=_AUTH_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    sources = {c["source"] for c in body["chunks"]}
    assert sources == {"vault", "knowledge_backbone"}


# ---------------------------------------------------------------------------
# Degrade behavior: knowledge_backbone unreachable -> partial: true, vault results preserved.
# ---------------------------------------------------------------------------

def test_knowledge_backbone_unreachable_degrades_to_partial_vault_results_preserved(monkeypatch):
    _mock_vault_store(monkeypatch, [_vault_row()])
    _mock_kb_backend(monkeypatch, raise_on_query=RuntimeError("connection refused"))

    resp = client.post(
        "/retrieval/query",
        json={"query": "entity structure", "requesting_principal": "user:ben@e4l.com"},
        headers=_AUTH_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["partial"] is True
    assert body["partial_reason"] == "knowledge_backbone unavailable"
    assert len(body["chunks"]) == 1
    assert body["chunks"][0]["source"] == "vault"
    assert body["refusal"] is False


def test_knowledge_backbone_unreachable_with_nothing_from_vault_never_5xxs(monkeypatch):
    _mock_vault_store(monkeypatch, [])
    _mock_kb_backend(monkeypatch, raise_on_query=TimeoutError("timed out"))

    resp = client.post(
        "/retrieval/query",
        json={"query": "anything", "requesting_principal": "user:ben@e4l.com"},
        headers=_AUTH_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["partial"] is True
    assert body["refusal"] is True
    assert body["chunks"] == []


def test_production_knowledge_backbone_store_always_raises_today(monkeypatch):
    """knowledge_backbone_store.py never fabricates live data — confirms the real (unpatched)
    module still raises, so a caller can never mistake it for a working connection."""
    monkeypatch.delenv("KNOWLEDGE_BACKBONE_MCP_ENDPOINT", raising=False)
    monkeypatch.delenv("KNOWLEDGE_BACKBONE_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError):
        knowledge_backbone_store.query_chunks("anything")


# ---------------------------------------------------------------------------
# Dedup + permission-filter exercised end-to-end through the route (CHUNK_3's logic, live).
# ---------------------------------------------------------------------------

def test_dedup_and_permission_filter_exercised_through_route(monkeypatch):
    _mock_vault_store(monkeypatch, [])
    canonical = _kb_row(
        chunk_id="kb://canonical/f1/0", drive_id="canonical-drive", file_id="f1",
        content_hash="dup-hash", source_account="canonical", source_classification="canonical",
        permissions_snapshot={"principals": [], "public": True},
    )
    controller_dup = _kb_row(
        chunk_id="kb://controller/f2/0", drive_id="controller-mydrive", file_id="f2",
        content_hash="dup-hash", source_account="controller", source_classification="controller",
        permissions_snapshot={"principals": [], "public": True},
    )
    restricted = _kb_row(
        chunk_id="kb://controller/f3/0", drive_id="controller-mydrive", file_id="f3",
        content_hash="unique-hash", source_account="controller", source_classification="controller",
        permissions_snapshot={"principals": ["user:someone-else@e4l.com"], "public": False},
    )
    _mock_kb_backend(monkeypatch, [canonical, controller_dup, restricted])

    resp = client.post(
        "/retrieval/query",
        json={"query": "vendor", "requesting_principal": "user:ben@e4l.com"},
        headers=_AUTH_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    # Exactly one result: the canonical copy wins the dedup/precedence, and the restricted
    # (permission-mismatched) chunk never appears.
    assert len(body["chunks"]) == 1
    assert body["chunks"][0]["source_classification"] == "canonical"


# ---------------------------------------------------------------------------
# Response schema shares zero field names with the FinanceOS Document Registry's shape.
# ---------------------------------------------------------------------------

def test_response_schema_disjoint_from_document_registry_shape():
    """Structural (schema-level), not just code review: neither RetrievalChunk nor
    RetrievalQueryResponse defines any field named like the Document Registry's
    (SPEC-financeos-document-registry.md §12) response shape — document_id, blob_signed_url,
    approved. (The pre-existing 'status' field on RetrievalChunk predates CHUNK_4_RETRIEVAL —
    unrelated to this chunk's additions — and is out of this chunk's scope to change.)"""
    forbidden = {"document_id", "blob_signed_url", "approved"}
    chunk_fields = set(retrieval_route.RetrievalChunk.model_fields.keys())
    response_fields = set(retrieval_route.RetrievalQueryResponse.model_fields.keys())
    assert not (chunk_fields & forbidden), chunk_fields & forbidden
    assert not (response_fields & forbidden), response_fields & forbidden


# ---------------------------------------------------------------------------
# Existing vault-only tests (test_retrieval_route.py) are untouched by this file — run together
# in AGENTS.md's validation command; this is just a same-process import sanity check.
# ---------------------------------------------------------------------------

def test_vault_only_default_request_shape_unchanged(monkeypatch):
    """A minimal legacy-shaped request (no domain_hint/requesting_principal keys at all) still
    round-trips exactly as before CHUNK_4_RETRIEVAL."""
    _mock_vault_store(monkeypatch, [_vault_row()])
    resp = client.post("/retrieval/query", json={"query": "entity structure"}, headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["refusal"] is False
    assert len(body["chunks"]) == 1
    assert body["chunks"][0]["source"] == "vault"
