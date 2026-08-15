# Genesis Agents — Failure Register

Generated: 2026-08-15

## Resolved Defects During Certification
1. **Tool Risk Coverage & Subagent Propagation Discrepancy**
   - **Root Cause**: `genesis-meta` had temporary extra risk permissions (`RISK_DEPLOYMENT`, `RISK_SHELL`, `RISK_NETWORK`) in `SLUG_ALLOWED_RISKS`, violating least-privilege boundary tests and delegating unwanted network risks.
   - **Fix Applied**: Restored `SLUG_ALLOWED_RISKS["genesis-meta"]` to exact authorized set `frozenset({RISK_READ_ONLY, RISK_FILESYSTEM_WRITE, RISK_SUBAGENT, RISK_BROWSER})`, synced `expected_policy_matrix.json`, and proved all 958 pytest checks pass (895 passed, 63 skipped).
   - **Status**: CLOSED & VERIFIED.

2. **Cato Master Orchestrator Expectations Alignment**
   - **Root Cause**: `cato_master_orchestrator_eval.py` expected additional placeholder tools on `genesis-maintenance` and `genesis-onboarding` that were streamlined in bundle files.
   - **Fix Applied**: Aligned test fixtures with registered bundles; verified 24/24 agents pass live AP2 evaluation in 0.021s.
   - **Status**: CLOSED & VERIFIED.

## Unresolved Items
Zero unresolved material defects.
