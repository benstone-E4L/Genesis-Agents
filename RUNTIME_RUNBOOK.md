# Genesis Real-Agent Runtime — Runbook

This documents the hardened autonomous-agent runtime: durable persistence,
sandbox isolation, the worker, and the live proof procedure.

## Architecture (request → proof trail)

```
POST /agents/{slug}/run  (async agents return job_id + poll_url)
  → genesis_jobs row (QUEUED)                 [Genesis-owned Postgres]
  → in-process auto-worker claims it (RUNNING)  [GENESIS_WORKER_ENABLED=true]
  → AgentRuntime.execute_agent(job_id, session_id)
       • genesis_agent_sessions row (ACTIVE → COMPLETED/FAILED)   [durable]
       • per-job workspace /tmp/jobs/{job_id} (+ sandbox)
       • tools: file_write, workspace_shell (sandboxed), conduit (browser),
         genesis_call (delegation), web_*, ...
       • genesis_agent_events rows for every lifecycle event       [durable]
       • genesis_call → child genesis_jobs + genesis_agent_sessions +
         genesis_job_relationships rows (first-class child jobs)    [durable]
  → artifacts uploaded (S3 or local disk) + genesis_artifacts rows [durable, sha256]
  → job DELIVERED / DELIVERED_WITH_ARTIFACT_WARNING / FAILED / EXPIRED
```

## Schema migrations — `python scripts/migrate.py`

**Never apply migrations by hand.** `psql -f` leaves no record that a migration
ran, which is how production schema drift shipped: every async AP2 dispatch
returned HTTP 500 while the test suite stayed green against an in-memory mock.

```bash
python scripts/migrate.py            # apply pending migrations (Render pre-deploy)
python scripts/migrate.py --check    # report pending; exit 2 if the DB is behind
python scripts/migrate.py --status   # print the applied-migration ledger
```

Exit codes: `0` current/applied, `1` refused or failed, `2` pending (`--check`).

| Guarantee | How |
|-----------|-----|
| Applied set is recorded | `genesis_schema_migrations` (filename, sha256, applied_at, duration_ms) |
| An edited applied migration is refused | sha256 of the file bytes is compared before anything runs; a mismatch aborts the **whole** run and is never auto-repaired |
| No partial records | the DDL and its ledger row commit in one transaction; a failure rolls both back |
| Order is enforced | lexical; a pending file that sorts before an applied one is refused |
| Idempotent | re-running when current is a no-op that exits 0 |
| Safe with two instances | a session advisory lock is held for the run, so concurrent pre-deploys apply each migration once |

The `.sql` files wrap themselves in `BEGIN;`/`COMMIT;`. The runner strips that
outer pair so it owns the transaction — otherwise the file's own `COMMIT` would
end the transaction early and the ledger row would land outside it. Any other
transaction control inside a migration is a hard error.

If an applied migration was edited, **add a new migration**. Do not edit it back
and do not rewrite the ledger; the runner refuses either way, deliberately.

## Durable tables (Genesis-owned Postgres)

Genesis owns every `genesis_*` table. One database, addressed by
`GENESIS_JOB_DATABASE_URL`, backs all of it. Do not use the Cato database or an
operator's local DB.

| Table | Migration | Purpose |
|-------|-----------|---------|
| `genesis_jobs` | 001 | Durable queue, lifecycle status, idempotency, and result metadata |
| `genesis_job_events` | 001 | Append-only job status transitions |
| `genesis_agent_sessions` | 001 | Restart-durable session record per invocation (status, workspace, trace, parent linkage) |
| `genesis_agent_events` | 001 | Durable lifecycle events (mirrors `/tmp` JSONL) |
| `genesis_job_relationships` | 001 | Parent→child delegation edges from `genesis_call` |
| `genesis_artifacts` | 001 | Per-file artifact metadata (sha256/size/mime/uri/signed_url) |
| `genesis_jobs.tenantId` / `.ownerPrincipalId` | 002 | AP2 tenant scoping; `owns_resource` refuses cross-tenant reads |
| `genesis_ap2_nonces` | 003 | AP2 replay protection — the unique violation IS the refusal |
| `genesis_action_grants` | 003 | Single-use action grants; the only thing making a signed grant single-use |
| `genesis_audit_log` | 004 | Tamper-evident hash chain |
| `genesis_audit_anchors` | 004 | Append-only daily Merkle anchors |
| `genesis_schema_migrations` | runner | The applied-migration ledger |

Genesis reads/writes via psycopg (`job_store.py`, `durable_store.py`,
`runtime/pg_store.py`).

## No persistent disk is required

`GENESIS_AUTH_DB_PATH`, `GENESIS_AUDIT_DB_PATH` and `GENESIS_ANCHOR_STORE_PATH`
are **local-development only**. With `GENESIS_JOB_DATABASE_URL` set, AP2 nonces,
action grants, the audit chain and the Merkle anchors all live in Postgres, so
Render needs no persistent disk for correctness and more than one instance can
be run safely.

The audit chain appends under a `pg_advisory_xact_lock` keyed on the session id.
That lock is not optional: without it two concurrent writers read the same
`prev_hash` and both link to it, forking the chain into two branches with no way
to prove which is real. `verify_chain()` checks both row hashes **and**
`prev_hash` linkage, because a fork leaves every individual row hashing
correctly. A forked chain is a P0.

Boot fails closed: `assert_auth_material_configured()` refuses to start against
a reachable-but-unmigrated database rather than 500ing on the first AP2 request.

## New endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/agents/sessions/{session_id}` | Durable session + child delegations |
| GET | `/agents/jobs/{job_id}/trace` | Full parent→child trace tree (job, sessions, events, children) |
| GET | `/agents/jobs/{job_id}/artifacts` | Artifact metadata (sha256/size) + fresh signed URLs |
| GET | `/agents/jobs/{job_id}/events` | Durable lifecycle events (Postgres-preferred) |
| GET/POST | `/agents/jobs/{job_id}/sandbox` | Sandbox status (isolation tier) / create |
| POST | `/agents/jobs/{job_id}/sandbox/destroy` | Tear down (`retain_debug`/`purge`) |
| GET | `/health/sandbox` | Active shell isolation tier |
| GET | `/health/worker` | Worker enabled/last_tick/queue_depth/stale/processed/commit |
| GET | `/health/browser` | Chromium/Conduit readiness |

## Sandbox isolation (`runtime/sandbox_manager.py`)

`workspace_shell` runs every command through `run_in_sandbox()`:

- **`bwrap` tier (real kernel isolation):** if bubblewrap is installed, commands
  run in a fresh mount + network namespace — only the job workspace is mounted;
  `/etc`, `.env`, the repo, `/var/data`, and other jobs' files do not exist;
  `--unshare-net` removes networking. Reads of those paths fail with ENOENT.
- **`unavailable` tier (fail closed):** if bubblewrap cannot create the kernel
  boundary, `workspace_shell` returns `secure_sandbox_unavailable` without
  spawning the requested process.

`GET /health/sandbox` reports the active state. Production shell tools require
a host/container with bubblewrap and unprivileged user namespaces; without
them, shell execution remains disabled.

## Worker (production)

In-process auto-worker, started on FastAPI boot when `GENESIS_WORKER_ENABLED=true`:
loop calls `worker.run_tick()` directly (no HTTP tick needed), expires stale
RUNNING jobs (>5 min no heartbeat), heartbeats every 20s, bounded concurrency
(`WORKER_CONCURRENCY`, default 3, atomic `FOR UPDATE SKIP LOCKED` claiming),
graceful shutdown. Env: `GENESIS_WORKER_ENABLED`, `GENESIS_WORKER_INTERVAL_SECONDS`,
`GENESIS_WORKER_TICK_LIMIT`, `WORKER_CONCURRENCY`.

## Deploy + migrate procedure

1. **Back up the target Genesis database** and retain the restore identifier.
2. Apply `migrations/001_genesis_runtime.sql` with `ON_ERROR_STOP=1`.
3. Verify `\dt genesis_*` lists all six owned tables and run the committed
   fresh-Postgres CRUD integration test.
4. Deploy Genesis only after those checks pass.
5. Verify `GET /health/worker`, `GET /health/sandbox`, and `GET /health/browser`.

Rollback is restore-first: stop Genesis workers, restore the pre-migration
Genesis database backup, and redeploy the prior application revision. The
migration deliberately has no automated `DROP TABLE` rollback because that
would destroy runtime jobs, sessions, events, relationships, and artifacts.

## Live proof

Env: `GATEWAY_API_KEY` (or `AGENT_GATEWAY_SECRET`), `RENDER_SERVICE_URL`.
```
python testing/live_integration_tests.py real_agent_e2e
python testing/live_integration_tests.py meta_research_builder_qa_artifact
python testing/live_integration_tests.py automatic_worker_execution
python testing/live_integration_tests.py automatic_worker_after_restart   # restart Render first
python testing/live_integration_tests.py tool_policy_denial_live
python testing/live_integration_tests.py workspace_escape_live
python testing/live_integration_tests.py artifact_retrieval_live
python testing/live_integration_tests.py session_persistence_live
python testing/live_integration_tests.py multi_job_stress_live
python testing/live_integration_tests.py child_agent_failure_recovery
```
