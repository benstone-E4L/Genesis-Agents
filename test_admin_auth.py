"""Authorization tests for /admin/disputes.

Admin is a *scope on a verified principal*, not a self-asserted header. Every
admin route requires BOTH the shared gateway credential and a signed principal
token carrying the ``admin`` scope, so neither credential alone is sufficient.

Avoids importlib.reload(main) because that re-registers the FastAPI/Starlette
lifespan and trips an upstream "Router got unexpected on_startup" TypeError
on Python 3.13 + the pinned FastAPI build.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main

_PRINCIPAL_TOKEN_KEY = "unit-test-principal-token-key-0123456789"
_GATEWAY_HEADERS = {"X-Agent-Api-Key": "test-gateway-key"}


def _token(*scopes: str, key: str = _PRINCIPAL_TOKEN_KEY) -> str:
    from runtime.request_auth import Principal, issue_principal_token

    return issue_principal_token(
        Principal(
            principal_id="service:cato",
            tenant_id="e4l",
            client_id="cato",
            scopes=frozenset(scopes),
            auth_method="ap2",
            expires_at=0,
        ),
        key=key,
    )


def _admin_headers(*scopes: str, key: str = _PRINCIPAL_TOKEN_KEY) -> dict[str, str]:
    return {**_GATEWAY_HEADERS, "X-Genesis-Principal-Token": _token(*scopes, key=key)}


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.delenv("AGENT_GATEWAY_SECRET", raising=False)
    monkeypatch.setenv("GENESIS_PRINCIPAL_TOKEN_KEY", _PRINCIPAL_TOKEN_KEY)
    yield


def test_admin_auth_rejects_gateway_key_alone():
    """The shared key resolves to the legacy principal, which has no admin scope."""
    client = TestClient(main.app)
    resp = client.get("/admin/disputes", headers=_GATEWAY_HEADERS)
    assert resp.status_code == 403, resp.text


def test_admin_auth_rejects_principal_without_admin_scope():
    client = TestClient(main.app)
    resp = client.get("/admin/disputes", headers=_admin_headers("agent.invoke"))
    assert resp.status_code == 403, resp.text


def test_admin_auth_accepts_admin_scoped_principal():
    """An admin-scoped principal passes the auth gate.

    The handler will then fail (no Postgres in the test env), but the status
    MUST NOT be 401/403 — anything else means auth let it through.
    """
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.get("/admin/disputes", headers=_admin_headers("admin"))
    assert resp.status_code not in (401, 403), resp.text


def test_admin_auth_has_no_hardcoded_identity():
    """No published email address may become an implicit admin credential."""
    from pathlib import Path

    source = Path(main.__file__).read_text(encoding="utf-8")
    assert "_DEFAULT_ADMIN_EMAILS" not in source
    assert "bullrushinvestments@gmail.com" not in source


def test_admin_auth_has_no_self_asserted_email_gate():
    """The removed X-Admin-Email mechanism must not silently come back.

    An unsigned, caller-chosen header is not a credential; leaving the
    allowlist in place would imply a control that no longer runs.
    """
    from pathlib import Path

    source = Path(main.__file__).read_text(encoding="utf-8")
    # Prose may explain the removal; no code may re-read the header or allowlist.
    assert 'alias="x-admin-email"' not in source.lower()
    assert "SWARMSYNC_ADMIN_EMAILS" not in source
    assert not hasattr(main, "ADMIN_EMAILS")


def test_admin_auth_requires_gateway_credential_even_with_admin_scope():
    """Neither credential alone opens an admin route."""
    client = TestClient(main.app)
    resp = client.get(
        "/admin/disputes",
        headers={"X-Genesis-Principal-Token": _token("admin")},
    )
    assert resp.status_code == 401, resp.text


def test_admin_auth_rejects_forged_admin_token():
    """An admin scope is only meaningful when the signature verifies."""
    client = TestClient(main.app)
    resp = client.get(
        "/admin/disputes",
        headers=_admin_headers("admin", key="an-entirely-different-key-0123456789abcd"),
    )
    assert resp.status_code == 401, resp.text
