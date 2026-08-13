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

## Durable tables (Genesis-owned Postgres)

Genesis owns all six `genesis_*` runtime tables through
`migrations/001_genesis_runtime.sql`. Apply that file transactionally with
`psql -v ON_ERROR_STOP=1 -f migrations/001_genesis_runtime.sql` to a reviewed
Genesis database URL. Do not use the Cato database or an operator's local DB.

| Table | Purpose |
|-------|---------|
| `genesis_jobs` | Durable queue, lifecycle status, idempotency, and result metadata |
| `genesis_job_events` | Append-only job status transitions |
| `genesis_agent_sessions` | Restart-durable session record per invocation (status, workspace, trace, parent linkage) |
| `genesis_agent_events` | Durable lifecycle events (mirrors `/tmp` JSONL) |
| `genesis_job_relationships` | Parent→child delegation edges from `genesis_call` |
| `genesis_artifacts` | Per-file artifact metadata (sha256/size/mime/uri/signed_url) |

Genesis reads/writes via psycopg (`job_store.py` and `durable_store.py`). The
migration must pass a fresh-database CRUD proof before deployment.

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
