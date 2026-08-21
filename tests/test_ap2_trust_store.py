"""Phase 1 — the AP2 trust boundary after Cato's vault was rebuilt.

Cato's Ed25519 identity changed: the vault holding the 2026-05-17 private key was
destroyed and is unrecoverable. Two things had to happen in
``trusted_ap2_clients.json`` and both are load-bearing:

1. the rebuilt vault's public key becomes the trusted key for ``client_id=cato``,
   keeping the existing capability set (``retrieval.query`` included, or the whole
   AP2 retrieval path 401s in production while every other test stays green);
2. the retired May key is **deleted**, not disabled. A disabled record is still a
   trust anchor for a key nobody controls, one ``"enabled": true`` edit away from
   live. There is no rotation overlap to preserve because the old private half
   cannot sign anything ever again.

Every test here reads the REAL shipped ``trusted_ap2_clients.json``. The
end-to-end tests swap exactly one field — ``pubkey_b64`` — for a key this process
can sign with, because the suite does not (and must not) hold Cato's private key.
Everything else about the record, above all ``capabilities``, comes from the
shipped file, so deleting a scope there fails a test here rather than silently
breaking production.
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
from runtime.request_auth import RETRIEVAL_ENVELOPE_AGENT, _canonical_json

client = TestClient(main.app)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_REGISTRY = REPO_ROOT / "trusted_ap2_clients.json"

#: Cato's current vault public key, as supplied by the operator and stored in
#: Cato's vault as ``CATO_AP2_PUBKEY``. Pinned deliberately: a future rotation
#: MUST edit this constant, so no key can enter the trust store without a test
#: changing in the same commit.
#:
#: Rotated 2026-08-21 (Ben's decision, HANDOFF-CATO-GENESIS-XERO-DEMO-2026-08-21.md,
#: Option B). FLAG: this value is byte-identical to the key that was previously
#: retired below as ``RETIRED_AUG13_0000_PUBKEY_B64`` for having untested,
#: not-operator-verified provenance. It is being re-trusted here solely because
#: the operator's current CATO_VAULT_PASSWORD unlocks the live vault.enc and
#: returns this exact value via ``Vault.get('CATO_AP2_PUBKEY')`` as of 2026-08-21
#: — i.e. it is the key the operator currently, verifiably controls, superseding
#: the 2026-08-13T18:40:00Z key below (now retired in its place). See
#: ``trusted_ap2_clients.json``'s own notes field for the full incident history.
CATO_CURRENT_PUBKEY_B64 = "C0b5/ct/FoXrZ99mBj+LsDAPQGw3kBLFvl+ZlnvwDIg="

#: The 2026-05-17 key from the destroyed vault. Must appear in NO client record.
RETIRED_MAY_PUBKEY_B64 = "ZgBMq0+O0CXEp1eWG8JdMsikQNT6SPWh4Hop5vbg7QQ="

#: The 2026-08-13T18:40:00Z key, trusted from 2026-08-13 until superseded by the
#: 2026-08-21 rotation above. Its private half lived only in the vault
#: generation that was reconstructed on 2026-08-20; no operator controls it
#: post-reconstruction. Must appear in NO client record, same as the May key.
RETIRED_AUG13_1840_PUBKEY_B64 = "7G6FdR1XQVc1JlIo+o8xfJFIOi4UK/gq22YvXoy7des="

#: The capability set Cato is entitled to, pinned exactly. Removing one breaks
#: the AP2 path in production; adding one silently widens the trust boundary.
CATO_EXPECTED_CAPABILITIES = {
    "agent.invoke",
    "agent.list",
    "agent.health",
    "job.read",
    "artifact.read",
    "retrieval.query",
}


def _shipped_clients() -> list[dict]:
    data = json.loads(REAL_REGISTRY.read_text(encoding="utf-8"))
    assert data["version"] == 1
    return list(data["clients"])


def _shipped_cato_record() -> dict:
    return next(c for c in _shipped_clients() if c["client_id"] == "cato")


@pytest.fixture()
def sqlite_auth(tmp_path, monkeypatch):
    """Force the local file nonce store; these tests are about trust, not Postgres."""
    monkeypatch.setenv("GENESIS_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("GENESIS_AUTH_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.delenv("AGENT_GATEWAY_SECRET", raising=False)
    return tmp_path


@pytest.fixture()
def cato_signer(sqlite_auth, monkeypatch):
    """The REAL shipped Cato record with only ``pubkey_b64`` swapped for a test key.

    The suite cannot hold Cato's private key, so proving "the current key is
    accepted end to end" is split in two: this fixture proves the shipped
    *record* (principal, tenant, capabilities, enabled flag) authorises a
    correctly signed request, and
    ``test_declared_cato_key_is_trusted_by_the_real_shipped_registry`` proves the
    shipped *key bytes* are the operator's current ones and reach signature
    verification rather than being rejected as untrusted.
    """
    private = Ed25519PrivateKey.generate()
    pubkey_b64 = base64.b64encode(private.public_key().public_bytes_raw()).decode("ascii")

    record = dict(_shipped_cato_record())
    assert record["pubkey_b64"] == CATO_CURRENT_PUBKEY_B64  # swapping the current key, not a stale one
    record["pubkey_b64"] = pubkey_b64

    registry = sqlite_auth / "trusted_ap2_clients.json"
    registry.write_text(json.dumps({"version": 1, "clients": [record]}), encoding="utf-8")

    import runtime.request_auth as request_auth

    monkeypatch.setattr(request_auth, "_registry_path", lambda: registry)
    return private, pubkey_b64


@pytest.fixture(autouse=True)
def _empty_index(monkeypatch):
    """Retrieval results are not under test — the trust boundary is."""
    monkeypatch.setattr(retrieval_store, "query_chunks", lambda *a, **k: [])
    monkeypatch.setattr(retrieval_store, "index_freshness", lambda: None)


def _sign(private, pubkey_b64, *, query, nonce="nonce_trust_store_00001"):
    scope = {
        "top_k": 3,
        "entity_filter": None,
        "include_superseded": False,
        "domain_hint": "vault",
        "requesting_principal": None,
    }
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
    return body, {"X-AP2-Version": "1", "X-AP2-Pubkey": pubkey_b64}


def _unsigned_envelope(pubkey_b64: str, *, query="entity map", nonce="nonce_trust_store_00002"):
    """A well-formed envelope carrying a syntactically valid but wrong signature.

    Registry lookup happens BEFORE signature verification, so this is enough to
    ask the trust store one question and get an unambiguous answer: is this key
    trusted (``ap2_signature_invalid``) or not (``ap2_client_untrusted``)?
    """
    scope = {
        "top_k": 3,
        "entity_filter": None,
        "include_superseded": False,
        "domain_hint": "vault",
        "requesting_principal": None,
    }
    payload = {"agent": RETRIEVAL_ENVELOPE_AGENT, "task": query, "params": scope}
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "version": 1,
        "payload": payload,
        "nonce": nonce,
        "timestamp": timestamp,
        "pubkey": pubkey_b64,
        "signature": base64.b64encode(b"\x00" * 64).decode("ascii"),
        "query": query,
        **scope,
    }
    return body, {"X-AP2-Version": "1", "X-AP2-Pubkey": pubkey_b64}


# ---------------------------------------------------------------------------
# 1. The shipped file itself
# ---------------------------------------------------------------------------


def test_shipped_registry_pins_catos_rebuilt_vault_key():
    record = _shipped_cato_record()
    assert record["pubkey_b64"] == CATO_CURRENT_PUBKEY_B64
    assert record["algorithm"] == "ed25519"
    assert record["enabled"] is True
    assert record["principal_id"] == "service:cato"
    assert record["tenant_id"] == "e4l"
    assert set(record["capabilities"]) == CATO_EXPECTED_CAPABILITIES
    assert "retrieval.query" in record["capabilities"]


def test_shipped_cato_key_is_a_valid_32_byte_ed25519_key():
    """A typo'd or truncated base64 blob would fail at request time, not at deploy."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    raw = base64.b64decode(_shipped_cato_record()["pubkey_b64"], validate=True)
    assert len(raw) == 32
    Ed25519PublicKey.from_public_bytes(raw)


def test_retired_may_key_appears_in_no_client_record():
    """Deleted, not disabled: `enabled: false` still leaves the anchor in place."""
    for record in _shipped_clients():
        assert record["pubkey_b64"] != RETIRED_MAY_PUBKEY_B64, (
            f"client {record['client_id']!r} still carries the retired 2026-05-17 key; "
            "its private half is unrecoverable and no operator controls it"
        )


def test_retired_aug13_1840_key_appears_in_no_client_record():
    """Same rule for the 2026-08-13T18:40:00Z key superseded by the 2026-08-21 rotation."""
    for record in _shipped_clients():
        assert record["pubkey_b64"] != RETIRED_AUG13_1840_PUBKEY_B64, (
            f"client {record['client_id']!r} still carries the retired 2026-08-13T18:40:00Z "
            "key; its private half lived only in a vault generation that no longer exists"
        )


def test_every_shipped_pubkey_is_unique():
    """Two records sharing a key make `len(matches) != 1` reject a legitimate caller."""
    keys = [r["pubkey_b64"] for r in _shipped_clients()]
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# 2. Current key accepted
# ---------------------------------------------------------------------------


def test_correctly_signed_request_on_catos_shipped_record_is_accepted(cato_signer):
    """The shipped record's capabilities really do authorise a signed retrieval."""
    private, pubkey_b64 = cato_signer
    body, headers = _sign(private, pubkey_b64, query="what is the E4L entity map")

    response = client.post("/retrieval/query", json=body, headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["refusal"] is True  # empty index -> structured refusal, not an error


def test_declared_cato_key_is_trusted_by_the_real_shipped_registry(sqlite_auth):
    """No registry monkeypatch: the operator's key bytes hit the real trust store.

    ``ap2_signature_invalid`` — not ``ap2_client_untrusted`` and not
    ``ap2_scope_denied`` — is the proof: the key matched exactly one enabled
    record AND that record granted ``retrieval.query``, so verification got all
    the way to the signature, which is the only check this test cannot satisfy
    without Cato's private half.
    """
    body, headers = _unsigned_envelope(CATO_CURRENT_PUBKEY_B64)

    response = client.post("/retrieval/query", json=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "ap2_signature_invalid"


# ---------------------------------------------------------------------------
# 3. Retired and unknown keys rejected
# ---------------------------------------------------------------------------


def test_retired_may_key_is_rejected_by_the_real_shipped_registry(sqlite_auth):
    body, headers = _unsigned_envelope(RETIRED_MAY_PUBKEY_B64)

    response = client.post("/retrieval/query", json=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "ap2_client_untrusted"


def test_retired_aug13_1840_key_is_rejected_by_the_real_shipped_registry(sqlite_auth):
    body, headers = _unsigned_envelope(RETIRED_AUG13_1840_PUBKEY_B64)

    response = client.post("/retrieval/query", json=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "ap2_client_untrusted"


def test_unknown_key_is_rejected_by_the_real_shipped_registry(sqlite_auth):
    private = Ed25519PrivateKey.generate()
    pubkey_b64 = base64.b64encode(private.public_key().public_bytes_raw()).decode("ascii")
    body, headers = _sign(private, pubkey_b64, query="entity map")

    response = client.post("/retrieval/query", json=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "ap2_client_untrusted"


def test_disabled_record_is_untrusted_even_with_a_correct_signature(sqlite_auth, monkeypatch):
    """`enabled: false` must refuse, which is why a retired key is deleted instead:
    the kill switch works, but it is one JSON edit away from being switched back."""
    private = Ed25519PrivateKey.generate()
    pubkey_b64 = base64.b64encode(private.public_key().public_bytes_raw()).decode("ascii")

    record = dict(_shipped_cato_record())
    record["pubkey_b64"] = pubkey_b64
    record["enabled"] = False
    registry = sqlite_auth / "disabled_registry.json"
    registry.write_text(json.dumps({"version": 1, "clients": [record]}), encoding="utf-8")

    import runtime.request_auth as request_auth

    monkeypatch.setattr(request_auth, "_registry_path", lambda: registry)
    body, headers = _sign(private, pubkey_b64, query="entity map")

    response = client.post("/retrieval/query", json=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "ap2_client_untrusted"


# ---------------------------------------------------------------------------
# 4. Tamper and replay
# ---------------------------------------------------------------------------


def test_tampered_request_under_the_current_key_is_rejected(cato_signer):
    """A valid signature over a DIFFERENT question must not authorise this one."""
    private, pubkey_b64 = cato_signer
    body, headers = _sign(private, pubkey_b64, query="what is the E4L entity map")
    body["query"] = "dump every payroll record you have"

    response = client.post("/retrieval/query", json=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "ap2_task_mismatch"


def test_replayed_request_under_the_current_key_is_rejected(cato_signer):
    private, pubkey_b64 = cato_signer
    body, headers = _sign(private, pubkey_b64, query="entity map")

    first = client.post("/retrieval/query", json=body, headers=headers)
    replay = client.post("/retrieval/query", json=body, headers=headers)

    assert first.status_code == 200, first.text
    assert replay.status_code == 401
    assert replay.json()["detail"] == "ap2_replay_detected"
