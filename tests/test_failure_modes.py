"""JOB 2 — proving tests for the failure-mode audit findings.

Every test here fails against the pre-fix code. They are grouped by finding id so the audit
table and the suite stay in step.

No live Postgres: job_store is exercised through a recording fake connection shaped like
psycopg's dict_row cursor, which is how the rest of this repo tests SQL construction
(see test_retrieval_route.py's fake psycopg connection).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Fake psycopg plumbing
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.executed: list[tuple[str, tuple | list | None]] = []
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last = self._responses.pop(0) if self._responses else None

    def fetchone(self):
        return self._last if not isinstance(self._last, list) else (self._last[0] if self._last else None)

    def fetchall(self):
        return self._last if isinstance(self._last, list) else ([] if self._last is None else [self._last])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, responses: list):
        self.cur = _FakeCursor(responses)

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def job_store_mod(monkeypatch):
    import job_store

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")
    return job_store


def _wire(monkeypatch, job_store, responses):
    conn = _FakeConn(responses)
    monkeypatch.setattr(job_store, "_conn", lambda: conn)
    return conn


# ---------------------------------------------------------------------------
# F-AUTH-01 — unicode prompt in the AP2 binding check raised TypeError -> HTTP 500
# ---------------------------------------------------------------------------

def test_f_auth_01_non_ascii_prompt_binds_without_a_type_error():
    """hmac.compare_digest rejects non-ASCII `str`. verify_agent_principal only catches
    AuthenticationError, so "café" in a prompt used to escape as a 500, not a 401/200."""
    import main

    prompt = "Résumé the Q3 clôture — 100% précis, no guesses →"
    body = {
        "payload": {"agent": "genesis-finance", "task": prompt, "params": {}},
        "prompt": prompt,
        "task": {},
    }

    main.assert_envelope_binds_request(body, "genesis-finance")  # must not raise


def test_f_auth_01_non_ascii_tamper_is_still_refused():
    """The unicode fix must not weaken the check into accepting anything."""
    import main
    from runtime.request_auth import AuthenticationError

    body = {
        "payload": {"agent": "genesis-finance", "task": "clôture Q3", "params": {}},
        "prompt": "clôture Q4",
        "task": {},
    }

    with pytest.raises(AuthenticationError, match="ap2_task_mismatch"):
        main.assert_envelope_binds_request(body, "genesis-finance")


# ---------------------------------------------------------------------------
# F-TENANT-01 — delegated child jobs were created with NULL tenant/owner
# ---------------------------------------------------------------------------

def test_f_tenant_01_child_job_inherits_parent_tenant_and_owner(monkeypatch, job_store_mod):
    conn = _wire(
        monkeypatch,
        job_store_mod,
        [{"tenantId": "e4l", "ownerPrincipalId": "service:cato"}, None, None],
    )

    job_store_mod.create_child_job(
        child_job_id="child-1", agent_slug="genesis-research",
        prompt="research this", parent_job_id="parent-1", params={},
    )

    insert = next(sql for sql, _ in conn.cur.executed if "INSERT INTO genesis_jobs" in sql)
    params = next(p for sql, p in conn.cur.executed if "INSERT INTO genesis_jobs" in sql)
    assert '"tenantId"' in insert and '"ownerPrincipalId"' in insert
    assert "e4l" in params and "service:cato" in params


def test_f_tenant_01_unowned_child_is_readable_by_the_legacy_gateway(monkeypatch, job_store_mod):
    """The concrete harm the fix prevents, asserted through the real ownership rule: a child
    row with NULL tenant AND NULL owner is owned by the shared-gateway principal, meaning any
    GATEWAY_API_KEY bearer can read the parent's delegated prompt and params."""
    from runtime.request_auth import Principal, legacy_gateway_principal, owns_resource

    orphan = {"id": "child-1", "tenantId": None, "ownerPrincipalId": None}
    cato = Principal(
        principal_id="service:cato", tenant_id="e4l", client_id="cato",
        scopes=frozenset({"job.read"}), auth_method="ap2", expires_at=0,
    )

    assert owns_resource(legacy_gateway_principal(), orphan) is True   # the hole
    assert owns_resource(cato, orphan) is False                        # and its mirror image

    inherited = {"id": "child-1", "tenantId": "e4l", "ownerPrincipalId": "service:cato"}
    assert owns_resource(cato, inherited) is True
    assert owns_resource(legacy_gateway_principal(), inherited) is False


# ---------------------------------------------------------------------------
# F-LIFE-01 — jobs claimed but not yet processed were reaped as stale
# ---------------------------------------------------------------------------

def test_f_life_01_claim_seeds_the_heartbeat(monkeypatch, job_store_mod):
    """run_tick claims WORKER_CONCURRENCY jobs then processes them SERIALLY. Without a seeded
    heartbeat, jobs 2..N sit RUNNING with lastHeartbeatAt NULL and the next tick reaps them."""
    conn = _wire(monkeypatch, job_store_mod, [[{"id": "j1"}], None])

    job_store_mod.claim_queued_jobs(limit=3)

    claim_sql = conn.cur.executed[0][0]
    assert '"lastHeartbeatAt" = NOW()' in claim_sql


def test_f_life_01_single_claim_seeds_the_heartbeat(monkeypatch, job_store_mod):
    conn = _wire(monkeypatch, job_store_mod, [{"id": "j1"}, None])

    job_store_mod.claim_job_by_id("j1")

    assert '"lastHeartbeatAt" = NOW()' in conn.cur.executed[0][0]


def test_f_life_01_reaper_does_not_treat_null_heartbeat_as_instantly_stale(
    monkeypatch, job_store_mod
):
    conn = _wire(monkeypatch, job_store_mod, [[]])

    job_store_mod.expire_stale_running_jobs(stale_minutes=5)

    sql = conn.cur.executed[0][0]
    assert '"lastHeartbeatAt" IS NULL' not in sql, (
        "a NULL heartbeat must fall back to startedAt, not mean 'reap immediately'"
    )
    assert 'COALESCE("lastHeartbeatAt", "startedAt", "createdAt")' in sql


def test_f_life_02_reaper_binds_stale_minutes_instead_of_interpolating(monkeypatch, job_store_mod):
    """stale_minutes was f-string interpolated into the SQL text."""
    conn = _wire(monkeypatch, job_store_mod, [[]])

    job_store_mod.expire_stale_running_jobs(stale_minutes=7)

    sql, params = conn.cur.executed[0]
    assert "make_interval(mins => %s)" in sql
    assert "INTERVAL '7 minutes'" not in sql
    assert 7 in params


# ---------------------------------------------------------------------------
# F-LIFE-03 — terminal jobs could be resurrected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "terminal,attempted",
    [
        ("EXPIRED", "DELIVERED"),
        ("FAILED", "DELIVERED"),
        ("REFUNDED", "SETTLED"),
        ("SETTLED", "REFUNDED"),
        ("DELIVERED", "RUNNING"),
    ],
)
def test_f_life_03_terminal_jobs_cannot_be_resurrected(
    monkeypatch, job_store_mod, terminal, attempted
):
    """A job the reaper already EXPIRED must not become DELIVERED when the worker finishes it
    a moment later — settlement decisions read `status`."""
    conn = _wire(monkeypatch, job_store_mod, [{"status": terminal}])

    assert job_store_mod.update_job_status("j1", attempted) is False
    assert not any("UPDATE genesis_jobs SET status" in sql for sql, _ in conn.cur.executed)


def test_f_life_03_non_terminal_transitions_still_work(monkeypatch, job_store_mod):
    conn = _wire(monkeypatch, job_store_mod, [{"status": "RUNNING"}, None, None])

    assert job_store_mod.update_job_status("j1", "DELIVERED") is True
    assert any("UPDATE genesis_jobs SET status" in sql for sql, _ in conn.cur.executed)


def test_f_life_03_idempotent_terminal_write_is_allowed(monkeypatch, job_store_mod):
    """A retry writing the SAME terminal status must not be reported as a refusal."""
    _wire(monkeypatch, job_store_mod, [{"status": "FAILED"}, None, None])

    assert job_store_mod.update_job_status("j1", "FAILED") is True


def test_f_life_03_human_initiated_dispute_after_settlement_still_works(
    monkeypatch, job_store_mod
):
    """The guard must stop the automatic reaper-vs-worker race WITHOUT breaking the
    deliberate lifecycle: main.py's dispute route lets a buyer dispute a SETTLED job."""
    _wire(monkeypatch, job_store_mod, [{"status": "SETTLED"}, None, None])

    assert job_store_mod.update_job_status(
        "j1", "DISPUTED", allow_terminal_override=True
    ) is True


def test_f_life_03_only_three_admin_routes_may_override_a_terminal_status():
    """The override is a narrow, auditable exception, not a general escape hatch.

    If a new caller starts passing allow_terminal_override, this test fails and forces the
    decision to be made deliberately rather than by copy-paste.
    """
    import re

    main_src = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
    worker_src = (PROJECT_ROOT / "worker.py").read_text(encoding="utf-8")
    call_src = (PROJECT_ROOT / "tools" / "genesis_call_tool.py").read_text(encoding="utf-8")

    overrides = re.findall(r'update_job_status\([^)]*allow_terminal_override=True', main_src)
    assert len(overrides) == 3, f"expected exactly 3 admin overrides in main.py, found {len(overrides)}"
    # The automatic paths must never override.
    assert "allow_terminal_override" not in worker_src
    assert "allow_terminal_override" not in call_src


# ---------------------------------------------------------------------------
# F-BUDGET-01 — sibling children each inherited the FULL delegation budget
# ---------------------------------------------------------------------------

def test_f_budget_01_delegated_spend_reduces_the_next_siblings_ceiling():
    from agent_runtime import AgentRuntime

    rt = AgentRuntime("http://llm.invalid", "k")
    assert rt._delegated_tokens_spent.get("parent-1", 0) == 0

    rt.record_delegated_spend("parent-1", tokens=3000)
    rt.record_delegated_spend("parent-1", tokens=500)

    assert rt._delegated_tokens_spent["parent-1"] == 3500
    # A 4000-token parent ceiling has 500 left for the third sibling, not another 4000.
    assert max(0, 4000 - 0 - rt._delegated_tokens_spent["parent-1"]) == 500


def test_f_budget_01_ctx_budget_actually_subtracts_sibling_spend():
    """The value genesis_call receives, not just the ledger behind it.

    Parent ceiling 4000 tokens / 200c. First child spends 3000. The SECOND child must be
    offered 1000, not another 4000 — otherwise nine siblings inherit nine times the ceiling.
    """
    from agent_runtime import AgentRuntime

    rt = AgentRuntime("http://llm.invalid", "k")
    first = rt.remaining_delegation_budget(
        "parent-1", token_budget=4000, total_tokens=0, cost_budget_cents=200
    )
    assert first == (4000, 200)

    rt.record_delegated_spend("parent-1", tokens=3000, cents=150)
    second = rt.remaining_delegation_budget(
        "parent-1", token_budget=4000, total_tokens=0, cost_budget_cents=200
    )

    assert second == (1000, 50)

    rt.record_delegated_spend("parent-1", tokens=9999, cents=9999)
    assert rt.remaining_delegation_budget(
        "parent-1", token_budget=4000, total_tokens=0, cost_budget_cents=200
    ) == (0, 0), "budget must floor at zero, never go negative"


def test_f_budget_01_spend_is_scoped_per_parent_job():
    from agent_runtime import AgentRuntime

    rt = AgentRuntime("http://llm.invalid", "k")
    rt.record_delegated_spend("parent-1", tokens=3000)

    assert rt._delegated_tokens_spent.get("parent-2", 0) == 0
    rt.record_delegated_spend(None, tokens=999)  # no parent -> nothing charged anywhere
    assert sum(rt._delegated_tokens_spent.values()) == 3000


@pytest.mark.anyio
async def test_f_budget_01_genesis_call_charges_the_child_back_to_the_parent():
    """The ledger is only real if genesis_call actually posts to it."""
    from tools import genesis_call_tool

    charged: list[tuple] = []

    class _Runtime:
        async def execute_agent(self, *a, **k):
            return {"ok": True, "response": "done", "resource_usage": {"total_tokens": 1234}}

        def record_delegated_spend(self, parent_job_id, *, tokens=0, cents=0):
            charged.append((parent_job_id, tokens))

    result = await genesis_call_tool.genesis_call(
        agent="genesis-research", task="t", _runtime=_Runtime(),
        _parent_job_id="parent-1", _parent_agent_slug="genesis-meta",
    )

    assert result["ok"] is True
    assert charged == [("parent-1", 1234)]


# ---------------------------------------------------------------------------
# F-DELEG-01 — delegated children never heartbeat, so the reaper killed them
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_f_deleg_01_child_job_is_heartbeaten_while_it_runs(monkeypatch):
    """A child runs INLINE and is inserted as RUNNING; nothing else beats its id."""
    import asyncio

    from tools import genesis_call_tool

    beats: list[str] = []

    class _JobStore:
        @staticmethod
        def heartbeat(job_id):
            beats.append(job_id)
            return True

    monkeypatch.setattr(genesis_call_tool, "job_store", _JobStore())
    monkeypatch.setattr(genesis_call_tool, "durable_store", None)
    monkeypatch.setattr(genesis_call_tool, "CHILD_HEARTBEAT_INTERVAL_S", 0.01)

    class _Runtime:
        async def execute_agent(self, *a, **k):
            await asyncio.sleep(0.06)  # outlive several heartbeat intervals
            return {"ok": True, "response": "done"}

        def record_delegated_spend(self, *a, **k):
            pass

    result = await genesis_call_tool.genesis_call(
        agent="genesis-research", task="t", _runtime=_Runtime(),
        _parent_job_id="parent-1", _parent_agent_slug="genesis-meta",
    )

    assert result["ok"] is True
    assert beats, "the delegated child was never heartbeaten"
    assert set(beats) == {result["child_job_id"]}


@pytest.mark.anyio
async def test_f_deleg_01_heartbeat_task_is_cancelled_when_the_child_finishes(monkeypatch):
    """An orphaned pump would keep a finished child looking alive to the reaper forever."""
    import asyncio

    from tools import genesis_call_tool

    beats: list[str] = []

    class _JobStore:
        @staticmethod
        def heartbeat(job_id):
            beats.append(job_id)
            return True

    monkeypatch.setattr(genesis_call_tool, "job_store", _JobStore())
    monkeypatch.setattr(genesis_call_tool, "durable_store", None)
    monkeypatch.setattr(genesis_call_tool, "CHILD_HEARTBEAT_INTERVAL_S", 0.01)

    class _Runtime:
        async def execute_agent(self, *a, **k):
            return {"ok": True, "response": "done"}

        def record_delegated_spend(self, *a, **k):
            pass

    await genesis_call_tool.genesis_call(
        agent="genesis-research", task="t", _runtime=_Runtime(),
        _parent_job_id="parent-1", _parent_agent_slug="genesis-meta",
    )
    before = len(beats)
    await asyncio.sleep(0.06)

    assert len(beats) == before, "heartbeat kept firing after the child completed"


# ---------------------------------------------------------------------------
# F-STORE-01 — an unusable AP2 nonce store failed as HTTP 500, and passed the boot guard
# ---------------------------------------------------------------------------

def test_f_store_01_boot_guard_rejects_an_unusable_nonce_store_path(monkeypatch, tmp_path):
    """GENESIS_AUTH_DB_PATH=/var/data/... on a host with no disk mounted there passed the
    length check and then failed on the FIRST AP2 request."""
    import main

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file", encoding="utf-8")
    monkeypatch.setenv("GENESIS_AUTH_DB_PATH", str(blocker / "sub" / "auth.db"))
    monkeypatch.setenv("GENESIS_PRINCIPAL_TOKEN_KEY", "k" * 40)
    monkeypatch.setenv("GENESIS_ACTION_GRANT_KEY", "g" * 40)

    with pytest.raises(RuntimeError, match="nonce store cannot be opened"):
        main.assert_auth_material_configured()


def test_f_store_01_boot_guard_accepts_a_usable_path(monkeypatch, tmp_path):
    import main

    monkeypatch.setenv("GENESIS_AUTH_DB_PATH", str(tmp_path / "data" / "auth.db"))
    monkeypatch.setenv("GENESIS_PRINCIPAL_TOKEN_KEY", "k" * 40)
    monkeypatch.setenv("GENESIS_ACTION_GRANT_KEY", "g" * 40)

    main.assert_auth_material_configured()  # must not raise


def test_f_store_01_unwritable_nonce_store_refuses_instead_of_raising_oserror(
    monkeypatch, tmp_path
):
    """An OSError here escaped verify_agent_principal's `except AuthenticationError` and
    surfaced as a 500 with a stack trace instead of a diagnosable refusal."""
    import sqlite3

    from runtime import request_auth
    from runtime.request_auth import AuthenticationError

    def _boom(*a, **k):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(sqlite3, "connect", _boom)

    with pytest.raises(AuthenticationError, match="ap2_nonce_store_unavailable"):
        request_auth._consume_nonce("cato", "n" * 20, 0, db_path=tmp_path / "auth.db")


# ---------------------------------------------------------------------------
# F-GRANT-01 — single-use action grants: double-spend and cross-binding
# ---------------------------------------------------------------------------

def _grant(tmp_path, **over):
    from runtime.action_grants import issue_action_grant

    kw = dict(
        principal_id="service:cato", tenant_id="e4l", tool="github_tool",
        args={"target": "prod"}, authorization_id="auth-1", key="k" * 40, now=1_800_000_000,
    )
    kw.update(over)
    return issue_action_grant(**kw)


def test_f_grant_01_double_spend_is_refused(tmp_path):
    from runtime.action_grants import GrantError, consume_action_grant

    token = _grant(tmp_path)
    common = dict(
        principal_id="service:cato", tenant_id="e4l", tool="github_tool",
        args={"target": "prod"}, db_path=tmp_path / "auth.db", key="k" * 40, now=1_800_000_000,
    )

    assert consume_action_grant(token, **common) == "auth-1"
    with pytest.raises(GrantError, match="grant_already_consumed"):
        consume_action_grant(token, **common)


@pytest.mark.parametrize(
    "override,error",
    [
        ({"principal_id": "service:other"}, "grant_principal_mismatch"),
        ({"tenant_id": "other"}, "grant_tenant_mismatch"),
        ({"tool": "workspace_shell"}, "grant_tool_mismatch"),
        ({"args": {"target": "staging"}}, "grant_args_mismatch"),
    ],
)
def test_f_grant_01_grant_is_bound_to_principal_tenant_tool_and_args(tmp_path, override, error):
    from runtime.action_grants import GrantError, consume_action_grant

    token = _grant(tmp_path)
    common = dict(
        principal_id="service:cato", tenant_id="e4l", tool="github_tool",
        args={"target": "prod"}, db_path=tmp_path / "auth.db", key="k" * 40, now=1_800_000_000,
    )
    common.update(override)

    with pytest.raises(GrantError, match=error):
        consume_action_grant(token, **common)


def test_f_grant_01_expired_grant_is_refused(tmp_path):
    from runtime.action_grants import GrantError, consume_action_grant

    token = _grant(tmp_path)

    with pytest.raises(GrantError, match="grant_expired"):
        consume_action_grant(
            token, principal_id="service:cato", tenant_id="e4l", tool="github_tool",
            args={"target": "prod"}, db_path=tmp_path / "auth.db", key="k" * 40,
            now=1_800_000_000 + 3600,
        )


@pytest.mark.anyio
async def test_f_budget_01_dispatch_passes_the_reduced_budget_to_genesis_call(
    monkeypatch, tmp_path
):
    """Covers the CALL SITE, not just the arithmetic: drives the real tool-dispatch loop and
    captures what genesis_call is actually handed after a sibling has already spent."""
    import agent_runtime
    from agent_runtime import AgentRuntime

    captured: dict = {}

    async def fake_tool(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "response": "child done"}

    monkeypatch.setattr(agent_runtime, "get_tool", lambda name: fake_tool)
    monkeypatch.setattr(agent_runtime, "tool_schemas_for", lambda allowed: [])
    monkeypatch.setattr(agent_runtime, "append_tool_intent", lambda **k: 1)
    monkeypatch.setattr(agent_runtime, "append_tool_event", lambda **k: 1)

    calls = {"n": 0}

    async def fake_llm(self, model, messages, tools, token_budget):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "choices": [{"message": {"content": None, "tool_calls": [
                    {"id": "1", "type": "function", "function": {
                        "name": "genesis_call", "arguments": '{"agent":"genesis-research","task":"t"}'}}
                ]}}],
                "usage": {"total_tokens": 100},
            }
        return {"choices": [{"message": {"content": "final", "tool_calls": []}}],
                "usage": {"total_tokens": 10}}

    monkeypatch.setattr(AgentRuntime, "_call_llm", fake_llm)

    rt = AgentRuntime("http://llm.invalid", "k")
    rt.record_delegated_spend("job-1", tokens=2500, cents=120)  # an earlier sibling

    await rt._run_loop(
        {"slug": "genesis-meta", "tools_advertised": ["genesis_call"],
         "token_budget": 4000, "conduit_budget_cents": 200, "system_prompt": "s"},
        "task", {}, "job-1", tmp_path, None, session_id="s-1",
    )

    assert captured, "genesis_call was never dispatched"
    # 4000 ceiling - 100 own tokens - 2500 already delegated = 1400 (NOT 3900, NOT 4000)
    assert captured["_remaining_token_budget"] == 1400
    assert captured["_remaining_cost_budget_cents"] == 80
