# Guardrails — Known Risks and Scope Exclusions

ralph: scan before every action.

Sources: `specs/SPEC-genesis-azure-phoenix-migration.md`, parent Azure SPEC, architecture map 2026-08-05.

## Pre-Loaded Risks

### SIGN: Touching agent logic or tool policy
`runtime/tool_policy.py`, `agent_runtime.py`, skill bundles, and 57 slug behaviors are OUT OF SCOPE.
Mitigation: hosting/deploy/observability only.

### SIGN: Phoenix blocks agent execution
Tracing must remain fire-and-forget; unreachable Phoenix cannot fail `/agents/*/run`.
Mitigation: keep degradation tests green; never await exporter on critical path.

### SIGN: Secrets in traces
`eval/redaction.py` must run before export; tests must stay green.
Mitigation: run secret-redaction tests every iteration.

### SIGN: Breaking money-path guards
`tests/test_prohibited_tools.py` and `tests/test_escrow_containment.py` are mandatory.
Mitigation: validation gate always includes them.

### SIGN: Multi-replica without store rewrite
Phase 1 = single ACA replica. `_verification_jobs`, workspace registry, etc. stay in-memory.
Mitigation: document constraint in CHUNK_5; do not scale replicas until stores fixed.

### SIGN: Planning Azure portal work as code
Provisioning ACA/Key Vault/DNS is HUMAN_GATES.
Mitigation: read `.ralph/HUMAN_GATES.md` each iteration.

## Scope Exclusions — Do Not Build

- DO NOT BUILD: `runtime/tool_policy.py` / `agent_runtime.py` / skill bundle changes
- DO NOT BUILD: FinanceOS or Cato repo edits
- DO NOT BUILD: Migrating Genesis job Postgres (SwarmSync-owned)
- DO NOT BUILD: LangSmith reintroduction
- DO NOT BUILD: Multi-replica in-memory store fixes (Phase 1)
- DO NOT BUILD: Azure resource provisioning in Python code

## Standing Guardrails (always active)

- DO NOT skip the validation gate.
- DO NOT commit with --no-verify.
- DO NOT add dependencies without updating AGENTS.md.
- DO NOT hard-code secrets.
- DO NOT modify files outside the current task scope.

## Accumulation Instructions

### Learned: {SHORT_TITLE}
{what went wrong and how to avoid it}
