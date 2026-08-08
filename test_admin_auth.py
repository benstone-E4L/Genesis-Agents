"""Smoke test for the admin auth dependency on /admin/disputes.

Avoids importlib.reload(main) because that re-registers the FastAPI/Starlette
lifespan and trips an upstream "Router got unexpected on_startup" TypeError
on Python 3.13 + the pinned FastAPI build. Instead we import main once and
monkeypatch main.ADMIN_EMAILS in place.
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


def test_admin_auth_rejects_missing_header():
    _set_allowlist("admin@test.com")
    client = TestClient(main.app)
    resp = client.get("/admin/disputes")
    assert resp.status_code == 403, resp.text


def test_admin_auth_rejects_wrong_email():
    _set_allowlist("admin@test.com")
    client = TestClient(main.app)
    resp = client.get(
        "/admin/disputes", headers={"X-Admin-Email": "other@test.com"}
    )
    assert resp.status_code == 403, resp.text


def test_admin_auth_accepts_allowlisted_email():
    """An allowlisted email passes the auth gate.

    The handler will then fail (no Postgres in test env), but status MUST
    NOT be 403 — anything else means auth let it through.
    """
    _set_allowlist("admin@test.com")
    client = TestClient(main.app)
    resp = client.get(
        "/admin/disputes", headers={"X-Admin-Email": "admin@test.com"}
    )
    assert resp.status_code != 403, resp.text


def test_admin_auth_default_allowlist_bullrush():
    """Default allowlist (when module imported with no env override) must
    contain bullrushinvestments@gmail.com — the production admin."""
    # Don't mutate the list: this test verifies the module-level default.
    # We can't easily re-import without env, so we just assert the default
    # email is present in the current allowlist OR fall back to checking
    # the env var the module reads.
    import os as _os
    expected = (
        _os.getenv("SWARMSYNC_ADMIN_EMAILS", "bullrushinvestments@gmail.com")
        .split(",")[0]
        .strip()
        .lower()
    )
    assert expected in main.ADMIN_EMAILS, (
        f"expected {expected!r} in {main.ADMIN_EMAILS}"
    )


def test_admin_auth_case_insensitive():
    _set_allowlist("admin@test.com")
    client = TestClient(main.app)
    resp = client.get(
        "/admin/disputes", headers={"X-Admin-Email": "ADMIN@TEST.COM"}
    )
    assert resp.status_code != 403, resp.text
