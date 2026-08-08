"""Smoke tests for proof_bridge (Phase 7 — VCAP proof bundles)."""
from __future__ import annotations

import base64
import importlib

import pytest


def _gen_test_keypair() -> tuple[str, str]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    k = Ed25519PrivateKey.generate()
    priv_b64 = base64.b64encode(k.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )).decode("ascii")
    pub_b64 = base64.b64encode(k.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )).decode("ascii")
    return priv_b64, pub_b64


def test_unsigned_wrapper(monkeypatch):
    """Without GENESIS_GATEWAY_PRIVKEY_B64, build_vcap_wrapper_jwt emits an
    alg=none token (header.payload.) — last segment empty."""
    monkeypatch.delenv("GENESIS_GATEWAY_PRIVKEY_B64", raising=False)
    import proof_bridge
    importlib.reload(proof_bridge)

    token = proof_bridge.build_vcap_wrapper_jwt({"job_id": "x", "agent_slug": "test"})
    assert token is not None
    parts = token.split(".")
    assert len(parts) == 3
    assert parts[2] == ""


def test_signed_wrapper_roundtrip(monkeypatch):
    """A token signed with a private key must verify under the matching pubkey."""
    priv_b64, pub_b64 = _gen_test_keypair()
    monkeypatch.setenv("GENESIS_GATEWAY_PRIVKEY_B64", priv_b64)
    monkeypatch.setenv("GENESIS_GATEWAY_PUBKEY_B64", pub_b64)

    import proof_bridge
    importlib.reload(proof_bridge)

    payload = {
        "job_id": "test-x",
        "agent_slug": "genesis-research",
        "input_hash": "abc",
    }
    token = proof_bridge.build_vcap_wrapper_jwt(payload)
    assert token is not None

    verify = proof_bridge.verify_vcap_wrapper_jwt(token, pubkey_b64=pub_b64)
    assert verify["ok"]
    assert verify["verified"] is True
    assert verify["payload"]["job_id"] == "test-x"


def test_signed_wrapper_tampered(monkeypatch):
    """If the payload is tampered, verification must fail."""
    priv_b64, pub_b64 = _gen_test_keypair()
    monkeypatch.setenv("GENESIS_GATEWAY_PRIVKEY_B64", priv_b64)

    import proof_bridge
    importlib.reload(proof_bridge)

    token = proof_bridge.build_vcap_wrapper_jwt({"job_id": "x", "agent_slug": "t"})
    assert token is not None
    parts = token.split(".")
    # Tamper the payload portion (force a different b64url-safe byte)
    tampered_payload = "A" + parts[1][1:] if parts[1] else parts[1]
    if tampered_payload == parts[1]:
        tampered_payload = "B" + parts[1][1:]
    tampered = parts[0] + "." + tampered_payload + "." + parts[2]

    verify = proof_bridge.verify_vcap_wrapper_jwt(tampered, pubkey_b64=pub_b64)
    assert verify["ok"] is False
    assert verify["error"] in ("signature_invalid", "payload_decode_failed")
