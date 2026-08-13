"""Genesis production state stores, exercised against a REAL PostgreSQL server.

These tests exist because a mocked connection cannot fail the way production
failed: it has no schema to drift from, no unique constraint to violate, no
advisory lock to contend on, and no transaction to roll back. Every assertion
here is about behaviour that only a real server can demonstrate.

Skips loudly and specifically when no real server is available — see
``tests/conftest.py``. A skip here is an honest "not proven", never a pass.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from runtime import pg_store


def _nonce() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex[:4]


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def test_postgres_is_selected_without_any_sqlite_path(pg_env, monkeypatch):
    """The whole point: no GENESIS_AUTH_DB_PATH, no GENESIS_AUDIT_DB_PATH, no disk."""
    monkeypatch.delenv("GENESIS_STORE_BACKEND", raising=False)
    assert pg_store.backend() == "postgres"
    assert pg_store.postgres_selected() is True


def test_boot_guard_passes_on_a_migrated_database(pg_env, monkeypatch):
    import main

    monkeypatch.setenv("GENESIS_PRINCIPAL_TOKEN_KEY", "k" * 40)
    monkeypatch.setenv("GENESIS_ACTION_GRANT_KEY", "g" * 40)
    monkeypatch.delenv("GENESIS_AUTH_DB_PATH", raising=False)
    main.assert_auth_material_configured()  # must not raise, and must not need a disk


def test_boot_guard_refuses_a_reachable_but_unmigrated_database(pg_env, monkeypatch):
    """The exact production failure: server up, schema absent, requests 500.

    The boot guard must catch this while it is still only a failed boot.
    """
    import main

    monkeypatch.setenv("GENESIS_PRINCIPAL_TOKEN_KEY", "k" * 40)
    monkeypatch.setenv("GENESIS_ACTION_GRANT_KEY", "g" * 40)
    with pg_store.transaction() as cur:
        cur.execute("ALTER TABLE genesis_ap2_nonces RENAME TO genesis_ap2_nonces_hidden")
    try:
        with pytest.raises(RuntimeError, match="genesis_ap2_nonces"):
            main.assert_auth_material_configured()
    finally:
        with pg_store.transaction() as cur:
            cur.execute("ALTER TABLE genesis_ap2_nonces_hidden RENAME TO genesis_ap2_nonces")


# ---------------------------------------------------------------------------
# AP2 replay protection
# ---------------------------------------------------------------------------


def test_nonce_is_single_use_and_a_replay_is_refused(pg_env):
    from runtime.request_auth import AuthenticationError, _consume_nonce

    nonce = _nonce()
    _consume_nonce("cato", nonce, int(time.time()) + 300)

    with pytest.raises(AuthenticationError, match="ap2_replay_detected"):
        _consume_nonce("cato", nonce, int(time.time()) + 300)


def test_the_same_nonce_from_a_different_client_is_not_a_replay(pg_env):
    from runtime.request_auth import _consume_nonce

    nonce = _nonce()
    _consume_nonce("client-a", nonce, int(time.time()) + 300)
    _consume_nonce("client-b", nonce, int(time.time()) + 300)  # must not raise


def test_concurrent_replays_of_one_nonce_admit_exactly_one(pg_env):
    """Two racing requests carrying the same envelope: one wins, one is refused.

    This is the assertion a mock cannot make. Replay protection is a race by
    definition — an attacker replays *fast* — and only a real unique index
    decides it.
    """
    from runtime.request_auth import AuthenticationError, _consume_nonce

    nonce = _nonce()
    expires = int(time.time()) + 300
    barrier = threading.Barrier(8)

    def attempt() -> str:
        barrier.wait(timeout=30)
        try:
            _consume_nonce("cato", nonce, expires)
            return "accepted"
        except AuthenticationError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: attempt(), range(8)))

    assert results.count("accepted") == 1, results
    assert all(r == "ap2_replay_detected" for r in results if r != "accepted"), results


def test_nonce_store_failure_refuses_rather_than_allowing(pg_env, monkeypatch):
    """Fail CLOSED. An unreachable store must never be read as 'no replay seen'."""
    from runtime import request_auth
    from runtime.request_auth import AuthenticationError

    monkeypatch.setenv("GENESIS_JOB_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    with pytest.raises(AuthenticationError, match="ap2_nonce_store_unavailable"):
        request_auth._consume_nonce("cato", _nonce(), int(time.time()) + 300)


def test_expired_nonces_are_swept_but_live_ones_survive(pg_env):
    from runtime.request_auth import AuthenticationError, _consume_nonce

    stale, live = _nonce(), _nonce()
    _consume_nonce("cato", stale, int(time.time()) - 600)
    _consume_nonce("cato", live, int(time.time()) + 600)

    # The sweep runs on the next consume; the stale row goes, the live one stays.
    _consume_nonce("cato", _nonce(), int(time.time()) + 600)
    with pg_store.transaction() as cur:
        cur.execute("SELECT nonce FROM genesis_ap2_nonces WHERE client_id = 'cato'")
        remaining = {row["nonce"] for row in cur.fetchall()}
    assert stale not in remaining
    assert live in remaining
    with pytest.raises(AuthenticationError, match="ap2_replay_detected"):
        _consume_nonce("cato", live, int(time.time()) + 600)


# ---------------------------------------------------------------------------
# Action grants — single use
# ---------------------------------------------------------------------------


def _grant(**overrides):
    from runtime import action_grants

    args = {"repo": "e4l/site", "ref": "main"}
    payload = {
        "principal_id": "cato:ben", "tenant_id": "e4l", "tool": "github_push",
        "args": args, "authorization_id": "auth-1", "key": "k" * 40,
    }
    payload.update(overrides)
    token = action_grants.issue_action_grant(**payload)
    return token, payload


def test_action_grant_is_single_use(pg_env):
    from runtime import action_grants
    from runtime.action_grants import GrantError

    token, p = _grant()
    consume = dict(
        principal_id=p["principal_id"], tenant_id=p["tenant_id"], tool=p["tool"],
        args=p["args"], key=p["key"],
    )
    assert action_grants.consume_action_grant(token, **consume) == "auth-1"
    with pytest.raises(GrantError, match="grant_already_consumed"):
        action_grants.consume_action_grant(token, **consume)


def test_concurrent_grant_redemption_admits_exactly_one(pg_env):
    """A deployment-class authorization must not execute twice under a race."""
    from runtime import action_grants
    from runtime.action_grants import GrantError

    token, p = _grant()
    consume = dict(
        principal_id=p["principal_id"], tenant_id=p["tenant_id"], tool=p["tool"],
        args=p["args"], key=p["key"],
    )
    barrier = threading.Barrier(6)

    def attempt() -> str:
        barrier.wait(timeout=30)
        try:
            action_grants.consume_action_grant(token, **consume)
            return "spent"
        except GrantError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: attempt(), range(6)))

    assert results.count("spent") == 1, results


def test_grant_store_failure_refuses_rather_than_allowing(pg_env, monkeypatch):
    from runtime import action_grants
    from runtime.action_grants import GrantError

    token, p = _grant()
    monkeypatch.setenv("GENESIS_JOB_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    with pytest.raises(GrantError, match="grant_store_unavailable"):
        action_grants.consume_action_grant(
            token, principal_id=p["principal_id"], tenant_id=p["tenant_id"],
            tool=p["tool"], args=p["args"], key=p["key"],
        )


def test_consumed_grant_records_its_owner(pg_env):
    """Ownership is retained so an audit can answer who spent the authorization."""
    from runtime import action_grants

    token, p = _grant()
    action_grants.consume_action_grant(
        token, principal_id=p["principal_id"], tenant_id=p["tenant_id"],
        tool=p["tool"], args=p["args"], key=p["key"],
    )
    with pg_store.transaction() as cur:
        cur.execute("SELECT tenant_id, principal_id, tool FROM genesis_action_grants")
        row = cur.fetchone()
    assert row["tenant_id"] == "e4l"
    assert row["principal_id"] == "cato:ben"
    assert row["tool"] == "github_push"


# ---------------------------------------------------------------------------
# Audit chain — the P0
# ---------------------------------------------------------------------------


def test_audit_chain_links_and_verifies(pg_env):
    from audit import AuditLog

    log = AuditLog()
    assert log.backend == "postgres"
    log.connect()
    for i in range(5):
        log.log("sess-a", "tool_call", f"tool_{i}", {"i": i}, {"ok": True})

    assert log.verify_chain("sess-a") is True
    rows = log.get_session_rows("sess-a")
    assert len(rows) == 5
    assert rows[0]["prev_hash"] == ""
    for previous, current in zip(rows, rows[1:]):
        assert current["prev_hash"] == previous["row_hash"]


def test_concurrent_writers_do_not_fork_the_chain(pg_env):
    """P0. Two writers on one session must produce ONE linear chain.

    Without the per-session advisory lock both readers see the same prev_hash
    and both link to it. Every row still hashes correctly against its own
    prev_hash, so a row-only verification would report a healthy chain over a
    history that has silently branched — which is why verify_chain also checks
    linkage, and why this test asserts on linkage rather than on row hashes.
    """
    from audit import AuditLog

    writers, per_writer = 6, 8
    barrier = threading.Barrier(writers)

    def write(worker: int) -> None:
        log = AuditLog()
        log.connect()
        barrier.wait(timeout=60)
        for i in range(per_writer):
            log.log("sess-race", "tool_call", f"w{worker}_{i}", {"i": i}, {"ok": True})

    with ThreadPoolExecutor(max_workers=writers) as pool:
        list(pool.map(write, range(writers)))

    log = AuditLog()
    log.connect()
    rows = log.get_session_rows("sess-race")
    assert len(rows) == writers * per_writer

    prev_hashes = [r["prev_hash"] for r in rows]
    assert len(set(prev_hashes)) == len(prev_hashes), "chain forked: a prev_hash was reused"
    assert log.verify_chain("sess-race") is True


def test_separate_sessions_keep_independent_chains(pg_env):
    from audit import AuditLog

    log = AuditLog()
    log.connect()
    log.log("sess-x", "tool_call", "t", {}, {"ok": True})
    log.log("sess-y", "tool_call", "t", {}, {"ok": True})
    log.log("sess-x", "tool_call", "t", {}, {"ok": True})

    assert log.verify_chain("sess-x") is True
    assert log.verify_chain("sess-y") is True
    assert log.get_session_rows("sess-y")[0]["prev_hash"] == ""


def test_a_tampered_row_is_detected(pg_env):
    from audit import AuditLog

    log = AuditLog()
    log.connect()
    log.log("sess-t", "tool_call", "t", {}, {"ok": True})
    log.log("sess-t", "tool_call", "t", {}, {"ok": True})
    assert log.verify_chain("sess-t") is True

    with pg_store.transaction() as cur:
        cur.execute(
            "UPDATE genesis_audit_log SET cost_cents = 999 WHERE session_id = 'sess-t' "
            "AND id = (SELECT MIN(id) FROM genesis_audit_log WHERE session_id = 'sess-t')"
        )
    assert log.verify_chain("sess-t") is False


def test_a_forked_chain_is_detected_even_though_each_row_hashes_correctly(pg_env):
    """Proves the linkage check earns its place.

    A row inserted with a *correct* row_hash over a *stale* prev_hash is exactly
    what a lost advisory lock would produce. Row-level verification alone passes
    it; the chain is still broken.
    """
    import audit
    from audit import AuditLog

    log = AuditLog()
    log.connect()
    log.log("sess-f", "tool_call", "a", {}, {"ok": True})
    log.log("sess-f", "tool_call", "b", {}, {"ok": True})
    rows = log.get_session_rows("sess-f")

    # Forge a third row that links to row 1 instead of row 2, hashed correctly.
    ts = time.time()
    with pg_store.transaction() as cur:
        cur.execute(
            "INSERT INTO genesis_audit_log (session_id, action_type, tool_name, inputs_json,"
            " outputs_json, cost_cents, error, timestamp, prev_hash, row_hash, inputs_digest,"
            " outputs_digest, schema_version)"
            " VALUES ('sess-f','tool_call','forged','{}','{}',0,'',%s,%s,'','d','d',2)"
            " RETURNING id",
            (ts, rows[0]["row_hash"]),
        )
        forged_id = int(cur.fetchone()["id"])
        forged_hash = audit._row_hash(
            forged_id, "sess-f", "tool_call", "forged", 0, ts, rows[0]["row_hash"], "d", "d"
        )
        cur.execute(
            "UPDATE genesis_audit_log SET row_hash = %s WHERE id = %s", (forged_hash, forged_id)
        )

    assert log.verify_chain("sess-f") is False


def test_audit_row_survives_the_float_timestamp_round_trip(pg_env):
    """The chain binds the timestamp, so a lossy column would break every hash."""
    from audit import AuditLog

    log = AuditLog()
    log.connect()
    log.log("sess-ts", "tool_call", "t", {}, {"ok": True})
    stored = log.get_session_rows("sess-ts")[0]["timestamp"]
    assert isinstance(stored, float)
    assert log.verify_chain("sess-ts") is True


def test_audit_intent_is_written_before_the_side_effect(pg_env):
    """Intent-before-execution ordering must survive the move to Postgres."""
    from runtime import genesis_audit

    intent_id = genesis_audit.append_tool_intent(
        session_id="sess-i", tool_name="github_push", inputs={"repo": "e4l/site"}
    )
    event_id = genesis_audit.append_tool_event(
        session_id="sess-i", tool_name="github_push",
        inputs={"repo": "e4l/site"}, outputs={"ok": True},
    )
    assert intent_id < event_id

    rows = genesis_audit.get_audit_log().get_session_rows("sess-i")
    assert [r["action_type"] for r in rows] == ["tool_intent", "tool_call"]
    assert genesis_audit.get_audit_log().verify_chain("sess-i") is True


def test_audit_export_and_summary_work_on_postgres(pg_env):
    from audit import AuditLog

    log = AuditLog()
    log.connect()
    log.log("sess-e", "tool_call", "alpha", {}, {"ok": True}, cost_cents=3)
    log.log("sess-e", "tool_call", "beta", {}, {"ok": False}, cost_cents=4, error="boom")

    summary = log.session_summary("sess-e")
    assert summary["action_count"] == 2
    assert summary["total_cost_cents"] == 7
    assert summary["errors"] == 1
    assert summary["tools_used"] == ["alpha", "beta"]

    csv_export = log.export_session("sess-e", fmt="csv")
    assert "alpha" in csv_export and "beta" in csv_export
    assert csv_export.splitlines()[0].startswith("id,session_id,action_type")

    jsonl = log.export_session("sess-e", fmt="jsonl")
    assert len(jsonl.splitlines()) == 2


def test_audit_secrets_are_redacted_before_they_are_stored(pg_env):
    from audit import AuditLog

    log = AuditLog()
    log.connect()
    log.log(
        "sess-r", "tool_call", "http",
        {"api_key": "sk-abcdefghijklmnopqrstuvwxyz012345"},
        {"ok": True, "authorization": "Bearer abcdefghijklmnop"},
    )
    row = log.get_session_rows("sess-r")[0]
    blob = row["inputs_json"] + row["outputs_json"]
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in blob
    assert "abcdefghijklmnop" not in blob


# ---------------------------------------------------------------------------
# Merkle anchors
# ---------------------------------------------------------------------------


def test_anchor_round_trips_through_postgres(pg_env):
    from datetime import datetime, timezone

    from anchor_logger import AnchorLogger
    from audit import AuditLog

    log = AuditLog()
    log.connect()
    for i in range(3):
        log.log("sess-anchor", "tool_call", f"t{i}", {}, {"ok": True})

    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    anchor_log = AnchorLogger()
    anchor = anchor_log.compute_daily_anchor(today)
    assert anchor["action_count"] == 3
    assert anchor["session_count"] == 1

    anchor_log.record_anchor(anchor)
    stored = anchor_log.get_anchor(today)
    assert stored is not None
    assert stored["merkle_root"] == anchor["merkle_root"]
    assert stored["leaf_hashes"] == anchor["leaf_hashes"]

    verified = anchor_log.verify_anchor(today)
    assert verified["match"] is True and verified["tampered"] is False


def test_anchor_detects_tampering_of_the_underlying_chain(pg_env):
    """Root check only: overwrites ``row_hash``, the value the leaves ARE.

    This proves the Merkle comparison works. It does NOT prove content tampering
    is detected — see
    ``test_anchor_detects_content_edit_that_leaves_row_hash_intact`` for that,
    which is the case an attacker with UPDATE would actually use.
    """
    from datetime import datetime, timezone

    from anchor_logger import AnchorLogger
    from audit import AuditLog

    log = AuditLog()
    log.connect()
    log.log("sess-anchor2", "tool_call", "t", {}, {"ok": True})

    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    anchor_log = AnchorLogger()
    anchor_log.record_anchor(anchor_log.compute_daily_anchor(today))

    with pg_store.transaction() as cur:
        cur.execute("UPDATE genesis_audit_log SET row_hash = 'tampered' WHERE id > 0")

    result = anchor_log.verify_anchor(today)
    assert result["match"] is False
    assert result["tampered"] is True


def test_anchor_detects_content_edit_that_leaves_row_hash_intact(pg_env):
    """The realistic tamper: edit the row, do NOT touch the hash it stores.

    The Merkle leaves are the recorded ``row_hash`` values, so rewriting
    ``tool_name`` alone leaves every leaf — and therefore the root — unchanged.
    ``test_anchor_detects_tampering_of_the_underlying_chain`` above overwrites
    ``row_hash`` itself, which is the one field the anchor commits to, so it can
    only ever prove that the anchor notices a change to its own leaves. An
    attacker with UPDATE on the audit table would edit the evidence, not the
    checksum of the evidence, and ``verify_anchor`` reported ``tampered: False``
    for exactly that case while its docstring claimed it verified integrity "by
    recomputing the Merkle root from the live DB".
    """
    from datetime import datetime, timezone

    from anchor_logger import AnchorLogger
    from audit import AuditLog

    log = AuditLog()
    log.connect()
    log.log("sess-anchor3", "tool_call", "browser.navigate", {}, {"ok": True})

    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    anchor_log = AnchorLogger()
    anchor_log.record_anchor(anchor_log.compute_daily_anchor(today))
    assert anchor_log.verify_anchor(today)["match"] is True

    with pg_store.transaction() as cur:
        cur.execute("UPDATE genesis_audit_log SET tool_name = 'send_payment' WHERE id > 0")

    result = anchor_log.verify_anchor(today)
    # The root is untouched by design; the row no longer hashes to its leaf.
    assert result["stored_root"] == result["recomputed_root"]
    assert result["rows_intact"] is False
    assert result["broken_sessions"] == ["sess-anchor3"]
    assert result["tampered"] is True
    assert result["match"] is False


def test_anchor_store_keeps_a_second_differing_anchor_for_a_date(pg_env):
    """A conflicting anchor is evidence, so it must not be collapsed or upserted."""
    from anchor_logger import AnchorLogger

    anchor_log = AnchorLogger()
    first = {"date": "2026-01-01", "session_count": 1, "action_count": 1,
             "leaf_hashes": ["a"], "merkle_root": "root-a", "computed_at": "t1"}
    second = dict(first, merkle_root="root-b", computed_at="t2")
    anchor_log.record_anchor(first)
    anchor_log.record_anchor(second)

    # get_anchor returns the FIRST recorded, matching the JSONL reader it replaced.
    assert anchor_log.get_anchor("2026-01-01")["merkle_root"] == "root-a"
    roots = [a["merkle_root"] for a in anchor_log.list_anchors(limit=10)]
    assert "root-a" in roots and "root-b" in roots


# ---------------------------------------------------------------------------
# Tenant ownership
# ---------------------------------------------------------------------------


def test_tenant_ownership_survives_on_postgres_job_rows(pg_env):
    import job_store
    from runtime.request_auth import Principal, legacy_gateway_principal, owns_resource

    owner = Principal(
        principal_id="cato:ben", tenant_id="e4l", client_id="cato",
        scopes=frozenset({"agent.invoke", "job.read"}), auth_method="ap2",
        expires_at=int(time.time()) + 300,
    )
    other = Principal(
        principal_id="someone:else", tenant_id="other", client_id="x",
        scopes=frozenset({"job.read"}), auth_method="ap2", expires_at=int(time.time()) + 300,
    )

    created = job_store.create_job(
        agent_slug="genesis-research", prompt="scoped",
        tenant_id="e4l", owner_principal_id="cato:ben",
    )
    row = job_store.get_job(created["id"])
    assert owns_resource(owner, row) is True
    assert owns_resource(other, row) is False
    # A tenant-scoped row is NOT readable by the shared-key legacy principal.
    assert owns_resource(legacy_gateway_principal(), row) is False


def test_child_jobs_inherit_tenant_ownership(pg_env):
    import job_store
    from runtime.request_auth import Principal, owns_resource

    parent = job_store.create_job(
        agent_slug="genesis-meta", prompt="parent",
        tenant_id="e4l", owner_principal_id="cato:ben",
    )
    child_id = f"child-{uuid.uuid4().hex[:12]}"
    job_store.create_child_job(
        child_job_id=child_id, agent_slug="genesis-pricing", prompt="child",
        parent_job_id=parent["id"],
    )
    child = job_store.get_job(child_id)
    assert child["tenantId"] == "e4l"
    assert child["ownerPrincipalId"] == "cato:ben"

    owner = Principal(
        principal_id="cato:ben", tenant_id="e4l", client_id="cato",
        scopes=frozenset({"job.read"}), auth_method="ap2", expires_at=int(time.time()) + 300,
    )
    assert owns_resource(owner, child) is True


# ---------------------------------------------------------------------------
# Stale-job reaper — the durable transition log must stay reconstructable
# ---------------------------------------------------------------------------


def test_stale_expiry_records_the_status_it_expired_from(pg_env, monkeypatch):
    """A reaped job's event row must say RUNNING -> EXPIRED, not (null) -> EXPIRED.

    ``genesis_job_events`` is the durable, append-only lifecycle record. The
    normal transition path writes ``fromStatus``; the reaper did not, so a job
    killed mid-execution left QUEUED->RUNNING followed by a bare EXPIRED with no
    predecessor. Anyone reconstructing the lifecycle from the event log alone —
    a settlement decision, a dispute, an incident review — cannot tell whether
    that job ever ran. The reaper's own WHERE clause is ``status = 'RUNNING'``,
    so the predecessor is never in doubt.
    """
    import job_store

    monkeypatch.setenv("GENESIS_JOB_DATABASE_URL", pg_env)

    job = job_store.create_job(agent_slug="genesis-content", prompt="reaper probe",
                               tenant_id="e4l", owner_principal_id="service:cato")
    assert job_store.claim_job_by_id(job["id"])["status"] == "RUNNING"

    assert job_store.expire_stale_running_jobs(stale_minutes=0) >= 1

    with pg_store.transaction() as cur:
        cur.execute(
            'SELECT "fromStatus", "toStatus" FROM genesis_job_events WHERE "jobId" = %s'
            ' ORDER BY "createdAt"', (job["id"],))
        transitions = [(r["fromStatus"], r["toStatus"]) for r in cur.fetchall()]

    assert transitions[-1] == ("RUNNING", "EXPIRED"), transitions
