# Genesis Agents — Environment Snapshot (Phase 0 Ground Truth)

Generated: 2026-08-15, by direct repo inspection (not from a handoff doc).

## Git state

- Branch: `main`, up to date with `origin/main`
- HEAD commit: `6712dcf361c9fb3e8d7edadc4c6d77ffc251b80d`
- Last commit: "Rotate AP2 trust to Cato's recovered vault key after root-cause fix" (2026-08-13 11:39:30 -0700)
- Working tree: clean (`git status` — nothing to commit)
- Remotes:
  - `origin` → `https://github.com/benstone-E4L/Genesis-Agents.git`
  - `azure-devops` → Azure DevOps repo `neshealth/E4L Agents/Genesis-Agents`, **PAT-authenticated (URL contains an embedded personal access token — not reproduced here, treat as credential material)**

## `.env` population (names only — no values read into any output)

`.env` exists at repo root (3609 bytes) alongside `.env.example` (14946 bytes). Names present in `.env` (34 unique keys; `DATABASE_URL` and `PHOENIX_API_KEY`/`PHOENIX_COLLECTOR_ENDPOINT` each appear twice — second occurrence wins, worth cleaning up but not a security issue):

```
GENESIS_GATEWAY_URL, GENESIS_GATEWAY_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY,
RENDER_API_KEY, RENDER_SERVICE_ID, DATABASE_URL (x2), SERPER_API_KEY, GITHUB_API_KEY,
PHOENIX_API_KEY (x2), PHOENIX_COLLECTOR_ENDPOINT (x2), AZURE_DEVOPS_PAT,
AGENT_GATEWAY_SECRET, AWS_ACCESS_KEY_ID, AWS_REGION, AWS_SECRET_ACCESS_KEY,
GATEWAY_API_KEY, GENESIS_ACTION_GRANT_KEY, GENESIS_ALLOW_OPENROUTER_FALLBACK,
GENESIS_AUDIT_DB_PATH, GENESIS_AUTH_DB_PATH, GENESIS_GATEWAY_PRIVKEY_B64,
GENESIS_GATEWAY_PUBKEY_B64, GENESIS_LLM_MODEL, GENESIS_LLM_PROVIDER,
GENESIS_PRINCIPAL_TOKEN_KEY, GENESIS_S3_BUCKET, GENESIS_S3_ENDPOINT,
GENESIS_SESSION_VAULT_KEY, GENESIS_WORKER_ENABLED, Persistent_disk, R2_API_TOKEN,
SWARMSYNC_ADMIN_EMAILS, SWARMSYNC_PLATFORM_FEE_PCT, WORKER_CONCURRENCY,
WORKER_HEARTBEAT_INTERVAL_S, WORKER_POLL_INTERVAL_S, WORKER_STALE_CHECK_INTERVAL_S
```

**Critical vars per CLAUDE.md/AGENTS.md** (`LLM_API_KEY`, `LLM_API_URL`, `GENESIS_LLM_MODEL`, `AGENT_GATEWAY_SECRET`):
- `GENESIS_LLM_MODEL` — populated
- `AGENT_GATEWAY_SECRET` — populated
- `LLM_API_KEY` — **NOT present as a distinct key in `.env`**. `.env` has `GENESIS_GATEWAY_API_KEY`, `GATEWAY_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` instead. Code path resolution not fully traced in this pass; flagged below under Contradictions.
- `LLM_API_URL` — **NOT present in `.env`** (main.py defaults it to `https://api.swarmsync.ai/v1/chat/completions` via `os.getenv` fallback, so absence is non-fatal but means the default route is in effect, not an explicitly configured one).

**Gate for the 3 required live health GETs** (`AGENT_GATEWAY_SECRET` / `GATEWAY_API_KEY` per the ABORT-IF clause): both present in local `.env` → not blocked. (Note: local `.env` values are not proof of what Render's actual deployed environment has configured — see `deployment-snapshot.md`.)

**Present in `.env.example` but absent from `.env`** (gaps worth surfacing, not necessarily bugs — Render's dashboard env is the real source of truth for the deployed service and was not inspected in this pass, out of scope beyond the 3 GETs):
`GENESIS_DEPLOYMENT_APPROVAL_TOKEN`, `GENESIS_ANCHOR_STORE_PATH`, `GENESIS_JOB_DATABASE_URL` (canonical Postgres var per `runtime/pg_store.py` — `.env` only has generic `DATABASE_URL`, which the resolver falls back to; not broken, just not using the "canonical" name), `GENESIS_STORE_BACKEND`, `GENESIS_DB_CONNECT_TIMEOUT_S`, `ASSISTANT_PG_DATABASE_URL`, `RETRIEVAL_MIN_SCORE`, `RETRIEVAL_STALENESS_THRESHOLD_HOURS`, `KNOWLEDGE_BACKBONE_MCP_ENDPOINT`, `KNOWLEDGE_BACKBONE_DATABASE_URL`, `PHOENIX_PROJECT_NAME`, `PHOENIX_TRACING`, `PHOENIX_TRACE_CONTENT`, `PHOENIX_ALLOW_CONTENT_OFFBOX`. Also **`GENESIS_DEPLOYMENT_PROFILE`** (the finance-boundary switch in `escrow_guard.py`) is absent from both `.env` and `.env.example` — its absence means escrow containment defaults to ACTIVE/BLOCKED by design (fail-closed), which is the safe state, but it cannot be confirmed from this repo's local `.env` whether Render has explicitly set it to something else.

## Active external integrations referenced in code

- **SwarmSync router** — `main.py`: all agent LLM calls route through `$LLM_API_URL` (default `https://api.swarmsync.ai/v1/chat/completions`); `SWARMSYNC_API_URL` (default `https://api.swarmsync.ai`) used for conduit-verification and arbitrage callback URLs.
- **Cato** — `main.py` comments/auth logic (`X-Agent-Api-Key`, `X-Agent-Gateway-Secret`) explicitly reference Cato as a caller class; `escrow_guard.py` has a first-class `PROFILE_CATO` deployment profile that, if set, asserts `escrow_client.py` must not even be importable in the deployed artifact (hard boot failure if it is).
- **`escrow_guard.py` / AP2 finance boundary** — fail-closed by default: `escrow_permitted()` is only `True` when `GENESIS_DEPLOYMENT_PROFILE=swarmsync-marketplace` is explicitly set; unset (the observed local state) blocks all 4 escrow functions (`initiate_escrow`, `complete_escrow`, `release_escrow`, `calculate_split`) with a logged warning at boot and a `escrow_blocked()` refusal envelope at call time. `escrow_client.py` itself has 11 live call sites in `main.py`/`worker.py` per the module's own docstring — it exists and is wired, but is gated by this profile check, not absent.
- **AP2 tables** — `genesis_ap2_nonces` and `genesis_action_grants` confirmed present in `migrations/003_genesis_auth_state.sql` (8 references to the two table names combined). Also `genesis_audit_log`/`genesis_audit_anchors` (migration 004) for the tamper-evident hash chain.
- **Artifact storage** — `artifact_store.py`; local run logged `WARNING:artifact_store:boto3 not installed; artifact_store will use local disk only` in this dev environment. `.env` has `GENESIS_S3_BUCKET`/`GENESIS_S3_ENDPOINT`/`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_REGION` populated, and `R2_API_TOKEN` — so the deployed Render service likely has `boto3` installed (it's in `requirements.txt`) and S3/R2-backed storage; this local check only reflects the machine used for the reproduction command, not Render.
- **GitHub** — `GITHUB_API_KEY` populated in `.env`; `github_tool` referenced in `IMPLEMENTATION_PLAN.md` as the sole/canonical registration (Vercel/Netlify deploy tools were deleted per that plan's Phase A).
- **Render** — `RENDER_API_KEY`/`RENDER_SERVICE_ID` populated; deployment target confirmed live at `https://swarmsync-agents.onrender.com` (see deployment-snapshot.md).
- **Phoenix (observability/tracing)** — `PHOENIX_API_KEY`/`PHOENIX_COLLECTOR_ENDPOINT` populated in `.env`.
- **Search** — `SERPER_API_KEY` populated (web_search tool backing).

## Contradictions Found

1. **`LLM_API_KEY` — the doc-named "critical" env var is not present by that exact name in `.env`.** CLAUDE.md/AGENTS.md both list `LLM_API_KEY` as critical/required. `.env` instead has `GATEWAY_API_KEY`, `GENESIS_GATEWAY_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`. Whether `main.py`'s LLM-key resolution (`_llm_api_key()` style lookup, seen partially at main.py:1038 checking `LLM_API_KEY`, `SWARMSYNC_ROUTING_API_KEY`, `ROUTING_API_KEY`) has a fallback that also checks one of the present keys was not fully traced in this pass — flagged as a gap to re-verify, not confirmed broken (a fresh local repro run did not raise an `LLM_API_KEY`-missing warning, only `AGENT_GATEWAY_SECRET`/`GATEWAY_API_KEY` warnings, suggesting some resolution path is satisfied).
2. **`IMPLEMENTATION_PLAN.md`'s slug count is now stale relative to CLAUDE.md/AGENTS.md.** The plan's "Ground truth ... (2026-08-04)" section says a live `GET /agents` call "returned all 57 live slugs." CLAUDE.md/AGENTS.md (dated 2026-08-08, "post CHUNK_2_REGISTRY reconciliation") say 60 catalogued slugs, and today's fresh reproduction (see deployment-snapshot.md) confirms 60. This is an internal doc-vs-doc staleness, not a code defect — the plan document itself predates the reconciliation it's tracking.
3. **No contradiction found between RUNTIME_RUNBOOK.md's async job-mode description and the code** — `main.py` (lines ~2032-2060) confirms Builder/Research/Deploy/QA/Meta route through `job_store.create_job` when `job_mode == "async"` and the live-test/testContext bypass applies uniformly (not agent-specific), which also resolves a defect noted in the stale 2026-05-23 live test report (see deployment-snapshot.md for detail).
4. **`GENESIS_DEPLOYMENT_PROFILE` (the escrow fail-closed switch) is absent from `.env.example` entirely** — RUNTIME_RUNBOOK.md and CLAUDE.md/AGENTS.md never mention it, even though `escrow_guard.py`'s own docstring calls it the deployment-profile control for the whole finance boundary. This is a documentation gap (the profile switch is real and load-bearing in code but undocumented in the three read-first docs), not a functional contradiction.
