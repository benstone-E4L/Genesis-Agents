# Genesis Agents — Regression Tests Register

Generated: 2026-08-15

The following regression test suites enforce the permanent certification boundaries:
- `test_tool_risk_coverage.py`: Enforces that no unauthorized slug gains `RISK_DEPLOYMENT` or `RISK_PAYMENT`.
- `tests/test_runtime_integrity.py`: Enforces delegation depth limits, chain tracking, and parent risk containment during `genesis_call`.
- `test_tool_policy_matrix.py` + `expected_policy_matrix.json`: Pinned 1,400-pair permission matrix preventing permission regression.
- `test_bundle_tool_registry.py`: Validates that every tool advertised in `skill_bundles/*.json` is registered and callable.
- `test_data_pipeline_tool.py`: Validates Data Agent Kit tool handlers and schema discovery.
- `scripts/cato_master_orchestrator_eval.py`: Automated 24-agent Cato Ed25519 AP2 signed end-to-end evaluation runner.
