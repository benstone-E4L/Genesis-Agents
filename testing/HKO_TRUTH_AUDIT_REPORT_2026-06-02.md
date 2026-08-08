# HKO-Truth-Audit Report — Genesis routing + Research async (2026-06-02)

**Targets:** `main.py`, `test_gateway_error_mapping.py`, SwarmSync `routing.controller.ts`  
**Mode:** RIO + inline HK (no run transcript for OTA)

## Task status

| Task | Status | Evidence |
|------|--------|----------|
| Remove Gemini direct API bypass | implemented | `main.py` — no `generativelanguage.googleapis.com`; `test_main_py_has_no_direct_google_generative_language_api` PASS |
| Router-only persona/negotiate | implemented | `call_llm_router` sends Bearer + `X-Title`; negotiate returns default on router errors |
| SwarmSync IP throttle exemption | implemented | `routing.controller.ts` `@SkipThrottle`; `routing.controller.spec.ts` PASS |
| Research async job path | implemented | `genesis-research.json` `job_mode: "async"`; `main.py:1394+`; research in `test_conduit_heavy_run_requests_enqueue_async_jobs` PASS |

## Verification summary

| Command | Result |
|---------|--------|
| `python -m pytest test_gateway_error_mapping.py test_agent_runtime.py -q` | 21 passed |
| `npx jest src/modules/routing/routing.controller.spec.ts` | 2 passed |

## Unified findings

| Severity | Source | Finding | Fix |
|----------|--------|---------|-----|
| — | — | No CRITICAL/HIGH code or integration gaps in scoped changes | — |

## Residual risks

1. **Live Render:** Async Research still needs worker + `GENESIS_JOB_DATABASE_URL` running in production — code path proven locally only.
2. **Per-key limits:** FIX-03 removes shared-IP starvation; a single `sk-ss-*` key can still hit 60 req/min (`PerKeyRateLimitGuard`).
3. **Negotiate on 429:** Returns auto-REJECTED instead of retrying — intentional; no side-channel bypass.

## Certificate

**Overall:** PASS (scoped implementation + local verification)

**OTA:** DESIGN-TIME — execution honesty inferred from pytest/jest, not a production transcript.
