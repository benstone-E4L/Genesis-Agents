# Genesis Agents — Deployment Snapshot (Phase 0 Ground Truth)

Generated: 2026-08-15. All three health GETs were unauthenticated GET-only calls against the
live Render deployment; no POST/PUT/DELETE/PATCH was issued.

## Live health checks

### `GET https://swarmsync-agents.onrender.com/health/worker`
- **HTTP 200**
- Body:
```json
{"enabled":true,"last_tick_at":"2026-08-15T14:04:34.949526+00:00","last_claimed_job_id":null,"currently_running_job_id":null,"processed_count":0,"failed_count":0,"queue_depth":0,"stale_job_count":0,"worker_mode":"trigger_dev","last_error":null,"commit":"6712dcf361c9fb3e8d7edadc4c6d77ffc251b80d"}
```
- **Deployed commit matches local HEAD exactly** (`6712dcf361c9fb3e8d7edadc4c6d77ffc251b80d`) — Render is running the code currently checked out locally; no deploy drift.
- `worker_mode: "trigger_dev"` is the default value of an undocumented env var (`os.getenv("WORKER_MODE", "trigger_dev")` in `main.py`) — not mentioned in RUNTIME_RUNBOOK.md and not set in `.env`. Worth a name/label review since "trigger_dev" reads like a dev-mode label on a production health endpoint, even though the worker is otherwise reporting healthy (`enabled: true`, `queue_depth: 0`, `last_error: null`).

### `GET https://swarmsync-agents.onrender.com/health/sandbox`
- **HTTP 200**
- Body:
```json
{"ok":true,"isolation":"unavailable","bwrap_available":false,"network_allowed":false,"note":"bwrap = real kernel isolation; process = hardened process sandbox + static guards (install bubblewrap / Docker deploy for full filesystem namespace isolation)."}
```
- **`isolation: "unavailable"` on the live deployment** — per RUNTIME_RUNBOOK.md's own description of the `unavailable` tier: "if bubblewrap cannot create the kernel boundary, `workspace_shell` returns `secure_sandbox_unavailable` without spawning the requested process." This confirms **`workspace_shell` is currently fail-closed/disabled on the production Render instance** (not just a documentation caveat — this is the live, current state). Any agent capability that depends on sandboxed shell execution is not functional in production right now.

### `GET https://swarmsync-agents.onrender.com/health/browser`
- **HTTP 200**
- Body:
```json
{"chromium_installed":true,"executable_path":"/opt/render/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome","chromium_dirs":["chromium-1228","chromium_headless_shell-1228"],"conduit_package_importable":true,"conduit_bridge_startable":true,"startup_error":null,"memory_warning":null,"render_instance_type":"web","smoke_test_url":"/health/conduit/smoke"}
```
- Chromium is installed, Conduit is importable and startable, no startup error. Browser-backed agents (conduit tool) have their prerequisite runtime available.

## Agent-count reproduction (fresh, against current code)

Command from CLAUDE.md/AGENTS.md, run locally against HEAD `6712dcf3`:

```bash
python -c "import main, bundle_loader; bf=sorted(p.stem for p in bundle_loader.BUNDLES_DIR.glob('*.json')); ap=list(main.AGENT_PERSONAS.keys()); g=[s for s in ap if bundle_loader.resolve_bundle_slug(s) in bf]; print(f'bundles={len(bf)} personas={len(ap)} guarded={len(g)} unguarded={len(ap)-len(g)}')"
```

**Output:** `bundles=24 personas=60 guarded=24 unguarded=36`

This **exactly matches** the table in CLAUDE.md/AGENTS.md (24 bundle-backed / 60 catalogued / 24 guarded / 36 unguarded). No drift since the 2026-08-08 reconciliation date. The reproduction command itself also emitted these boot-time warnings (informational, from the local machine's environment, not from Render):
```
conduit/tools/rubric.py not found — Track 2 rubric verification disabled
WARNING:artifact_store:boto3 not installed; artifact_store will use local disk only
WARNING:main:escrow containment active: escrow_client is NOT bound in this build. Automation may prepare; a human pays; automation records.
WARNING:main:AGENT_GATEWAY_SECRET is not set — negotiate callbacks will be sent without authentication
WARNING:main:GATEWAY_API_KEY is not set — protected routes fail closed and lifespan startup will refuse service
```
These reflect the **bare `python -c` invocation not loading `.env`** (no dotenv load in that one-liner), not the actual deployed Render environment (which the health GETs above confirm is up and serving with the matching commit). The escrow-containment-active warning is the expected fail-closed default described in `escrow_guard.py` and is not itself evidence of a problem.

## Reconciling stale prior certification reports against current code

### `testing/GENESIS_AGENTS_LIVE_TEST_REPORT.md` (2026-05-23, 14/20 live-functional)
Re-checked against current `main.py` (not re-run live — POST calls were out of scope for this task):

- **Finding: "4 agents (Builder, Research, Deploy, QA) time out due to a one-line hardcoded bypass restricted to `genesis-meta` only."** — **Appears fixed in code.** `main.py` lines ~2032-2047 show the `live_test`/`testContext` bypass (`_prefer_sync_bundle_run(body)`) is applied generically to *any* bundle-backed slug before the AgentRuntime/ConduitBridge path, not gated to a specific agent name. The async job-mode path (`job_store.create_job`) for Builder/Research/Deploy/QA/Meta, described in RUNTIME_RUNBOOK.md, is also present in code. **Not independently re-verified live** (no POST issued in this task) — flagged as code-confirmed only, needs a live run to fully close out.
- **Finding: Finance Agent (Agent 10) persona-scope bug preventing task execution.** — `skill_bundles/genesis-finance.json` exists, meaning `genesis-finance` is now one of the 24 bundle-backed (guarded) slugs rather than a persona-only path — a structurally different code path than what the May report tested. Whether the specific scope bug is resolved was **not verifiable without a live POST**, which was out of scope for this task. `IMPLEMENTATION_PLAN.md` explicitly excludes `genesis_finance_x402` from full/real-mode invocation in its own test plan ("Guarded finance/deploy agents ... are never invoked in real/full mode by any task below — mocked/guard-proof only"), so this may remain an intentionally-untested-live path going forward, not merely an oversight.
- **Finding: Smart tier routing disabled (`GENESIS_LLM_MODEL` not set to `auto`).** — `.env` does have `GENESIS_LLM_MODEL` populated (value not read into this artifact); whether it is literally `auto` was not confirmed (reading the value would violate this task's "never print `.env` values" constraint). Flagged as unconfirmed, re-check via a live routing-metadata response if this matters for certification.

### `testing/HKO_CERTIFICATE.md` (2026-06-13, CONDITIONAL, 2 deferred partial findings)
- **Condition 1: "Add one integration test that hits the real genesis_jobs Postgres table (not mocked)."** — `tests/test_postgres_stores.py` and `tests/test_migration_runner.py` now exist (found via grep for `GENESIS_JOB_DATABASE_URL`/`DATABASE_URL` references) and migrations 001-005 are present with a migration ledger table (`genesis_schema_migrations`) per RUNTIME_RUNBOOK.md. Suggests this condition has since been addressed, but **not independently executed/verified in this pass** (no test run requested — REQUIRED TESTS: NONE for this task).
- **Condition 2: "Add a Meta delegation eval that verifies `trace.tool_calls` contains a real `genesis_call` dispatch."** — `test_meta_orchestration_trace.py` and `test_subagent_trace_contract.py` exist in the repo root, consistent with this condition having been addressed. Not executed in this pass.
- **Residual risk #3 in that certificate** ("`/health/browser` and `/health/worker` are read-only observers — a worker that crashes without updating `_worker_state` would show stale healthy data") remains architecturally true today: the live `/health/worker` GET above returns `last_tick_at` from ~this session, which is a live heartbeat, not proof against the stale-data failure mode described. Still an open, undated residual risk.

## Not verified in this pass (explicitly out of scope)

- No live agent invocation (`POST /agents/{slug}/run`) was made — the task packet authorized exactly 3 GETs, no more.
- Whether Render's actual deployed environment variables match local `.env` was not checked (would require Render dashboard/API access beyond the 3 allowed GETs).
- `GENESIS_LLM_MODEL`'s actual value (`auto` vs. a specific model string) was not read, per the no-secrets-printed constraint applied broadly to `.env` values.
