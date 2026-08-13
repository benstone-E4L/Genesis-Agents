"""Fail-closed browser-admin tests + protected refund/resolve API tests.

Admin authorization is a scope on a verified principal (see test_admin_auth.py).
The browser admin page stays disabled: a page must never hold the service-wide
gateway credential, and no signed HttpOnly admin session flow exists yet.

Uses the same import-main-once pattern as test_admin_auth.py — calling
importlib.reload(main) on Python 3.13 + the pinned FastAPI build trips a
"Router got unexpected on_startup" TypeError from Starlette's lifespan
re-registration.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main

_PRINCIPAL_TOKEN_KEY = "unit-test-principal-token-key-0123456789"
_GATEWAY_HEADERS = {"X-Agent-Api-Key": "test-gateway-key"}


def _admin_headers(*scopes: str) -> dict[str, str]:
    from runtime.request_auth import Principal, issue_principal_token

    token = issue_principal_token(
        Principal(
            principal_id="service:cato",
            tenant_id="e4l",
            client_id="cato",
            scopes=frozenset(scopes or ("admin",)),
            auth_method="ap2",
            expires_at=0,
        ),
        key=_PRINCIPAL_TOKEN_KEY,
    )
    return {**_GATEWAY_HEADERS, "X-Genesis-Principal-Token": token}


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.delenv("AGENT_GATEWAY_SECRET", raising=False)
    monkeypatch.setenv("GENESIS_PRINCIPAL_TOKEN_KEY", _PRINCIPAL_TOKEN_KEY)
    yield


# ---------------------------------------------------------------------------
# /admin HTML page
# ---------------------------------------------------------------------------


def test_admin_page_rejects_unauthenticated_request():
    client = TestClient(main.app)
    r = client.get("/admin")
    assert r.status_code == 401, r.text


def test_admin_page_is_disabled_even_for_authenticated_admin():
    client = TestClient(main.app)
    r = client.get("/admin", headers=_admin_headers("admin"))
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == "admin_ui_disabled_pending_signed_identity"


def test_admin_page_trailing_slash_is_disabled():
    client = TestClient(main.app)
    r = client.get("/admin/", headers=_admin_headers("admin"))
    assert r.status_code == 503
    assert r.json()["detail"] == "admin_ui_disabled_pending_signed_identity"


def test_admin_ui_asset_has_no_browser_auth_or_third_party_script_surface():
    source = (main._STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "X-Agent-Api-Key" not in source
    assert "gateway_key" not in source
    assert "type=\"password\"" not in source
    assert "fetch(" not in source
    assert "<script" not in source.lower()
    assert "cdn." not in source.lower()
    assert "GATEWAY_API_KEY=" not in source
    assert "signed, HttpOnly administrator session" in source


# ---------------------------------------------------------------------------
# /admin/disputes/{job_id}/refund — a real money path
# ---------------------------------------------------------------------------


def test_admin_refund_rejects_gateway_key_alone():
    client = TestClient(main.app)
    r = client.post(
        "/admin/disputes/fake-job-id/refund", headers=_GATEWAY_HEADERS
    )
    assert r.status_code == 403, r.text


def test_admin_refund_rejects_principal_without_admin_scope():
    client = TestClient(main.app)
    r = client.post(
        "/admin/disputes/fake-job-id/refund",
        headers=_admin_headers("agent.invoke", "job.read"),
    )
    assert r.status_code == 403, r.text


def test_admin_refund_passes_auth_for_admin_scope():
    """An admin-scoped principal reaches the handler. With no Postgres in the
    test env the handler will 404/503/500 — anything except 401/403 means auth
    let the request through.

    `raise_server_exceptions=False` lets us assert on the response status even
    when job_store raises (no DATABASE_URL in this env)."""
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post(
        "/admin/disputes/fake-job-id/refund", headers=_admin_headers("admin")
    )
    assert r.status_code not in (401, 403), r.text


# ---------------------------------------------------------------------------
# /admin/disputes/{job_id}/resolve
# ---------------------------------------------------------------------------


def test_admin_resolve_rejects_gateway_key_alone():
    client = TestClient(main.app)
    r = client.post(
        "/admin/disputes/fake-job-id/resolve", headers=_GATEWAY_HEADERS
    )
    assert r.status_code == 403, r.text


def test_admin_resolve_rejects_principal_without_admin_scope():
    client = TestClient(main.app)
    r = client.post(
        "/admin/disputes/fake-job-id/resolve",
        headers=_admin_headers("agent.invoke", "job.read"),
    )
    assert r.status_code == 403, r.text


def test_admin_resolve_passes_auth_for_admin_scope():
    """`raise_server_exceptions=False` — see test_admin_refund_passes_auth."""
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post(
        "/admin/disputes/fake-job-id/resolve", headers=_admin_headers("admin")
    )
    assert r.status_code not in (401, 403), r.text
