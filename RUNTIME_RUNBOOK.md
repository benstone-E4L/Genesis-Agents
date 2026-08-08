# Genesis Real-Agent Runtime — Runbook

This documents the hardened autonomous-agent runtime: durable persistence,
sandbox isolation, the worker, and the live proof procedure.

## Architecture (request → proof trail)

```
POST /agents/{slug}/run  (async agents return job_id + poll_url)
  → genesis_jobs row (QUEUED)                 [Supabase Postgres, Prisma-owned]
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

## Durable tables (Supabase Postgres, owned by SwarmSync.AI Prisma)

Migration: `SwarmSync.AI/apps/api/prisma/migrations/20260626000000_genesis_real_agent_runtime/`.
Applied by the SwarmSync **API** Render deploy pre-step (`prisma migrate deploy`).
**Never** create these with raw SQL / `db push` — that causes drift the CI
(`prisma-drift-check.yml`, `db-sync-check.yml`) rejects.

| Table | Purpose |
|-------|---------|
| `genesis_agent_sessions` | Restart-durable session record per invocation (status, workspace, trace, parent linkage) |
| `genesis_agent_events` | Durable lifecycle events (mirrors `/tmp` JSONL) |
| `genesis_job_relationships` | Parent→child delegation edges from `genesis_call` |
| `genesis_artifacts` | Per-file artifact metadata (sha256/size/mime/uri/signed_url) |

Genesis reads/writes via psycopg (`durable_store.py`). All writes are
**best-effort**: if the migration isn't applied yet, Genesis logs once and falls
back to file/in-memory so nothing breaks during rollout.

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
- **`process` tier (fallback):** new session/process-group (kill whole tree on
  timeout), RLIMIT_CPU/AS/FSIZE/NPROC caps, minimal allow-listed env (no
  secrets), cwd confined to workspace, and a static guard that blocks dangerous
  commands, pipe-to-shell, and sensitive-path/traversal reads.

`GET /health/sandbox` reports which tier is active. **Render's native Python
runtime has no `bwrap`**, so it runs the `process` tier. To get full kernel
isolation, deploy Genesis via a Docker image with `apt-get install -y bubblewrap`
(or run on a host where bubblewrap + unprivileged userns are available). Either
tier blocks the escape attempts in `test_sandbox_manager.py` and
`workspace_escape_live`.

## Worker (production)

In-process auto-worker, started on FastAPI boot when `GENESIS_WORKER_ENABLED=true`:
loop calls `worker.run_tick()` directly (no HTTP tick needed), expires stale
RUNNING jobs (>5 min no heartbeat), heartbeats every 20s, bounded concurrency
(`WORKER_CONCURRENCY`, default 3, atomic `FOR UPDATE SKIP LOCKED` claiming),
graceful shutdown. Env: `GENESIS_WORKER_ENABLED`, `GENESIS_WORKER_INTERVAL_SECONDS`,
`GENESIS_WORKER_TICK_LIMIT`, `WORKER_CONCURRENCY`.

## Deploy + migrate procedure

1. Merge the SwarmSync.AI branch (schema + migration).
2. **Back up the DB** — run the `database-logical-backup` GitHub Actions workflow; confirm success.
3. Deploy the SwarmSync **API** on Render (pre-deploy runs `prisma migrate deploy` → creates the 4 tables). Verify with `list_migrations` / `\dt genesis_*`.
4. Deploy Genesis (push to its `main`; auto-deploys). Genesis starts fresh and picks up the tables.
5. Verify: `GET /health/worker` (enabled, commit), `GET /health/sandbox` (isolation), `GET /health/browser`.

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
