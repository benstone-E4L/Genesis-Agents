# HKO-Truth-Audit Certificate: Genesis Agents Runtime Hardening
**Date:** 2026-06-13
**Audit target:** Genesis Agents repo — Phases 1–10 implementation
**Severity threshold:** HIGH

| Layer | Findings | Critical/High |
|-------|----------|--------------|
| HK (Code) | 3 | 2 HIGH (both fixed) |
| OTA (Contract) | 2 | 2 functional (both fixed) |
| RIO (Integration) | 3 | 1 broken (fixed), 2 partial (deferred) |
| MULTI (overlap) | 1 | 1 CAUSAL LINK (fixed) |
| CAUSAL LINKs | 1 | worker.py status/log mismatch |
| HK Coverage | COMPLETE | — |

**Overall result: CONDITIONAL**

> No CRITICAL findings. 2 HIGH findings resolved before certificate issuance. CONDITIONAL because 2 partial findings (E2E real-DB integration, Meta delegation proof via live run) remain deferred — acceptable for current stage.

**Conditions for full PASS:**
1. Add one integration test that hits the real genesis_jobs Postgres table (not mocked).
2. Add a Meta delegation eval that verifies `trace.tool_calls` contains a real `genesis_call` dispatch (requires live agent invocation in CI or staging).

**Residual risks (even at CONDITIONAL):**
1. `workspace_shell` blocks known dangerous absolute paths only by prefix/substring — a creative agent could still write to `/tmp/jobs/other_job_id` if it knows another job's ID. Full isolation requires OS-level namespace/container sandboxing per job.
2. The `_SECRET_VALUE_RE` covers known key prefixes but not arbitrary high-entropy secrets (e.g. a random 40-char hex token). Full secret detection would require entropy analysis.
3. The `/health/browser` and `/health/worker` endpoints are read-only observers — a worker that crashes without updating `_worker_state` would show stale healthy data until the next restart.

---

## VerifyAPI Proof Record
**Status:** SKIPPED — VerifyAPI submission not run in this session.
This certificate reflects local findings only. To obtain a proof chain entry, re-run Phase 5.5 when connectivity to api.swarmsync.ai is available.
