"""K-05 — the owner-scoped principal token Genesis mints must be sufficient to read its own job.

Cato's `_poll_job` (cato/tools/genesis.py) deliberately sends ONLY `X-Genesis-Principal-Token`.
The shared GATEWAY_API_KEY is omni-privilege (any holder reads any job) and `poll_url` arrives
inside an untrusted response body, so Cato refuses to send the shared secret to a URL the
response chose. That least-privilege posture is correct and is pinned by Cato's own tests, so
the contract has to hold on THIS side.

Scope note on the original report: `GET /agents/jobs/{job_id}` and `/artifacts` ALREADY honoured
the token (they use verify_continuation_principal). The genuine gaps were the sibling read
routes — /events, /trace, /sandbox — which were gated on the gateway key alone. Both are pinned
here so neither can regress.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_TOKEN_KEY = "unit-test-principal-token-key-0123456789"

# The token-readable side channels a poller legitimately needs. The two POST /sandbox routes are
# deliberately NOT here: they mutate sandbox state, and a read-scoped polling token must not be
# able to create or destroy a sandbox.
READ_ROUTES = (
    "/agents/jobs/{job_id}",
    "/agents/jobs/{job_id}/events",
    "/agents/jobs/{job_id}/trace",
    "/agents/jobs/{job_id}/sandbox",
    "/agents/jobs/{job_id}/artifacts",
)


def _owned_job(job_id="job-owned"):
    return {
        "id": job_id, "status": "QUEUED", "agentSlug": "genesis-research",
        "tenantId": "e4l", "ownerPrincipalId": "service:cato",
        "prompt": "p", "params": {}, "outputArtifactUris": [],
    }


@pytest.fixture()
def app_client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.delenv("AGENT_GATEWAY_SECRET", raising=False)
    monkeypatch.setenv("GENESIS_PRINCIPAL_TOKEN_KEY", _TOKEN_KEY)
    monkeypatch.setenv("GENESIS_AUTH_DB_PATH", tmp + "/auth.db")
    monkeypatch.setenv("GENESIS_ACTION_GRANT_KEY", "g" * 40)

    import main

    jobs = {
        "job-owned": _owned_job(),
        "job-other": {**_owned_job("job-other"), "ownerPrincipalId": "service:someone-else"},
        "job-other-tenant": {**_owned_job("job-other-tenant"), "tenantId": "other-tenant"},
        # The F-TENANT-01 shape: a legacy row with NO owner at all.
        "job-unowned": {**_owned_job("job-unowned"), "tenantId": None, "ownerPrincipalId": None},
    }
    monkeypatch.setattr(main, "get_job", lambda jid: dict(jobs[jid]) if jid in jobs else None)
    monkeypatch.setattr(main, "_JOB_STORE_OK", True)
    return TestClient(main.app), jobs


def _token(scopes=("job.read", "artifact.read"), principal_id="service:cato", tenant_id="e4l"):
    from runtime.request_auth import Principal, issue_principal_token

    return issue_principal_token(
        Principal(
            principal_id=principal_id, tenant_id=tenant_id, client_id="cato",
            scopes=frozenset(scopes), auth_method="ap2", expires_at=0,
        ),
        key=_TOKEN_KEY,
    )


# ---------------------------------------------------------------------------
# The contract, from Cato's side: token alone, no gateway key anywhere.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", READ_ROUTES)
def test_principal_token_alone_reads_its_own_job(app_client, route):
    """No X-Agent-Api-Key is sent — exactly what Cato's _poll_job does."""
    client, _ = app_client

    response = client.get(
        route.format(job_id="job-owned"),
        headers={"X-Genesis-Principal-Token": _token()},
    )

    assert response.status_code not in (401, 403), (
        f"{route} refused the owner-scoped token Genesis itself minted: "
        f"{response.status_code} {response.text[:200]}"
    )


def test_token_only_poll_of_a_queued_job_returns_the_job_not_401(app_client):
    """The exact call Cato makes: poll_url + principal token, nothing else.

    A 401 here is what turns a perfectly good queued job into a ledger INDETERMINATE that a
    human has to reconcile.
    """
    client, _ = app_client

    response = client.get(
        "/agents/jobs/job-owned", headers={"X-Genesis-Principal-Token": _token()}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "job-owned"
    assert body["status"] == "QUEUED"


# ---------------------------------------------------------------------------
# Least privilege: a token reads ONLY its own job, and says 403, not 404.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", READ_ROUTES)
@pytest.mark.parametrize("foreign", ["job-other", "job-other-tenant"])
def test_principal_token_cannot_read_another_principals_job(app_client, route, foreign):
    client, _ = app_client

    response = client.get(
        route.format(job_id=foreign), headers={"X-Genesis-Principal-Token": _token()}
    )

    assert response.status_code == 403, (
        f"{route} leaked {foreign} to a non-owner: {response.status_code}"
    )
    assert response.json()["detail"] == "resource owner mismatch"


@pytest.mark.parametrize("route", READ_ROUTES)
def test_f_tenant_01_is_not_reopened_by_the_side_channel_routes(app_client, route):
    """Cross-check against the failure-mode audit.

    owns_resource treats a row with BOTH tenantId and ownerPrincipalId NULL as belonging to the
    legacy gateway principal. These routes must not hand such a row to a token principal — that
    is the same hole F-TENANT-01 closed on delegated child jobs, arriving by another door.
    """
    client, _ = app_client

    response = client.get(
        route.format(job_id="job-unowned"), headers={"X-Genesis-Principal-Token": _token()}
    )

    assert response.status_code == 403


@pytest.mark.parametrize("route", READ_ROUTES)
def test_token_without_the_read_scope_is_refused(app_client, route):
    client, _ = app_client

    response = client.get(
        route.format(job_id="job-owned"),
        headers={"X-Genesis-Principal-Token": _token(scopes=("agent.invoke",))},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "principal scope denied"


# ---------------------------------------------------------------------------
# Fail closed on bad tokens.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", READ_ROUTES)
@pytest.mark.parametrize(
    "bad_token,expected",
    [
        ("genesis-principal-v1.garbage.garbage", "principal_token_signature_invalid"),
        ("not-even-a-token", "principal_token_invalid"),
    ],
)
def test_forged_token_fails_closed(app_client, route, bad_token, expected):
    client, _ = app_client

    response = client.get(
        route.format(job_id="job-owned"), headers={"X-Genesis-Principal-Token": bad_token}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == expected


@pytest.mark.parametrize("route", READ_ROUTES)
def test_expired_token_fails_closed(app_client, route, monkeypatch):
    """Signed by the right key, but past its TTL — must not read anything."""
    import time

    from runtime.request_auth import Principal, issue_principal_token

    stale = issue_principal_token(
        Principal(
            principal_id="service:cato", tenant_id="e4l", client_id="cato",
            scopes=frozenset({"job.read", "artifact.read"}), auth_method="ap2", expires_at=0,
        ),
        key=_TOKEN_KEY,
        now=int(time.time()) - 100_000,
    )
    client, _ = app_client

    response = client.get(
        route.format(job_id="job-owned"), headers={"X-Genesis-Principal-Token": stale}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "principal_token_expired"


# ---------------------------------------------------------------------------
# The gateway-key path must keep working for existing callers.
# ---------------------------------------------------------------------------

# The gateway key's reach is deliberately asymmetric across these routes, and this fix must not
# change either half. /agents/jobs/{id} and /artifacts already enforce ownership, so the shared
# key cannot read an AP2-owned row (trusted_ap2_clients.README.md: "the shared gateway key
# remains a legacy compatibility credential but cannot read AP2-owned rows"). The side-channel
# routes never enforced ownership, and tightening them here would break current callers.
_OWNERSHIP_ENFORCED_FOR_GATEWAY_KEY = (
    "/agents/jobs/{job_id}",
    "/agents/jobs/{job_id}/artifacts",
)
_SIDE_CHANNEL_ROUTES = (
    "/agents/jobs/{job_id}/events",
    "/agents/jobs/{job_id}/trace",
    "/agents/jobs/{job_id}/sandbox",
)


@pytest.mark.parametrize("route", _SIDE_CHANNEL_ROUTES)
def test_gateway_key_path_on_side_channels_is_unchanged(app_client, route):
    """legacy_gateway_principal() carries no job.read scope, so routing it through
    _require_owned_job would have 403'd every existing caller. It must not."""
    client, _ = app_client

    response = client.get(
        route.format(job_id="job-owned"), headers={"X-Agent-Api-Key": "test-gateway-key"}
    )

    assert response.status_code not in (401, 403), (
        f"{route} broke the existing gateway-key caller: {response.status_code}"
    )


@pytest.mark.parametrize("route", _OWNERSHIP_ENFORCED_FOR_GATEWAY_KEY)
def test_gateway_key_still_cannot_read_an_ap2_owned_row(app_client, route):
    """Pre-existing, deliberate, and preserved: the omni-privilege key is not a way around
    tenant scoping on the routes that already enforce it."""
    client, _ = app_client

    response = client.get(
        route.format(job_id="job-owned"), headers={"X-Agent-Api-Key": "test-gateway-key"}
    )

    assert response.status_code == 403


@pytest.mark.parametrize("route", _OWNERSHIP_ENFORCED_FOR_GATEWAY_KEY)
def test_gateway_key_still_reads_legacy_unowned_rows(app_client, route):
    """The compatibility path that keeps pre-AP2 callers working."""
    client, _ = app_client

    response = client.get(
        route.format(job_id="job-unowned"), headers={"X-Agent-Api-Key": "test-gateway-key"}
    )

    assert response.status_code not in (401, 403)


@pytest.mark.parametrize("route", READ_ROUTES)
def test_no_credential_at_all_is_refused(app_client, route):
    client, _ = app_client

    response = client.get(route.format(job_id="job-owned"))

    assert response.status_code == 401


def test_side_channel_routes_fail_closed_when_ownership_cannot_be_proven(app_client, monkeypatch):
    """If the job store is down we cannot prove ownership. Serve nothing rather than guess."""
    import main

    client, _ = app_client
    monkeypatch.setattr(main, "_JOB_STORE_OK", False)

    for route in ("/agents/jobs/job-owned/events", "/agents/jobs/job-owned/trace"):
        response = client.get(route, headers={"X-Genesis-Principal-Token": _token()})
        assert response.status_code == 503, route


def test_sandbox_mutating_routes_still_require_the_gateway_key(app_client):
    """A read-scoped polling token must not be able to create or destroy a sandbox."""
    client, _ = app_client
    headers = {"X-Genesis-Principal-Token": _token()}

    assert client.post("/agents/jobs/job-owned/sandbox", headers=headers, json={}).status_code == 401
    assert client.post(
        "/agents/jobs/job-owned/sandbox/destroy", headers=headers, json={}
    ).status_code == 401


# ---------------------------------------------------------------------------
# F-LEGACY-01 — the documented compatibility path was unreachable
# ---------------------------------------------------------------------------

def test_f_legacy_01_legacy_principal_can_reach_the_ownership_check_at_all():
    """owns_resource has a branch that returns True for a legacy principal on an unowned row.
    That branch was dead: _require_owned_job refused on the scope check first, so a gateway-key
    caller got 403 "principal scope denied" even on a pre-AP2 job it was meant to be able to
    read. Grant the read scopes so the intended branch is reachable."""
    from runtime.request_auth import legacy_gateway_principal, owns_resource

    legacy = legacy_gateway_principal()

    assert legacy.has_scope("job.read")
    assert legacy.has_scope("artifact.read")
    assert owns_resource(legacy, {"tenantId": None, "ownerPrincipalId": None}) is True


def test_f_legacy_01_grant_does_not_widen_access_to_owned_rows():
    """The whole point of the compatibility identity: it still cannot read AP2-owned rows,
    and it still cannot administer."""
    from runtime.request_auth import legacy_gateway_principal, owns_resource

    legacy = legacy_gateway_principal()

    assert owns_resource(legacy, {"tenantId": "e4l", "ownerPrincipalId": "service:cato"}) is False
    assert legacy.has_scope("admin") is False
