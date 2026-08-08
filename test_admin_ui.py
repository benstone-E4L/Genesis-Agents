"""Smoke tests for the styled admin HTML page + refund/resolve action endpoints.

Uses the same monkeypatch-in-place pattern as test_admin_auth.py — calling
importlib.reload(main) on Python 3.13 + the pinned FastAPI build trips a
"Router got unexpected on_startup" TypeError from Starlette's lifespan
re-registration, so we import main once and mutate the module-level
ADMIN_EMAILS list around each test.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture(autouse=True)
def _restore_admin_emails():
    """Snapshot + restore main.ADMIN_EMAILS around each test."""
    original = list(main.ADMIN_EMAILS)
    try:
        yield
    finally:
        main.ADMIN_EMAILS[:] = original


def _set_allowlist(*emails: str) -> None:
    main.ADMIN_EMAILS[:] = [e.strip().lower() for e in emails if e.strip()]


# ---------------------------------------------------------------------------
# /admin HTML page
# ---------------------------------------------------------------------------


def test_admin_page_serves_html():
    """GET /admin returns the styled HTML page (no auth gate; the page itself
    handles auth client-side via X-Admin-Email)."""
    client = TestClient(main.app)
    r = client.get("/admin")
    assert r.status_code == 200, r.text
    assert "text/html" in r.headers.get("content-type", "").lower()
    body = r.text
    assert "Genesis Admin" in body
    # Sanity-check that the auth-header instruction is present in the page,
    # because that's the contract the UI relies on.
    assert "X-Admin-Email" in body or "x-admin-email" in body.lower()


def test_admin_page_trailing_slash():
    """The trailing-slash variant also serves the same page."""
    client = TestClient(main.app)
    r = client.get("/admin/")
    assert r.status_code == 200
    assert "Genesis Admin" in r.text


# ---------------------------------------------------------------------------
# /admin/disputes/{job_id}/refund
# ---------------------------------------------------------------------------


def test_admin_refund_rejects_missing_header():
    _set_allowlist("admin@test.com")
    client = TestClient(main.app)
    r = client.post("/admin/disputes/fake-job-id/refund")
    assert r.status_code == 403, r.text


def test_admin_refund_rejects_wrong_email():
    _set_allowlist("admin@test.com")
    client = TestClient(main.app)
    r = client.post(
        "/admin/disputes/fake-job-id/refund",
        headers={"X-Admin-Email": "intruder@example.com"},
    )
    assert r.status_code == 403, r.text


def test_admin_refund_passes_auth_for_allowlisted():
    """Allowlisted admin reaches the handler. With no Postgres in the test
    env, the handler will 404/503/500 — anything except 403 means auth
    let the request through.

    `raise_server_exceptions=False` lets us assert on the response status
    even when job_store raises (no DATABASE_URL in this env)."""
    _set_allowlist("admin@test.com")
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post(
        "/admin/disputes/fake-job-id/refund",
        headers={"X-Admin-Email": "admin@test.com"},
    )
    assert r.status_code != 403, r.text


# ---------------------------------------------------------------------------
# /admin/disputes/{job_id}/resolve
# ---------------------------------------------------------------------------


def test_admin_resolve_rejects_missing_header():
    _set_allowlist("admin@test.com")
    client = TestClient(main.app)
    r = client.post("/admin/disputes/fake-job-id/resolve")
    assert r.status_code == 403, r.text


def test_admin_resolve_rejects_wrong_email():
    _set_allowlist("admin@test.com")
    client = TestClient(main.app)
    r = client.post(
        "/admin/disputes/fake-job-id/resolve",
        headers={"X-Admin-Email": "intruder@example.com"},
    )
    assert r.status_code == 403, r.text


def test_admin_resolve_passes_auth_for_allowlisted():
    """`raise_server_exceptions=False` — see test_admin_refund_passes_auth."""
    _set_allowlist("admin@test.com")
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post(
        "/admin/disputes/fake-job-id/resolve",
        headers={"X-Admin-Email": "admin@test.com"},
    )
    assert r.status_code != 403, r.text
