# Guardrails — Known Risks and Scope Exclusions

ralph: before taking any action, scan this file. If your action matches a SIGN, stop and report.

## Pre-Loaded Risks

### SIGN: Cross-repository edit
Every chunk has exactly one owning repository. CHUNK_1 may edit FinanceOS only; CHUNK_2 through CHUNK_4 may edit Genesis only.
Mitigation: check the current chunk, working directory, Git root, and staged paths before every commit.

### SIGN: Duplicate completed FinanceOS integration
FinanceOS Blob, Registry, Airtable, Metabase, Phoenix, and financial adapter implementation is excluded from this workspace.
Mitigation: CHUNK_1 adds regression tests only. Report a genuine production defect before requesting expanded scope.

### SIGN: Financial connector through Composio
Xero, Stripe, Gusto, Expensify, banking, cards, payments, payroll, tax filing, bookkeeping, and equivalents are prohibited even through aliases or provider-qualified names.
Mitigation: normalize names, default deny, and prove denial happens before any SDK call.

### SIGN: Policy bypass
Composio beside or ahead of `runtime/tool_policy.py` creates a parallel permission system.
Mitigation: require the existing tool-policy decision before adapter resolution or invocation.

### SIGN: Silent local artifact fallback
Falling back to local disk when production remote storage is expected can create false upload success.
Mitigation: retain local storage only as an explicit development/test backend; fail closed on missing production Blob configuration.

### SIGN: Secret or signed-link leakage
Azure connection strings, SAS tokens, access keys, Composio credentials, authorization headers, provider payload secrets, and full signed URLs must not enter logs, fixtures, commits, or evidence.
Mitigation: use obvious fake values and assert redaction in failure tests.

### SIGN: Genesis escrow baseline hidden by broad deselection
At the scaffolded exact HEAD, three pre-existing escrow-policy tests fail while the other 606 tests pass and 15 skip: `test_escrow_settled_on_success_no_callback`, `test_escrow_released_on_failure`, and `test_timeout_triggers_escrow_release` in `testing/test_job_lifecycle.py`.
Mitigation: deselect only those three exact node IDs, never the file or a keyword class; rerun them without deselection after any escrow-policy change and remove each exclusion as soon as its baseline is repaired.

## Scope Exclusions — Do Not Build

- DO NOT BUILD FinanceOS Blob, Document Registry, Airtable, Metabase, Phoenix, Xero, Stripe, Gusto, or Expensify implementation.
- DO NOT BUILD Document Intelligence; it requires a later evidence-backed delta specification.
- DO NOT BUILD or alter Genesis hosting, Azure Container Apps, Trigger.dev dispatch, or FinanceOS scheduling.
- DO NOT perform operational credential provisioning or live service tests as a code chunk.
- DO NOT modify Cato, existing Ralph workspaces, Vault status/evidence, or unrelated dirty files.

## Standing Guardrails

- DO NOT add dependencies without pinning them using the owning repository's convention.
- DO NOT skip the validation gate, use `--no-verify`, or emit completion on failed/skipped validation.
- DO NOT generate code for a future chunk or refactor outside the current task.
- DO NOT stage with `git add -A`; stage only the current task's named files.
- DO NOT claim live readiness from mocked tests or prose.

## Accumulation Instructions

Append newly proven failure patterns below with the sign, evidence, and mitigation.
