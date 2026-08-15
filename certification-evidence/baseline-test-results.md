# Genesis Agents — Phase 2 Baseline Test Results & Static Risk Scan

**Date:** 2026-08-15
**Repo:** `C:\Users\Work\Desktop\vault\projects\My Github\Genesis Agents`
**Commit tested:** `6712dcf361c9fb3e8d7edadc4c6d77ffc251b80d` (branch `main`, dated 2026-08-13)
**Working tree:** clean except this untracked `certification-evidence/` directory
**Environment:** local Windows dev machine, Python 3.13.2, pytest 9.0.2. No live deployment was touched. `testing/live_integration_tests.py` and `testing/live_real_agent_tests.py` were NOT run (explicitly out of scope for this task — they hit `https://swarmsync-agents.onrender.com`).

This is Phase 2 of the E2E certification doctrine: **passing this suite is the floor, not certification.**

---

## 1. Test run

### Setup

```
python -m pip install -r requirements.txt pytest pytest-asyncio --user
```

Installed cleanly. Pip reported pre-existing version conflicts against unrelated **global** packages on this machine (not part of this repo's dependency graph): `cato-daemon` wants `uvicorn>=0.38.0`, `theharvester` wants pinned versions of `beautifulsoup4`/`fastapi`/`lxml`/`playwright`/`PyYAML`/`uvicorn`, `playwright 1.49.1` wants `pyee==12.0.0` (we now have `13.0.1`). None of these are Genesis Agents' own declared dependencies (`requirements.txt` has no `theharvester`/`cato-daemon`); flagging only because a shared Python environment means a future `pip install` for a different project on this same box could silently downgrade something Genesis needs. Recommend a dedicated venv for this repo before relying on `pip install -r requirements.txt` again.

### Lint / typecheck

**Not run.** No `pyproject.toml`, `pytest.ini`, `setup.cfg`, `ruff.toml`, `.flake8`, or `mypy.ini` exists anywhere in the repo root. Per task scope, no new tooling/config was introduced. The only checked-in quality gate is `.github/workflows/money-path-guards.yml`, which is a pytest-based CI job (see §3), not a linter.

### Full local pytest suite

Command:

```
python -m pytest -q --tb=short
```

Result:

```
888 passed, 63 skipped in 86.18s (0:01:26)
Exit code: 0
```

Collection: **951 tests collected**, 0 collection errors, across:

| Location | Test count |
|---|---|
| `tests/` (auth/state/security-hardening suite) | 335 |
| root-level `test_*.py` (30 files: `test_admin_auth.py`, `test_admin_ui.py`, `test_agent_runtime.py`, `test_artifact_store.py`, `test_bundle_tool_registry.py`, `test_capability_cards.py`, `test_conduit_sessions.py`, `test_conduit_verifier.py`, `test_data_pipeline_tool.py`, `test_gateway_error_mapping.py`, `test_job_store.py`, `test_knowledge_backbone.py`, `test_llm_transport_resolver.py`, `test_meta_orchestration_trace.py`, `test_observability_events.py`, `test_phases_4_6_11.py`, `test_prompt_builder.py`, `test_proof_bridge.py`, `test_retrieval_route.py`, `test_retrieval_route_ap2.py`, `test_retrieval_route_knowledge_backbone.py`, `test_runtime_hardening.py`, `test_sandbox_manager.py`, `test_session_durability.py`, `test_subagent_trace_contract.py`, `test_tool_policy.py`, `test_tool_policy_matrix.py`, `test_tool_risk_coverage.py`, `test_worker_autostart.py`, `test_workspace_isolation.py`) | 313 |
| `eval/tests/` | 268 |
| `testing/test_job_lifecycle.py` | 30 |
| `testing/` other files | not collected — `live_integration_tests.py` and `live_real_agent_tests.py` are not named `test_*.py`, so pytest's default discovery does not pick them up. They were also not run explicitly, per task scope. |

**No failures.** The 63 skips are all environment-gated, not silently-disabled tests (see §2.9 for the full breakdown and risk read).

### CI-declared money-path guard job (ran locally for cross-check)

`.github/workflows/money-path-guards.yml` runs a narrower, stdlib+pytest-only subset that is the repo's actual required-to-pass gate:

```
python -m pytest tests/test_prohibited_tools.py -v
python -m pytest tests/test_escrow_containment.py -v
```

Both pass locally as part of the full run above (they're inside `tests/`, all 335 of which passed). The workflow's remaining steps (boot-assertion script, Cato-profile negative control, marketplace-default negative control) are shell-script assertions, not pytest — not re-executed here since they require importing the app's own `tools`/`runtime` modules with specific env-var profiles; nothing in this repo's code changed, so there is no reason to expect a different result than the last CI run on this commit. Flagging as **not independently re-verified in this session** rather than claiming it passed.

---

## 2. Static risk scan

### 2.1 Dead/stale agent registrations

No dead agent bundles found in `skill_bundles/*.json` — all 20+ bundles (`genesis-billing`, `genesis-builder`, `genesis-commerce`, `genesis-finance`, etc.) correspond to tool modules present under `tools/`.

**Finding (MEDIUM, doc hygiene):** `MISSING_DEPENDENCIES.md` at repo root documents missing imports (`agent_framework`, `agent_framework.azure`, `azure.identity.aio`, `anthropic`) for agent files `agents/builder_agent.py` and `agents/builder_agent_enhanced.py` under a directory tree (`apps/agents-gateway/agents/`) that **does not exist in this repository** — `git ls-files` and `find` both return nothing for any `agents/` directory or `builder_agent*.py` file here. This document was evidently carried over from a different (larger, monorepo-style) checkout and now describes files that aren't in this repo. It is stale and could mislead a future session into thinking a `builder_agent.py` exists to be fixed. Recommend deleting or clearly re-scoping this file — did not touch it, per no-edit scope.

### 2.2 Untested routes

`main.py` registers ~55 FastAPI routes. Cross-referencing route paths and handler function names against every test file (`tests/`, root `test_*.py`, `testing/test_job_lifecycle.py`) by two independent methods (literal path-string grep, then handler-function-name grep) found **zero references** to the following handler functions/routes:

- `verify_hash` (`GET /verify/hash`)
- `start_verification` / `get_verification_status` (`POST /verify`, `GET /verify/{job_id}`)
- `start_arbitrage_verification` / `get_arbitrage_verification_status` (`/internal/arbitrage/verification-jobs[/…]`)
- `a2a_handler` (`POST /a2a`) and `a2a_discovery` (`GET /.well-known/agents.json`)
- `demo_commerce_info`, `demo_escrow_flow`, `demo_trust_badge`, `demo_task_verify` (all `/demo/*`)
- `marketplace_search` (`GET /marketplace/search`)
- `store_buyer_session` (`POST /jobs/{job_id}/session`)
- `serve_artifact` (`GET /artifacts/{job_id}/{name}`)
- `verify_proof` (`GET /proofs/{proof_id}/verify`)
- `job_artifacts` (`GET /jobs/{job_id}/artifacts` — distinct from the tested `agents/jobs/{job_id}/artifacts`)

That's roughly a quarter of the public route surface with **no local unit or route-level test found**. This is not proof these routes are broken — some may be thin wrappers over already-tested store/tool functions — but it is proof the local suite gives zero direct evidence for them. The `/verify/*` and `/internal/arbitrage/*` group is the highest-priority gap since it's a payment-verification-adjacent surface (arbitrage settlement, proof verification) that a later phase's finance red-team should target explicitly rather than assume covered.

### 2.3 Undocumented env vars

Static-scanned every `os.getenv(...)`/`os.environ.get(...)`/`os.environ[...]` call across all `.py` files (excluding test files) and diffed against `.env.example` (both the `VAR=` lines and free-text prose mentions). **96 distinct env vars are read in code; 47 are declared in `.env.example`.** After removing OS-level noise (`PATH`, `USER`/`USERNAME` — only used as a local-Postgres-test username fallback in `tests/conftest.py`), **43 app-relevant env vars are read in source but never mentioned in `.env.example`**, including several that are security- or finance-adjacent and arguably belong in the reference file:

- **Secrets/credentials never documented:** `INTERNAL_SECRET` (used in `escrow_client.py` and twice in `main.py` — gates internal callback auth), `RENDER_INTERNAL_SECRET`, `GITHUB_TOKEN` (`tools/github_tool.py`), `CONDUIT_INVOICE_SECRET` (`conduit_verifier.py`), `TRIGGER_SECRET_KEY` (`trigger_dispatch.py`), `NAMECOM_TOKEN`/`NAMECOM_USERNAME`, `SENDGRID_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASS`, `SERPER_API_KEY`.
- **Sandbox security knobs never documented:** `GENESIS_SANDBOX_ALLOW_NETWORK`, `GENESIS_SANDBOX_MAX_TIMEOUT_S`, `GENESIS_SANDBOX_RLIMIT_AS_MB`, `GENESIS_SANDBOX_RLIMIT_CPU_S`, `GENESIS_SANDBOX_RLIMIT_FSIZE_MB`, `GENESIS_SANDBOX_RLIMIT_NPROC` (`runtime/sandbox_manager.py`) — an operator reading only `.env.example` would not know these exist or that they default to enabling network access off / a 1024MB memory cap, etc. Same category of risk as the documented vars — an operator changing sandbox posture would have to read source to find the knob.
- **Test-provisioning vars, lower risk but still undocumented:** `GENESIS_TEST_DATABASE_URL`, `GENESIS_TEST_PROVISION_POSTGRES`, `GENESIS_EVAL_LIVE_CHECK`.
- **Everything else** (`ENVIRONMENT`, `GENESIS_BASE_URL`, `GENESIS_COMMIT`, `GENESIS_LOCAL_ARTIFACT_DIR`, `GENESIS_S3_ENDPOINT`, `GENESIS_SESSION_VAULT_DIR`, `LEGACY_PERSONA_MODEL`, `LOG_LEVEL`, `RENDER_GIT_COMMIT`, `RENDER_INSTANCE_TYPE`, `RENDER_SERVICE_TYPE`, `RENDER_SERVICE_URL`, `SWARMSYNC_API_INTERNAL_URL`, `SWARMSYNC_API_URL`, `TRIGGER_API_URL`, `WORKER_CALLBACK_MAX_ATTEMPTS`, `WORKER_CALLBACK_TIMEOUT_S`, `WORKER_MODE`, `X402_PLATFORM_WALLET_ADDRESS`, `DATABASE_URL`) is lower-severity operational config.

**Risk:** `.env.example` is the primary onboarding/deploy-config surface. A `grep .env.example` audit for "what secrets does this service need" would currently miss `INTERNAL_SECRET`, `GITHUB_TOKEN`, `CONDUIT_INVOICE_SECRET`, and the sandbox rlimit knobs — all of which gate either an internal-trust boundary or the sandbox's resource/network containment.

### 2.4 Wildcard permissions

`runtime/request_auth.py::Principal.has_scope()` supports a literal `"*"` wildcard scope (`return scope in self.scopes or "*" in self.scopes`, and the same check is repeated inline at line 225). **No client record in `trusted_ap2_clients.json` currently grants `"*"`** — grepped the file directly, zero matches — so this path is currently dormant, not exploited. But **zero tests exercise this branch either way** (grepped `tests/` and all `test_*.py` for `"*"` used as a scope value — no hits). That means: (a) no regression test would catch a future config change accidentally granting `"*"` to a client, and (b) no test proves the wildcard branch's behavior is intentional/bounded (e.g., does `"*"` bypass tenant/owner scoping too, or only the named-scope check?). Recommend a permission-matrix test that explicitly asserts no live client can hold `"*"` and that a hypothetical `"*"` grant is still tenant/owner-scoped, not a full bypass.

### 2.5 Direct finance-system calls bypassing `escrow_guard.py`

Checked every file importing/mentioning `xero`/`stripe`/`payment` (`agent_runtime.py`, `capability_cards.py`, `eval/genesis_client.py`, `eval/run_experiment.py`, `main.py`, `runtime/tool_policy.py`, `tools/billing_tool.py`, `tools/commerce_tool.py`, `tools/data_pipeline_tool.py`, `tools/finance_tool.py`). Found **no bypass**: `tools/finance_tool.py` and `tools/billing_tool.py` are explicitly self-documented as read/report-only (`"RECORDS. No function in this module constructs or transmits a payment."`), and `finance_import_x402_transactions` / `billing_import_ar_ledger` both explicitly quarantine themselves as "NOT IMPLEMENTED" pending contract sign-off rather than silently importing. `escrow_client.py` (the only module allowed to touch escrow state) is separately containment-tested by `tests/test_escrow_containment.py`, which passed. This matches the doctrine — no finding here beyond confirming it holds.

### 2.6 Hidden fallback paths

Two are explicitly self-documented in `.env.example` and worth re-flagging because they're security-relevant and easy to trigger by accident:
1. Without `GENESIS_PRINCIPAL_TOKEN_KEY` set, "every AP2-signed request from Cato fails closed with HTTP 401, and the gateway silently falls back to the shared-key legacy principal, which cannot read owner-scoped jobs" — i.e., a missing key doesn't just weaken auth, it silently reroutes to a different, lower-privilege principal rather than failing the request outright. Confirmed this is asserted by `tests/test_request_auth.py::test_startup_refuses_to_boot_without_signed_identity_material`, so the boot-time guard is real; the finding is purely about how easy it'd be to miss in an ops runbook if that env var got unset post-deploy without a restart.
2. `GENESIS_ALLOW_OPENROUTER_FALLBACK` (default `false`) — an LLM-provider fallback path exists in `main.py` (`_openrouter fallback` reads `OPENROUTER_API_KEY`, which per §2.3 is itself undocumented in `.env.example`). Off by default, so not a live risk, but the fallback and its credential are two separate undocumented/under-visible things stacked on each other.

### 2.7 Swallowed exceptions

**Zero bare `except:` clauses** anywhere in production source (confirmed by full-repo grep). `except Exception:` appears 24 times outside test files. Manually inspected every occurrence; the large majority are legitimate defensive cleanup (`rollback()`/`close()` in `finally`-style paths in `runtime/pg_store.py`, `runtime/genesis_audit.py`, tracing spans in `runtime/phoenix_tracing.py`/`runtime/observability.py` marked `# pragma: no cover — must never break dispatch`). Two are worth flagging:

- `agent_runtime.py:659` — swallows any exception while parsing `usage.get("total_tokens", ...)` from an LLM response, inside the token-budget enforcement path (`MAX_TOKENS_PER_JOB` check immediately follows). If usage parsing throws for a malformed/unexpected response shape, that turn's tokens silently don't count toward the budget rather than failing closed. Low-severity on its own (budget enforcement is presumably reinforced elsewhere), but it's the one `except Exception: pass` that sits directly next to a cost-control gate rather than a logging/cleanup path — worth a follow-up test that feeds a malformed `usage` field and asserts the budget check still fails safe rather than under-counting.
- `main.py:1617` — swallows failure to deliver the arbitrage-verification **failure callback** itself (`X-Internal-Secret`-authenticated POST to `SWARMSYNC_API_URL`). If the failure-notification POST fails, there is no log line at all — a caller could be left waiting on a webhook that will never arrive, with nothing in Genesis's own logs to show why.

None of these are `except Exception: pass` next to auth, escrow, or audit-chain code — those paths (`runtime/request_auth.py`, `escrow_client.py`, `runtime/genesis_audit.py`'s append path) do not swallow.

### 2.8 TODO/FIXME near auth/state/retry/security

**Zero** `TODO`/`FIXME`/`XXX` comments anywhere in production `.py` source (root, `runtime/`, `tools/`, `migrations/`). None in test files either. Either the repo has a policy against leaving them in, or they're tracked elsewhere (e.g. `IMPLEMENTATION_PLAN.md`, `.ralph/`) — not a finding, just confirming the scan came back clean.

### 2.9 Skipped/disabled tests

No `@pytest.mark.skip` or `xfail` decorators found in the codebase — every one of the 63 skips observed in the run is a **runtime `pytest.skip(...)` gated on environment availability**, not a hard-coded disable. Breakdown:

| Count | Reason | Read |
|---|---|---|
| 48 | "no real PostgreSQL configured" (`tests/test_postgres_stores.py`, `test_audit_chain_integrity.py`, `test_migration_runner.py`, `test_store_failure_modes.py`) — deliberately refuse to run Postgres-semantics assertions (unique violations, advisory locks, transactional DDL) against SQLite/mocks | Legitimate — the test file docstrings explicitly reject faking these against SQLite. `GENESIS_TEST_DATABASE_URL` or `GENESIS_TEST_PROVISION_POSTGRES=1` unlocks them. **Not exercised in this run** — see §3 for the one adjacent test that *did* provision real Postgres. |
| 10 | "rubric engine not available" (`test_conduit_verifier.py`, Track 2 content-rubric verification) | **See finding below — this is not routine environment-gating, it's a missing module.** |
| 2 | "needs Postgres" (`test_job_store.py`) | Same category as the 48 above. |
| 1 | `ASSISTANT_PG_DATABASE_URL not set` (`test_retrieval_route.py`) | Expected — that's an externally-provisioned e4l-work-os/Cato database, documented in `.env.example` as optionally unset locally. |
| 1 | `requires functional Linux bwrap` (`test_sandbox_manager.py`) | Expected on Windows; this is a Linux-namespace sandbox test. |
| 1 | `set GENESIS_EVAL_LIVE_CHECK=1 to reconcile against the live gateway` (`eval/tests/test_live_catalogue.py`) | Correctly gated — this would hit the live gateway, out of scope here. |

**Finding (HIGH, needs owner confirmation):** the 10 "rubric engine not available" skips in `test_conduit_verifier.py` are not simply an unset env var. `conduit_verifier.py` conditionally imports `conduit/tools/rubric.py` relative to the gateway root; **that file, and the entire `conduit/` directory it expects, do not exist anywhere in this repository** — confirmed via `find`, `git ls-files conduit/`, and `git log -- conduit/`, all empty. A prior cleanup commit (`test_orphan_submodule_and_colliding_package_are_removed`, which passed in this run) explicitly asserts the old `conduit-browser` git submodule and `.gitmodules` are gone — this looks like a leftover reference from before that removal, or a module that's only ever provisioned at deploy time by a separate process and never checked into this repo. Either way: **Track 2 (rubric-based content verification) has zero test coverage in this checkout, and there is no local evidence one way or the other about whether it works in production.** This should be resolved (confirm the deploy-time provisioning story, or delete the dead conditional-import path) before Track 2 verification can be claimed as certified in a later phase.

### 2.10 Mock-only integrations presented as real

`test_meta_orchestration_trace.py` explicitly documents its own honesty boundary in its module docstring: "Mocks the LLM network calls but does NOT mock `genesis_call` itself... proves the full tool-call chain and trace recording." That's a good pattern — worth calling out because it's exactly the kind of self-disclosure the doctrine wants, and because it directly bears on the HKO deferred-finding check in §3 below (it proves the delegation *mechanism* locally, but is still not a live agent invocation).

Beyond that one self-disclosed case, no other test file in the local suite claims "real"/"live"/"production" behavior while actually mocking the underlying call — spot-checked `test_llm_transport_resolver.py`, `test_runtime_hardening.py`, and `eval/tests/test_target.py` for this pattern and found none. (This was a targeted check, not exhaustive of all 951 tests — a full mock-vs-real audit belongs in a later certification phase per the doctrine's Phase 3/4.)

### 2.11 Stale infra references (Supabase / Azure / LangSmith / Docker Desktop given canonical infra is Render + Postgres)

- **LangSmith:** every reference found (`runtime/phoenix_tracing.py`, `eval/traceable.py`, `tests/test_phoenix_tracing.py`, `eval/EVALUATION.md`) is either historical documentation of the migration *away* from LangSmith, or an active regression test — `tests/test_phoenix_tracing.py::test_phoenix_replaced_langsmith_outright` and `::test_no_langsmith_runtime_coupling_remains` — that sets `LANGSMITH_API_KEY`/`LANGSMITH_TRACING` env vars and asserts they have zero effect and that no module imports `langsmith` or reads a `LANGSMITH_*` var. Both passed. **Not stale — this is the desired end state, actively guarded.**
- **Supabase:** the only reference (`test_job_store.py`) is a fixture connection string (`postgresql://…@example.supabase.co:6543/postgres?pgbouncer=true&connection_limit=1`) used to prove `pgbouncer`/`connection_limit` query params get stripped before use. `.env.example` explicitly states "Pooled Supabase/Prisma URLs are fine" for `GENESIS_JOB_DATABASE_URL` — Genesis's canonical Postgres can legitimately *be* Supabase-hosted; this isn't confusion about a different platform, it's compatibility with one valid Postgres hosting option. Not a finding.
- **Azure:** no reference in any Genesis runtime/tool/route source file. Only appears in `MISSING_DEPENDENCIES.md` (already flagged as stale/out-of-scope in §2.1) and in `ralph/` planning-doc subdirectories, which are working notes for other build tracks, not shipped code.
- **Docker Desktop:** zero references anywhere in the repo.

No corrective action needed beyond the `MISSING_DEPENDENCIES.md` cleanup already flagged in §2.1.

---

## 3. HKO_CERTIFICATE.md (2026-06-13) deferred findings — resolved or not?

The prior certificate listed the repo as **CONDITIONAL** with two explicit conditions for full PASS:

**Condition 1 — "Add one integration test that hits the real genesis_jobs Postgres table (not mocked)."**
**RESOLVED**, with evidence from this run. `tests/test_genesis_migration.py::test_fresh_postgres_migration_supports_production_store_crud` **passed** in the full suite (it was not among the 63 skips). Read the test body directly: it locates real `initdb`/`pg_ctl`/`createdb`/`psql` binaries (found at `C:\Program Files\PostgreSQL\15\bin` on this machine), spins up an ephemeral local Postgres cluster on a free port, applies the actual `migrations/001_genesis_runtime.sql` migration (which creates `genesis_jobs` and five other runtime tables), and then runs store-family CRUD against it. This is a genuine, non-mocked integration test — it only self-skips (`pytest.skip("local PostgreSQL binaries are required...")`) when no local Postgres install exists, which was not the case here. **Condition 1 is closed.**

**Condition 2 — "Add a Meta delegation eval that verifies `trace.tool_calls` contains a real `genesis_call` dispatch (requires live agent invocation in CI or staging)."**
**PARTIALLY RESOLVED — the mechanism is now locally proven, but the literal "live" requirement is not met by anything in this local suite.** `test_meta_orchestration_trace.py` (7 tests, all passed) does exactly what the condition's first half asks: it dispatches through the real `AgentRuntime`/`genesis_call` code path (not mocked) and asserts the resulting trace records the tool-call chain — its own docstring says so explicitly (quoted in §2.10). What it does **not** do is the condition's second half: a live agent invocation against a running gateway/model in CI or staging. That capability exists only in `testing/live_real_agent_tests.py`, which this task was explicitly instructed not to run. So: the delegation-tracing *mechanism* is certified as correct by a real (mocked-LLM, real-dispatch) local test; whether that mechanism holds under an actual live multi-agent run through the deployed gateway is still unverified by anything executed in this session, and is squarely Phase 3/8/9 work in the broader certification doctrine (single-agent and multi-agent workflow certification against the real deployment), not Phase 2.

**Net:** 1 of 2 conditions fully closed; the other is meaningfully advanced (mechanism proven locally) but still requires a live-environment test run — explicitly out of this task's scope — before it can be called closed. Recommend the certification owner treat Condition 2 as "downgraded from deferred-blocker to Phase 9 task" rather than "resolved."

---

## 4. Summary

- **Test run: 888 passed, 0 failed, 63 skipped, exit code 0.** No test files were edited. No live deployment was touched.
- **No lint/typecheck config exists in the repo** — none run, per scope (don't introduce new tooling).
- **Highest-priority risk-scan findings to carry into the next phase:**
  1. Track 2 rubric-based content verification (`conduit_verifier.py` → `conduit/tools/rubric.py`) references a module that does not exist anywhere in this repo — 10 tests skip because of it, and there is zero local evidence it works. (§2.9)
  2. ~15 routes / handler functions in `main.py`, including the entire `/verify/*` and `/internal/arbitrage/*` verification-lifecycle surface, have no local test reference by path or function name. (§2.2)
  3. `.env.example` is missing 43 env vars actually read in source, including security-relevant ones (`INTERNAL_SECRET`, `GITHUB_TOKEN`, `CONDUIT_INVOICE_SECRET`, sandbox rlimit/network knobs). (§2.3)
  4. `MISSING_DEPENDENCIES.md` describes files that don't exist in this repo — stale, should be deleted or rescoped. (§2.1)
  5. HKO deferred finding #2 (live Meta-delegation trace) is not closed by anything runnable locally — carry forward explicitly as Phase 9 work, don't let it get silently marked done because a same-named local test now exists. (§3)
- **No escrow/finance bypass found.** **No bare excepts, no TODO/FIXME near security code, no hard-disabled tests.** These are clean.

This report does not itself certify Genesis. It establishes the floor the doctrine requires before Phases 3–17 (single-agent, tool, permission/finance red-team, injection, retrieval, multi-agent, Cato/FinanceOS E2E, durability, chaos, concurrency, and production-like scenario testing against the real deployment) can begin.
