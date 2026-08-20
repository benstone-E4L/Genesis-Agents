# Required Ralph changes — delta only. Do not rerun completed work.
# Date: 2026-08-19

## Do not redo (working or already specified elsewhere)

| Workspace | Classification | Evidence |
|---|---|---|
| `Genesis Agents/ralph/e4l-retrieval-route-ralph` CHUNK_1..6 | already implemented | `.ralph/progress.md` CHUNK COMPLETE through CHUNK_6; 71 passed retrieval tests 2026-08-08 |
| `Genesis Agents/docs/FINANCE-TOOL-CONTRACTS.md` | already implemented (contract) | Authoritative; stubs still stubs — do not “fix” by calling them from Cato |
| `Cato/cato/tools/genesis.py` MONEY_DOMAIN_AGENTS | already implemented (safety) | Keep deny on stub money slugs |
| FinanceOS `src/xero/operation-registry.yaml` + `xero-write.ts` | partial / disarmed | Writer exists, gates off. Do not rebuild. |
| FinanceOS Python Xero MCP `mcp-servers/xero/server.py` | already implemented | Read+draft write live this session |
| PCP `generated/E4L_*_REGISTRY.yaml` | already implemented, extend | Pointer added conceptually; keep as architecture registry |

## FinanceOS Ralph Chunks (`E4L-FinanceOS/app/specs/Ralph Chunks`)

Progress logs are empty except retrieval-route (Genesis). Treat **code presence** as separate from Ralph loop completion.

| Workspace | Ralph loop | For Genesis accounting | Delta |
|---|---|---|---|
| accounting-framework-and-subledgers (8 chunks) | missing (no progress entries) | CoA mapping, IC, RTO, inventory, tax bridge, combining TB | **Do not re-spec in Genesis.** Optional: add one sentence in SPEC that Genesis profiles *consume* combining_run / IC register when those tables exist. File: `SPEC-accounting-framework-and-subledgers.md` — wait until a real loop starts; no Genesis rewrite. |
| entity-expansion-and-external-sources | missing | Stripe/Gusto/Expensify read adapters | Genesis stripe profile should call FinanceOS adapter **when it exists**, not a new Stripe client in Genesis. No spec edit required until CHUNK_4_ADAPTERS lands. |
| financeos-autonomous-operating-layer | missing | Xero snapshots/sync, Cato control room | **Incorrect for this architecture if duplicated in Genesis.** Genesis must not grow a second Xero sync. |
| metabase-reporting-layer | missing | dashboards | Out of Genesis scope. |
| azure-phoenix-infrastructure-residual | missing | observability | Out of Genesis scope. |

## Genesis Ralph

| File | Classification | Delta |
|---|---|---|
| `ralph/integration-architecture-residual-ralph/specs/01_CHUNK_1_FINANCEOS_BOUNDARIES.md` | spec exists; progress empty | Keep. Genesis accounting must not import Trigger.dev/Composio into FinanceOS. No change. |
| `ralph/integration-architecture-residual-ralph/specs/02_CHUNK_2_GENESIS_BLOB.md` | spec exists | Irrelevant to accounting profiles. No change. |
| `ralph/integration-architecture-residual-ralph/specs/03_CHUNK_3_GENESIS_COMPOSIO.md` | spec exists | Composio remains Genesis-only and barred from Xero. No change. |
| `ralph/integration-architecture-residual-ralph/SPEC-integration-architecture-final-stack.md` | authoritative boundary | **Minimum delta after topology:** add a pointer that E4L accounting specialization lives in `Genesis Agents/accounting/` and uses host Xero MCP, not a new FinanceOS write path. Do this in a follow-on edit by parent — one short “Accounting specialization” subsection. Do not reopen the four chunks. |
| `ralph/e4l-retrieval-route-ralph/specs/*` | implemented | **No change.** Retrieval is not the accounting CoA map. Optional later: index `accounting/*.yaml` into the retrieval store — new chunk, not a rewrite of CHUNK_4. |

## New Ralph work (SUPERSEDED 2026-08-19)

The proposed `e4l-accounting-specialization-ralph` one-hat chunks (one slug + YAML profiles) are **REJECTED**. Do not create that workspace.

Locked implementation: 14 guarded `skill_bundles/genesis-e4l-*.json` on this gateway. Cato dispatches those slugs. Entity YAML stays context packs.

The one-hat chunk list below is obsolete. Do not execute it.

~~CHUNK_1_BUNDLE genesis-e4l-accounting.json~~ REJECTED
~~CHUNK_2_PROFILE_LOADER~~ REJECTED (entity injection remains; hats do not)
~~CHUNK_3_CATO_ROUTE one slug~~ SUPERSEDED by 14 GENESIS_AGENTS entries
~~CHUNK_4_SCENARIO_TESTS~~ implemented as specialist-slug tests
~~CHUNK_5_VERIFY~~ still required: compileall + no Xero writes in tests
