# Guardrails — Known Risks and Scope Exclusions

ralph: before taking any action, scan this file. If your action matches a SIGN, stop and report.

## Pre-Loaded Risks (from master spec §13 Failure/Recovery + architecture-cartographer audit)

### SIGN: Vault index stale/fails
Master spec §13: "Watchdog alert + staleness banner on Ask-E4L answers; retrieval still cites
what it has; refusal path prevents confident staleness." This route IS one of the two Retrieval
Contract implementations (the cloud one) — it must set `stale: true` past
`RETRIEVAL_STALENESS_THRESHOLD_HOURS` and must never hide staleness by silently answering as if
current.
Mitigation: staleness flag is a hard acceptance criterion in CHUNK_4_RETRIEVAL and a tested
contract row in CHUNK_5_TESTS. Do not ship the route without it.

### RESOLVED: Assistant-tier PG server is now live (2026-08-08)
Provisioned via `/microsoft-azure-master`: `psql-e4l-assistant-prod.postgres.database.azure.com`
(PostgreSQL 16, Burstable Standard_B2ms, resource group `rg-e4l-assistant-prod`, subscription
"NES Production"), database `e4l_knowledge`, with the `vector` (pgvector 0.8.2) and `pg_diskann`
(0.6.5) extensions already `CREATE EXTENSION`-enabled in that database. Admin credential lives
ONLY in Key Vault `kv-e4l-asst-prod`, secret `psql-e4l-assistant-prod-admin-password` — never in
this repo, never in any chunk spec, never printed by any command. Firewall currently allows Azure
services broadly (SSL-required) plus one admin IP for setup; it does NOT yet have a rule for
wherever this Genesis gateway is actually deployed (Render today; ACA per the unexecuted Azure
migration) — **CHUNK_4_RETRIEVAL must still treat a connection failure as a first-class, tested
degraded path** (the mitigation below stays fully in force), because the firewall will need a
rule for Genesis's real egress IP/subnet before this route works in production, and that's a
deploy-time step, not something this chunk can complete for itself.
Mitigation (unchanged): `retrieval_store.py` must handle unset/unreachable
`ASSISTANT_PG_DATABASE_URL` as a first-class, tested path (degraded-refusal, HTTP 200, gateway
still boots) — not an afterthought. CHUNK_4_RETRIEVAL and CHUNK_5_TESTS both hardcode this as an
acceptance criterion. `ASSISTANT_PG_DATABASE_URL`'s value is read from Key Vault at deploy time,
never hardcoded — see AGENTS.md's env block.

### SIGN: Genesis's 57 catalogued agent slugs are not all guarded
Architecture-cartographer audit: only 21 of 57 `AGENT_PERSONAS` slugs resolve to a real
tool-loop/budget-cap/escrow-checked bundle; 36 are unguarded single-turn personas. Financial
work routed to an unguarded slug bypasses `escrow_guard.py` entirely (P0 risk in the audit).
Mitigation: **not this workstream's job to fix routing** — the retrieval route added here does
not dispatch to any Genesis agent slug at all (it is a standalone read-only query endpoint), so
this risk cannot be triggered by code built in this workstream. Documented here only so no
future chunk in this workspace is tempted to add an "ask an agent to summarize the retrieved
chunks" shortcut that routes through an unguarded slug without checking the allowlist first.
CHUNK_2_REGISTRY changes which slugs *exist* (24 bundles instead of 21 reachable) but does not
change which of them are finance-adjacent-safe — do not assume "resolvable" means "safe for
finance work."
**RESOLVED elsewhere (2026-08-08, informational only):** the estate now has a single canonical
answer to "which Genesis slugs are safe to call" — `cato/tools/genesis.py::GENESIS_AGENTS` in the
Cato repo (20 hand-curated, verified bundle-backed slugs, with its own independent, config-proof
`MONEY_DOMAIN_AGENTS` denylist), which `e4l-work-os`'s guardrails also now reference as the shared
source of truth instead of deriving a second list from this repo's persona catalogue. This repo's
own `/agents` catalogue (57 slugs) and bundle registry (24 files) remain the underlying ground
truth Cato's list was curated from — no code change needed here, just noting the cross-repo
resolution so a future reader of this file isn't left thinking it's still unresolved.

### SIGN: Contradiction surfacing is easy to accidentally implement wrong
The contract requires two disagreeing `status: active` chunks to both be returned, unmerged. The
most natural-looking SQL (`ORDER BY score DESC LIMIT top_k`) already does this correctly by
default *if* you never deduplicate by topic/entity — the risk is a well-intentioned "let's not
return near-duplicate chunks" optimization that silently violates the contract.
Mitigation: CHUNK_4_RETRIEVAL's acceptance criteria explicitly forbid deduplication/merging;
CHUNK_5_TESTS has a dedicated fixture test for this. If a future change adds dedup logic, it
must exempt same-entity-different-content chunks by design, not by exception.

## Scope Exclusions — Do Not Build

- DO NOT BUILD: the Azure/Phoenix migration for Genesis (docs-only per the architecture-cartographer
  audit; not part of the master spec's Phases A-G). A separate ralph workspace for this already
  exists at the repo root (`specs/01_CHUNK_1_DOCKER.md` through `05_CHUNK_5_CONFIG.md`,
  `SPEC-genesis-azure-phoenix-migration.md`) — do not touch it, do not merge work into it.
- DO NOT BUILD: AP2 signature verification. Confirmed not implemented anywhere in this repo
  (`trusted_ap2_clients.README.md`); the repo's own docs and the master spec both name it
  explicit future work, out of Phases A-G. SwarmSync's `a2a-mandate-signer.service.ts` is the
  eventual wire-format reference when someone does build this — not this workstream.
- DO NOT BUILD: a fix for the Trigger.dev fire-and-forget stub (`trigger_dispatch.py`, 44 lines,
  no matching `genesis-job-process` task definition found anywhere in this repo per the
  cartographer audit). Not called for by the master spec's build sequence for this workstream.
- RESOLVED (2026-08-08): the `eval/` vs `evals/` question is decided — `eval/` (singular) is
  canonical. Evidence: `eval/` has a maintained README explaining its design (LangSmith
  `@traceable` integration, explicitly additive/non-invasive to the Genesis service), a full
  13-file test suite, and matches this repo's actual current layout. `evals/` (plural) is stale —
  `evals/reports/latest.json` was generated `2026-06-13T13:16:06Z` (nearly two months old) and its
  own fixture paths are hardcoded to `C:\Users\Ben\Desktop\Github\Genesis-Agents\...`, a different
  machine and a different casing/hyphenation of this repo's path — it is a carried-over artifact
  from an old environment, not a maintained parallel system. `CHUNK_6_EVALCLEANUP` deletes
  `evals/` entirely; `eval/` is untouched and remains the one eval harness for this repo.
- DO NOT BUILD: any code path where the pgvector/pg_diskann index (or Cato's local index) is
  treated as authoritative over the vault itself. Both indexes are disposable/derived per master
  spec §9's closing line: "The vault remains the only source of truth; both indexes are
  disposable derived state, rebuildable from the vault at any time."

## Standing Guardrails (always active)

- DO NOT add npm/pip/gem dependencies without updating AGENTS.md.
- DO NOT skip the validation gate, even for trivial changes.
- DO NOT commit with --no-verify.
- DO NOT generate code for a future chunk's domain.
- DO NOT modify files outside the current task's scope.
- DO NOT hard-code secrets, API keys, or credentials.
- DO NOT edit `main.py` more than necessary — it is already 3,550+ lines; new logic belongs in
  new modules (`retrieval_route.py`, `retrieval_store.py`) mounted via `include_router`, per
  CHUNK_4_RETRIEVAL.
- DO NOT touch Cato, e4l-work-os, the E4L Coordination Ledger, or FinanceOS repos/code — other
  workstreams own those in parallel. This workspace's scope is the Genesis Agents repo only.

## Accumulation Instructions

When ralph encounters a new failure pattern, append below:

### Learned: {SHORT_TITLE}
{what went wrong and how to avoid it}
