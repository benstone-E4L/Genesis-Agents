"""Smoke test for conduit_sessions."""
import base64, os, pytest


def test_vault_not_configured_returns_error(monkeypatch):
    monkeypatch.delenv("GENESIS_SESSION_VAULT_KEY", raising=False)
    import importlib
    import conduit_sessions
    importlib.reload(conduit_sessions)

    r = conduit_sessions.store_session(job_id="test", session_data={"cookies": []})
    assert r["ok"] is False
    assert r["error"] == "vault_not_configured"


def test_roundtrip_with_key(monkeypatch, tmp_path):
    import base64, secrets
    key_b64 = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    monkeypatch.setenv("GENESIS_SESSION_VAULT_KEY", key_b64)
    monkeypatch.setenv("GENESIS_SESSION_VAULT_DIR", str(tmp_path))

    import importlib
    import conduit_sessions
    importlib.reload(conduit_sessions)

    test_data = {"cookies": [{"name": "auth", "value": "secret"}]}
    r1 = conduit_sessions.store_session(job_id="job-x", session_data=test_data)
    assert r1["ok"]

    r2 = conduit_sessions.load_session(job_id="job-x")
    assert r2["ok"]
    assert r2["session_data"] == test_data

    r3 = conduit_sessions.delete_session(job_id="job-x")
    assert r3["ok"]
    assert r3["deleted"] is True

    r4 = conduit_sessions.load_session(job_id="job-x")
    assert r4["ok"] is False
    assert r4["error"] == "session_not_found"
