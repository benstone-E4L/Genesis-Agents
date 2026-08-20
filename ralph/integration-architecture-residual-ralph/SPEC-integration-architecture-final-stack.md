# SPEC: Integration Architecture — Residual Final Stack

## Metadata

- Version: 2.0
- Revised: 2026-08-12
- Tier: FULL — this document protects financial-write, credential, artifact, and audit boundaries.
- Status: Ralph prep scaffolded for the residual CODE work; implementation not started.
- Replaces: version 1.1 dated 2026-08-07.
- Repositories:
  - FinanceOS: `C:\Users\Work\Desktop\vault\projects\E4L-FinanceOS\app`
  - Genesis Agents: `C:\Users\Work\Desktop\vault\projects\My Github\Genesis Agents`
- Purpose of this revision: reconcile the proposed stack with the code that now exists, remove duplicate build work, and split the remaining work by the repository that owns it.
- Status authority: this specification states scope and acceptance only. Current operational status comes from `vault-next` structured state and evidence.

## 1. Executive Summary

Most of the FinanceOS integration work described by version 1.1 is now implemented or has its own Ralph workspace. It must not be built again from this specification. FinanceOS already contains an Azure Blob client and document-ingestion path, Document Registry schema/API work, and an Airtable client plus `document.airtable-sync` job. Metabase has a separate seven-chunk workspace. Remaining Document Registry work is governed by its own Ralph workspace.

The active CODE work in this specification is therefore narrow:

1. **FinanceOS:** add an automated architecture-boundary test pack. It protects the already-established rules but does not rebuild Blob, Registry, Airtable, Metabase, Phoenix, or financial adapters.
2. **Genesis Agents:** replace its current boto3/S3 artifact backend with Azure Blob while preserving the public artifact-store behavior and safe local-development fallback.
3. **Genesis Agents:** add Composio for approved non-financial tools only, routed through Genesis's existing tool-policy gate.
4. **Genesis Agents:** add regression and contract tests proving those two changes cannot create a financial authority or bypass existing policy.

These are separate repository builds. A Ralph workspace may coordinate their acceptance contract, but a chunk must modify only one repository.

## 2. Evidence-Based Reconciliation

### 2.1 As-built, completed, deferred, and residual table

| Component | Evidence observed 2026-08-12 | Classification | Owner / action |
|---|---|---|---|
| FinanceOS Postgres authority | FinanceOS uses Postgres for durable financial state and pg-boss jobs; the Azure database cutover is separately evidenced in the project state | **AS-BUILT — do not rebuild** | FinanceOS. Preserve the boundary. |
| FinanceOS Azure Blob client | `src/modules/dataroom/blob.ts` uses `BlobServiceClient`, uploads bytes, and returns the accepted SHA-256; `registerDocument` in `src/modules/dataroom/index.ts` writes the blob and verifies its hash | **AS-BUILT — do not rebuild** | FinanceOS. Only regression protection remains here. |
| Document Registry schema/API | Commits `34e1424` and `6b361cd` added schema and read-only Registry API work | **AS-BUILT for those slices — do not rebuild** | FinanceOS. Other Registry slices remain owned by `assistant-architecture/ralph/document-registry-ralph`, not this spec. |
| FinanceOS Airtable client and document sync | Commit `48f3018`; `src/connectors/airtable/client.ts`, `src/modules/dataroom/airtable-document-sync.ts`, and the `document.airtable-sync` pg-boss registration exist | **AS-BUILT — do not rebuild** | FinanceOS. Live base access/configuration is an OPERATIONAL gate. |
| Metabase reporting | A dedicated workspace exists at `specs/Ralph Chunks/metabase-reporting-layer` and is `NOT_STARTED` | **ALLOCATED ELSEWHERE — do not duplicate** | The Metabase workspace owns this work. |
| Trigger.dev | `trigger_dispatch.py` and the Trigger.dev job path already exist in Genesis | **AS-BUILT — do not rebuild** | Genesis. Preserve; do not introduce Trigger.dev into FinanceOS. |
| Genesis artifact storage | `artifact_store.py` imports boto3, reads `GENESIS_S3_*`, emits `s3://` URIs, and falls back to local disk; `requirements.txt` includes boto3 | **RESIDUAL CODE** | Genesis: migrate the remote backend to Azure Blob without assuming an Azure application host. |
| Composio | No Composio implementation or dependency was found in Genesis at the inspected HEAD | **RESIDUAL CODE** | Genesis: add it only for the approved non-financial connector set. |
| FinanceOS/Genesis hosting | Genesis commit `3a84603` removes the unused Azure Container Apps/Phoenix migration workspace | **OUT OF SCOPE** | This spec makes no Genesis ACA assumption. Hosting is governed separately. |
| Phoenix | Observability only; it is not a financial or audit authority | **BOUNDARY ONLY** | Do not create a Phoenix build in this spec. Protect fail-open observability behavior. |
| Azure Document Intelligence | InvoiceProof extraction quality has no accepted evidence gate result in this workstream | **DEFERRED / GATED** | No build chunk. Run the gate in section 8; create a new delta spec only if it passes the build threshold. |
| Remaining Document Registry / Drive sync / migration | The dedicated Registry workspace records some chunks complete and others not started | **ALLOCATED ELSEWHERE** | Do not pull those chunks into this spec. |
| Live Airtable base access | Code can be configured, but evidence notes that the token does not yet prove usable real-base access | **OWNER-BLOCKED OPERATIONAL** | No replacement client or parallel sync job. Resolve access and run the existing live path. |

### 2.2 Authority rule

FinanceOS Postgres is the only financial authority. Genesis, Composio, Trigger.dev, Airtable, Azure Blob, Phoenix, Metabase, and Document Intelligence are consumers, execution aids, stores, or views. None may become the record of a financial approval, scheduled finance job, audit decision, or write authorization.

## 3. Non-Negotiable Boundaries

1. FinanceOS Postgres remains the authority for schedules, approvals, audit rows, financial state, and proof metadata.
2. pg-boss remains FinanceOS's only queue and scheduler. Trigger.dev, Temporal, Durable Functions, or a Composio scheduler must not enter the FinanceOS process boundary.
3. Composio is **Genesis-only** and **non-financial-only**. It must not expose or invoke Xero, Stripe, Gusto, Expensify, banking, payment, payroll, or bookkeeping tools.
4. Xero, Stripe, Gusto, and Expensify remain direct FinanceOS adapters governed by FinanceOS approval and capability policy.
5. Genesis's existing `runtime/tool_policy.py` remains the policy authority for agent tool access. Composio must be behind that gate, not beside it.
6. Azure Blob stores bytes. A blob URI, object, or provider response is not an approval or audit record.
7. Phoenix stores traces/evaluations only. A Phoenix outage must never block an approval, job, or write.
8. Airtable is a human workbench. It must not hold financial-write credentials, write FinanceOS Postgres directly, or become the durable record of an approval.
9. Metabase is read-only and remains governed by its existing Ralph workspace.
10. No Genesis hosting move is implied. Azure Blob can be consumed from the current approved Genesis host.

### Accounting specialization (2026-08-19)

E4L accounting specialists live in `Genesis Agents/accounting/` and `skill_bundles/genesis-e4l-*.json` (14 guarded agents). They use the host Xero MCP. They do not add a FinanceOS write path, a Genesis Xero client, or Composio Xero. `genesis-e4l-accounting` (one-hat/profiles) is rejected. Do not reopen CHUNK_1–4 for this.

## 4. Scope Classification

Standing orders require each phase to be classified before Ralph prep.

### CODE — becomes Ralph chunks

- **FinanceOS repository:** automated architecture-boundary tests only.
- **Genesis repository:** Azure Blob artifact backend and migration-safe compatibility behavior.
- **Genesis repository:** Composio connector/auth adapter for approved non-financial services.
- **Genesis repository:** policy, redaction, failure-mode, and regression tests for both integrations.

### OPERATIONAL — explicit gates, never code chunks

- Provision Genesis Azure Blob container access in the actual approved hosting environment.
- Provision Composio project credentials and approved connections.
- Confirm live Airtable token/base/table access and exercise the existing FinanceOS sync job.
- Run one real artifact round trip and one real approved Composio read action after deployment.
- Record credentials in the approved secret store; never in source, Ralph files, logs, or evidence text.

### OUT OF REPO / ALLOCATED ELSEWHERE — no chunks here

- Metabase roles, views, dashboards, and live configuration.
- Remaining Document Registry, Google Drive sync, historical migration, and cross-consumer Registry work.
- Azure/Phoenix hosting migration work.
- Accounting framework and `combining_run` work.
- Cato UI/workflow work.
- Document Intelligence implementation unless and until its evidence gate authorizes a separate delta specification.

## 5. Residual Build A — FinanceOS Boundary Tests

### Objective

Protect the architecture already built without rebuilding any integration.

### Allowed changes

- A focused test file under the existing FinanceOS test layout.
- Minimal package-script wiring only if required to ensure the test runs in `npm run verify`.
- No production integration code unless a test exposes a genuine boundary defect; any such defect must be reported before expanding scope.

### Required assertions

- FinanceOS production source and dependencies do not import Trigger.dev or Composio.
- FinanceOS does not expose a Composio code path to Xero, Stripe, Gusto, or Expensify.
- The existing Airtable path cannot write FinanceOS Postgres directly and remains a workbench/document-reference sync.
- Phoenix instrumentation is non-blocking where it is invoked by covered FinanceOS paths.
- Existing payment and financial-write approval tests remain green.

### Acceptance

- `npm run verify` passes at exact HEAD.
- The new test fails if a prohibited Trigger.dev or Composio production dependency is introduced.
- The test contains no live secret and makes no external network call.
- No Blob, Registry, Airtable, Metabase, or Phoenix implementation is duplicated.

## 6. Residual Build B — Genesis Azure Blob Artifact Backend

### Objective

Replace Genesis's boto3/S3 remote artifact backend with Azure Blob while preserving the behavior relied on by callers of `artifact_store.py`.

### Functional requirements

- Preserve the existing artifact-store public operations and response semantics used by Genesis callers: store, retrieve/download, and list by job.
- Use Azure Blob for the configured remote backend.
- Continue to support an explicit local-development/test backend; it must not silently become the production backend when remote configuration is expected.
- Store artifacts under a deterministic job-scoped path equivalent to the existing `{job_id}/...` organization.
- Preserve content type, filename, size, and integrity metadata where available.
- Return a provider-neutral artifact reference to internal callers. Provider-specific Blob URLs must not become permanent cross-system identifiers.
- Never log connection strings, SAS tokens, access keys, or full signed URLs.
- Remove boto3 and obsolete `GENESIS_S3_*` configuration only after all active callers and tests use the new backend.
- The change must not move Genesis to Azure Container Apps and must not alter Trigger.dev dispatch behavior.

### Failure behavior

- Missing production Blob configuration fails closed with a clear non-secret error; no invisible production write to local disk.
- Transient Azure errors use bounded retry behavior and surface an actionable failure.
- A partial upload is not reported as a completed artifact.
- Listing an empty job returns the existing empty-result shape rather than an exception.
- An artifact whose integrity metadata does not match downloaded bytes is rejected and logged without secret material.

### Acceptance

- Existing artifact-store tests are updated and pass.
- New tests cover upload, download, list, empty list, missing configuration, provider failure, and redaction.
- `rg -i "boto3|GENESIS_S3_|s3://"` has no active production artifact-backend hits after migration; historical prose may remain only if clearly labeled.
- `azure-storage-blob` is pinned using the Genesis repository's dependency convention.
- The complete Genesis bounded test command from its repository instructions passes.

## 7. Residual Build C — Genesis Composio Adapter

### Objective

Add a connector/auth adapter for Genesis agents to use approved non-financial services without creating a parallel permission system.

### Approved connector classes

- Gmail
- Google Drive
- Slack
- Monday.com
- Google Calendar
- GitHub

The concrete action allowlist must be narrower than the provider allowlist. Each enabled action requires an explicit policy entry and test.

### Prohibited connector classes

- Xero
- Stripe
- Gusto
- Expensify
- banks, cards, payments, payroll, tax filing, bookkeeping, or any equivalent financial-write-adjacent provider

Name matching must be normalized so case, punctuation, aliases, or provider-qualified names cannot bypass the prohibition.

### Integration requirements

- Resolve the current supported Composio Python package/API during implementation and pin it using Genesis's dependency convention; do not copy a stale SDK call from this spec.
- Introduce one Genesis-owned adapter boundary. Agent code must not call the SDK directly.
- A tool/action request must pass the existing `runtime/tool_policy.py` decision before the adapter resolves or invokes it.
- Default deny: an unlisted provider or action is rejected.
- Bind sessions/connections to the existing Genesis actor/user context. Never use one unrestricted global session across users.
- Use the existing Genesis audit/logging convention while redacting access tokens, authorization headers, provider payload secrets, and signed links.
- Composio failure must fail the requested Genesis tool action clearly; it must not authorize a fallback financial connector or mutate FinanceOS.
- Do not modify FinanceOS, Trigger.dev scheduling, or Genesis hosting as part of this chunk.

### Acceptance

- Unit tests prove every prohibited financial provider is denied before an SDK call.
- Unit tests prove unknown providers/actions default deny.
- Unit tests prove an approved action still fails when `runtime/tool_policy.py` denies it.
- Unit tests prove an approved, policy-authorized non-financial action reaches the adapter exactly once.
- Logs and raised errors contain no test token or authorization header.
- A repository search shows application code reaches Composio through the adapter only.
- The complete Genesis bounded test command passes.

## 8. Deferred Gate — Azure Document Intelligence

Document Intelligence is not an active build phase and must not receive a Ralph chunk from this version.

### Gate method

1. Establish a versioned, representative invoice corpus with expected fields and no secrets committed to the repository.
2. Run the current InvoiceProof extraction path without Document Intelligence.
3. Measure required-field extraction accuracy, invalid-field rate, and human-correction rate.
4. Record reproducible inputs, commands, outputs, sample size, and acceptance threshold in evidence.
5. Decide:
   - If the agreed threshold is met, record **OMIT** and do not build Document Intelligence.
   - If it is not met and Document Intelligence demonstrates a material, reproducible improvement on the same corpus, write a new delta spec for preprocessing only.

### Immutable boundary if later authorized

Document Intelligence output may feed InvoiceProof only. It may not call Xero, approve a transaction, write financial state, or replace InvoiceProof, AuditProof, or VerifyAPI.

## 9. Interfaces and Ownership

| Interface | Producer | Consumer | Authority |
|---|---|---|---|
| Genesis artifact reference | Genesis artifact-store adapter | Genesis API/job callers | Genesis execution metadata; not financial authority |
| Blob object bytes | Genesis Azure Blob adapter | Genesis artifact retrieval | Storage only |
| Composio action request | Genesis agent runtime after policy decision | Genesis Composio adapter | Tool execution request only |
| FinanceOS document record | FinanceOS Registry | Cato/Genesis/Airtable consumers | FinanceOS Postgres metadata is authoritative |
| Airtable workbench row | Existing FinanceOS sync/API workflow | Human reviewer | View/trigger only; not approval authority |
| Phoenix span | Instrumented application | Phoenix | Observability only |

No chunk may implement both sides of a cross-repository interface by directly editing both repositories. Contract tests and fixtures must be owned by the repository whose behavior they verify.

## 10. Security and Compliance

- Secrets come from environment/approved secret storage and are never committed.
- Tests use obvious fake values and must assert redaction.
- Signed Blob URLs, if ever returned at an external boundary, are short-lived and are not stored as durable identifiers.
- Composio scopes are least privilege per approved action.
- No connector may turn a Genesis proposal into a FinanceOS approval or write.
- Existing FinanceOS capability and human-approval gates remain unchanged.
- `XERO_PRODUCTION_WRITE_ENABLED` behavior is untouched.
- Dependency additions receive the repository's standard vulnerability/license checks.

## 11. Ralph Chunk Plan

Ralph prep must produce repository-specific chunks in this order. It may use separate workspaces where repository tooling requires it.

1. **FINANCEOS_BOUNDARIES** — FinanceOS only; automated architecture-boundary regression tests.
2. **GENESIS_BLOB** — Genesis only; Azure Blob artifact backend and compatibility tests.
3. **GENESIS_COMPOSIO** — Genesis only; adapter, policy integration, provider/action allowlist, and unit tests.
4. **GENESIS_VERIFY** — Genesis only; combined regression, secret-redaction, failure-mode, and exact-HEAD verification.

Dependencies:

- `FINANCEOS_BOUNDARIES` is independent.
- `GENESIS_BLOB` and `GENESIS_COMPOSIO` are independently buildable.
- `GENESIS_VERIFY` depends on both Genesis chunks.
- Operational live verification follows code verification and never blocks the creation of honest code evidence; it does block any claim of live readiness.

## 12. Definition of Done

This specification's CODE work is complete only when:

- [ ] FinanceOS boundary tests pass in its normal verification suite without rebuilding completed integrations.
- [ ] Genesis uses Azure Blob rather than boto3/S3 for configured remote artifact storage.
- [ ] Genesis artifact-store behavior remains compatible and has failure/redaction coverage.
- [ ] Genesis Composio access is adapter-only, default-deny, and subordinate to `runtime/tool_policy.py`.
- [ ] Xero, Stripe, Gusto, Expensify, and equivalent financial providers are structurally denied through Composio.
- [ ] Neither repository contains secrets or newly logged signed credentials.
- [ ] FinanceOS remains pg-boss-only; Trigger.dev remains Genesis-only.
- [ ] Metabase, completed FinanceOS Blob/Registry/Airtable work, and dedicated Document Registry work were not duplicated.
- [ ] Document Intelligence remains unbuilt unless a later evidence-backed delta spec is approved.
- [ ] Exact changed HEADs and bounded test outputs are recorded as evidence; prose alone does not establish completion.

Operational readiness is a separate claim. It requires real Azure Blob and approved Composio smoke tests plus the existing Airtable live-access gate, with sanitized evidence.

## 13. Evidence References

- FinanceOS Blob implementation: `src/modules/dataroom/blob.ts`
- FinanceOS document ingestion/hash verification: `src/modules/dataroom/index.ts`
- FinanceOS Registry API: `src/api/routes/documents.ts`
- FinanceOS Airtable implementation: `src/connectors/airtable/client.ts`, `src/modules/dataroom/airtable-document-sync.ts`, `src/worker/jobs/registry.ts`
- FinanceOS implementation commits: `34e1424`, `6b361cd`, `48f3018`
- Dedicated Registry work/state: `C:\Users\Work\Desktop\vault\projects\E4L-FinanceOS\assistant-architecture\ralph\document-registry-ralph`
- Dedicated Metabase work/state: `specs/Ralph Chunks/metabase-reporting-layer`
- Genesis current artifact backend: `artifact_store.py`, `requirements.txt`
- Genesis Trigger.dev path: `trigger_dispatch.py`
- Genesis policy boundary: `runtime/tool_policy.py`
- Genesis removal of unused ACA/Phoenix migration workspace: commit `3a84603`
