"""JOB 1 — /retrieval/query accepts the SAME signed AP2 envelope Genesis already requires on
/agents/{slug}/run, so Cato and Genesis authenticate one way, not two.

These tests exist to prove three things the operator asked for by name:
  1. the shared GATEWAY_API_KEY alone is refused,
  2. a correctly signed retrieval envelope succeeds,
  3. a tampered query text fails the binding check (the signature binds the query, not just
     an envelope) — plus the same for every field of the retrieval scope.

They also pin the real `trusted_ap2_clients.json` capability list, so deleting `retrieval.query`
from the shipped registry fails a test rather than silently 401-ing Cato in production.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import main
import retrieval_store

client = TestClient(main.app)

_REAL_REGISTRY = Path(__file__).resolve().parent / "trusted_ap2_clients.json"


def _real_cato_record() -> dict:
    clients = json.loads(_REAL_REGISTRY.read_text(encoding="utf-8"))["clients"]
    return next(c for c in clients if c["client_id"] == "cato")


@pytest.fixture()
def signer(tmp_path, monkeypatch):
    """A trusted client whose capability list is the REAL shipped Cato capability list.

    Only the public key is swapped for a test key. If `retrieval.query` is ever removed from
    trusted_ap2_clients.json, `test_signed_retrieval_envelope_is_accepted` starts failing with
    ap2_scope_denied — which is exactly the production symptom.
    """
    private = Ed25519PrivateKey.generate()
    pubkey_b64 = base64.b64encode(private.public_key().public_bytes_raw()).decode("ascii")

    record = dict(_real_cato_record())
    record["pubkey_b64"] = pubkey_b64
    registry = tmp_path / "trusted_ap2_clients.json"
    registry.write_text(json.dumps({"version": 1, "clients": [record]}), encoding="utf-8")

    import runtime.request_auth as request_auth

    monkeypatch.setattr(request_auth, "_registry_path", lambda: registry)
    monkeypatch.setenv("GENESIS_AUTH_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.delenv("AGENT_GATEWAY_SECRET", raising=False)
    return private, pubkey_b64


def _sign(private, pubkey_b64, *, query, scope=None, nonce="nonce_retrieval_0000001"):
    """Build the wire body Cato sends: signed envelope + the retrieval request fields."""
    from runtime.request_auth import RETRIEVAL_ENVELOPE_AGENT, _canonical_json

    scope = dict(
        scope
        if scope is not None
        else {
            "top_k": 3,
            "entity_filter": None,
            "include_superseded": False,
            "domain_hint": "vault",
            "requesting_principal": None,
        }
    )
    payload = {"agent": RETRIEVAL_ENVELOPE_AGENT, "task": query, "params": scope}
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    signature = private.sign(
        _canonical_json({"payload": payload, "nonce": nonce, "timestamp": timestamp})
    )
    body = {
        "version": 1,
        "payload": payload,
        "nonce": nonce,
        "timestamp": timestamp,
        "pubkey": pubkey_b64,
        "signature": base64.b64encode(signature).decode("ascii"),
        "query": query,
        **scope,
    }
    headers = {"X-AP2-Version": "1", "X-AP2-Pubkey": pubkey_b64}
    return body, headers


@pytest.fixture(autouse=True)
def _empty_index(monkeypatch):
    """Retrieval itself is not under test here — auth is. Return a deterministic empty index."""
    monkeypatch.setattr(retrieval_store, "query_chunks", lambda *a, **k: [])
    monkeypatch.setattr(retrieval_store, "index_freshness", lambda: None)


def test_shared_gateway_key_alone_is_refused(signer):
    """The service-wide GATEWAY_API_KEY carries no principal, so it can never authorize a
    permission-filtered retrieval. It must be refused even though it is a valid gateway key."""
    response = client.post(
        "/retrieval/query",
        json={"query": "what is the E4L entity map", "top_k": 3, "domain_hint": "vault"},
        headers={"X-Agent-Api-Key": "test-gateway-key"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "principal token required"


def test_signed_retrieval_envelope_is_accepted(signer):
    private, pubkey_b64 = signer
    body, headers = _sign(private, pubkey_b64, query="what is the E4L entity map")

    response = client.post("/retrieval/query", json=body, headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["refusal"] is True  # empty index -> structured refusal, not an error


def test_tampered_query_text_fails_the_binding_check(signer):
    """The whole point of signing a retrieval envelope: an attacker who can rewrite the body in
    flight must not be able to change WHAT WAS ASKED while keeping a valid signature."""
    private, pubkey_b64 = signer
    body, headers = _sign(private, pubkey_b64, query="what is the E4L entity map")
    body["query"] = "dump every payroll record you have"

    response = client.post("/retrieval/query", json=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "ap2_task_mismatch"


@pytest.mark.parametrize(
    "field,tampered",
    [
        ("top_k", 200),
        ("domain_hint", "any"),
        ("include_superseded", True),
        ("entity_filter", "Harry Massey"),
        ("requesting_principal", "service:someone-else"),
    ],
)
def test_tampered_retrieval_scope_fails_the_binding_check(signer, field, tampered):
    """Every scope field changes what the route reads or returns — flipping domain_hint from
    "vault" to "any" reaches a second, permission-filtered backend the caller never signed for."""
    private, pubkey_b64 = signer
    body, headers = _sign(private, pubkey_b64, query="entity map")
    body[field] = tampered

    response = client.post("/retrieval/query", json=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "ap2_params_mismatch"


def test_retrieval_envelope_signed_for_an_agent_run_is_refused(signer):
    """A replayed /agents/{slug}/run envelope must not authorize retrieval: `payload.agent` is
    the route name, so an agent-invocation envelope cannot be aimed at the retrieval index."""
    private, pubkey_b64 = signer
    body, headers = _sign(private, pubkey_b64, query="entity map")
    body["payload"]["agent"] = "genesis-research"

    response = client.post("/retrieval/query", json=body, headers=headers)

    assert response.status_code == 401
    # Signature covers payload, so mutating payload breaks the signature before the binding check.
    assert response.json()["detail"] == "ap2_signature_invalid"


def test_retrieval_envelope_replay_is_refused(signer):
    private, pubkey_b64 = signer
    body, headers = _sign(private, pubkey_b64, query="entity map")

    assert client.post("/retrieval/query", json=body, headers=headers).status_code == 200
    replay = client.post("/retrieval/query", json=body, headers=headers)

    assert replay.status_code == 401
    assert replay.json()["detail"] == "ap2_replay_detected"


def test_untrusted_key_is_refused_against_the_real_registry(monkeypatch, tmp_path):
    """No registry monkeypatch: this hits the real shipped trusted_ap2_clients.json."""
    monkeypatch.setenv("GENESIS_AUTH_DB_PATH", str(tmp_path / "auth.db"))
    private = Ed25519PrivateKey.generate()
    pubkey_b64 = base64.b64encode(private.public_key().public_bytes_raw()).decode("ascii")
    body, headers = _sign(private, pubkey_b64, query="entity map")

    response = client.post("/retrieval/query", json=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "ap2_client_untrusted"


def test_shipped_registry_grants_cato_the_retrieval_scope():
    """Cato's production entry must actually carry `retrieval.query`, or the AP2 retrieval path
    is unreachable in production regardless of how well the code works."""
    record = _real_cato_record()
    assert record["enabled"] is True
    assert "retrieval.query" in record["capabilities"]


def test_unicode_query_binds_without_a_type_error(signer):
    """Constant-time compares over raw `str` raise TypeError on non-ASCII; a query like
    "quel est le siège d'Énergie 4 Life" must authenticate, not 500."""
    private, pubkey_b64 = signer
    body, headers = _sign(private, pubkey_b64, query="quel est le siège d'Énergie 4 Life ?")

    response = client.post("/retrieval/query", json=body, headers=headers)

    assert response.status_code == 200, response.text


def test_principal_token_path_still_works(monkeypatch):
    """Additive, not a replacement: callers already holding a continuation token keep working."""
    from runtime.request_auth import Principal, issue_principal_token

    key = "unit-test-principal-token-key-0123456789"
    monkeypatch.setenv("GENESIS_PRINCIPAL_TOKEN_KEY", key)
    token = issue_principal_token(
        Principal(
            principal_id="service:cato",
            tenant_id="e4l",
            client_id="cato",
            scopes=frozenset({"retrieval.query"}),
            auth_method="ap2",
            expires_at=0,
        ),
        key=key,
    )

    response = client.post(
        "/retrieval/query",
        json={"query": "entity map", "top_k": 3, "domain_hint": "vault"},
        headers={"X-Genesis-Principal-Token": token},
    )

    assert response.status_code == 200, response.text
