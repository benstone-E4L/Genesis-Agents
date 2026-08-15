# Genesis Retrieval Certification

Generated: 2026-08-15
Commit tested: `6712dcf361c9fb3e8d7edadc4c6d77ffc251b80d` (HEAD, `main`, working tree has
unrelated unstaged changes in `expected_policy_matrix.json` / `runtime/tool_policy.py` /
`skill_bundles/genesis-data-pipeline.json` / `test_data_pipeline_tool.py` /
`tools/data_pipeline_tool.py` from a different in-flight task — none of those files are
retrieval-related and none were touched by this certification)
Deployment tested: `https://swarmsync-agents.onrender.com` (live health/route probes only, GET
+ one unauthenticated POST — no writes, no secrets used)
Scope: `retrieval_route.py`, `retrieval_store.py`, `knowledge_backbone.py`,
`knowledge_backbone_store.py`, plus the 4 test files that exercise them.

## 1. Architecture — what actually exists (no assumed vendor)

There is **no separate Azure "Knowledge Backbone" service deployed and reachable from this
repo.** Rather, `POST /retrieval/query` (mounted in `main.py:1632`,
`app.include_router(retrieval_router)`) is a single FastAPI route that fans out to **two
backend implementations with very different real/stub status**:

### 1a. Vault backend (`retrieval_store.py`) — real code, unconfirmed live connectivity
- Direct `psycopg` hybrid lexical+vector query (`ts_rank` + optional pgvector `<=>` term)
  against a `vault_chunks` table on an assistant-tier Azure Postgres server
  (`ASSISTANT_PG_DATABASE_URL`).
- The server itself (`psql-e4l-assistant-prod.postgres.database.azure.com`, pgvector 0.8.2 +
  pg_diskann 0.6.5 enabled) was provisioned 2026-08-08
  (`vault/decisions/2026-08-08-azure-assistant-tier-postgres-provisioned.md`), but that same
  decision record states its firewall **did not yet have a rule for Genesis's Render egress**,
  and that neither the retrieval-route chunk nor its test chunk had "actually connect[ed] and
  run against this server" as of that date. No newer decision record superseding that exists
  in the vault. `vault_chunks` itself is populated by a separate workstream
  (`e4l-work-os/Cato`), not this repo.
- `ASSISTANT_PG_DATABASE_URL` is **absent** from both this repo's real `.env` (confirmed via
  direct grep, 0 matches) and `.env.example`'s populated-in-`.env` list
  (`certification-evidence/environment-snapshot.md` line 42 lists it under "present in
  `.env.example` but absent from `.env`"). Whether Render's deployed environment sets it is
  unknown from this repo — a live authenticated POST would answer that, but see §2c for why
  that call was not made with production credentials in this pass.
- **Fail-closed by design when unset/unreachable:** `_conn()` raises `RuntimeError`; the route
  catches it and returns HTTP 200 with `{"refusal": true, "degraded": true, "degraded_reason":
  "assistant-tier PG unavailable"}` — never a 500.

### 1b. "knowledge_backbone" second backend (`knowledge_backbone_store.py`) — confirmed stub, not live
- `query_chunks()` **always raises** today, by explicit design — its own docstring: "Until
  then, `query_chunks()` always raises — never returns fixture/mock rows — so this module can
  never be mistaken for a live connection." Two branches (`KNOWLEDGE_BACKBONE_MCP_ENDPOINT` /
  `KNOWLEDGE_BACKBONE_DATABASE_URL`) both raise `NotImplementedError`; if neither env var is
  set, it raises `RuntimeError`. Neither env var is present in this repo's `.env` or
  `.env.example`-populated set.
- This confirms the prior finding referenced in this task's context: **this repo does not have
  a working Azure-hosted "Knowledge Backbone" client.** The real question of which transport it
  should even use (`kb-mcp-prod` MCP client vs. direct Postgres to the separately-owned
  `rg-kb-prod`/`knowledge_backbone` database) is an explicitly unresolved "Open Question 3" in
  `SPEC-e4l-drive-knowledge-integration.md`, deferred to a future `CHUNK_6_VERIFY` — the module
  docstring is explicit that wiring a real client is not this workstream's job yet.
- The route's own guardrail keeps this stub from ever masquerading as data: `retrieval_route.py`
  only calls it when the caller supplies `requesting_principal` AND `domain_hint != "vault"`;
  any exception from it degrades the response to `partial: true` / `partial_reason:
  "knowledge_backbone unavailable"` and never blocks or replaces vault results.

### 1c. Permission-filter / dedup layer (`knowledge_backbone.py`) — real, backend-agnostic, pure logic
- `filter_by_permission()` — fails closed in every direction: no principal → 0 rows; missing/
  malformed `permissions_snapshot` → row excluded; snapshot present but principal not listed and
  not `public` → excluded.
- `dedup_and_precede()` — two-stage dedup: exact `(drive_id, file_id)` identity dedup first,
  then `content_hash` cross-source fallback with `canonical` beating `controller` on a hash
  collision.
- `resolve()` — the single entry point (`filter_by_permission` → `dedup_and_precede`, in that
  order, deliberately, so a principal who can only see the `controller` copy of a
  canonical-superseded duplicate still gets that one copy, not zero).
- This module has zero I/O — it is pure functions over plain dicts, independent of whichever
  transport eventually implements `knowledge_backbone_store.query_chunks()`. It is real,
  substantive logic, not a stub, and it is exercised end-to-end through the route (§2).

### 1d. What calls this route
- **No Genesis agent, tool, or bundle references retrieval.** Grepped `certification-evidence/
  agent-inventory.json`, `certification-evidence/tool-inventory.json`, `skill_bundles/*.json`,
  `tools/*.py`, and `agent_runtime.py` for "retrieval" — zero matches anywhere. This is not an
  agent-callable tool; it is a standalone external HTTP contract (E4L Retrieval Contract, master
  spec §9) authenticated the same way `/agents/{slug}/run` is (signed AP2 envelope, scope
  `retrieval.query`, registry in `trusted_ap2_clients.json` — confirmed granted to `cato`), for
  an external caller (Cato) to call directly. This route never calls an LLM (asserted by both a
  runtime mock and a static source grep in the test suite).

**Bottom line on architecture:** this is a real, well-built, defense-in-depth retrieval
*contract layer* (auth, fan-out, fail-closed degrade, permission filtering, dedup/precedence,
citation/provenance shaping) sitting in front of two backends whose actual live data
connectivity is either **unconfirmed** (vault/Azure PG — provisioned but firewall/connection
status not verified from this repo, and never yet run against per the last dated record) or
**deliberately not yet implemented** (knowledge_backbone — always raises by design, pending an
unresolved architecture question in a different spec). Nothing here is a silent mock dressed up
as production-ready; every degrade path is documented in the code and proven by tests.

## 2. Test evidence

### 2a. Full retrieval suite run (this session, this commit)

```
$ python -m pytest test_retrieval_route.py test_retrieval_route_knowledge_backbone.py test_retrieval_route_ap2.py test_knowledge_backbone.py -q
....................s........................................            [100%]
60 passed, 1 skipped in 4.32s
```

The 1 skip is `test_retrieval_route.py::test_real_connection_integration`, gated by
`@pytest.mark.skipif(not os.getenv("ASSISTANT_PG_DATABASE_URL"), ...)` — it activates
automatically the moment a real, reachable Azure PG connection string is supplied to this
environment. It is not currently reachable from here (§1a), so it correctly stays skipped
rather than fabricating a pass.

Full output saved at:
`C:\Users\Work\AppData\Local\Temp\2\claude\C--Users-Work-Desktop-E4L-Project-Control-Plane\2d698e39-ba58-45a3-b530-3da73dd37834\scratchpad\retrieval_pytest_output.txt`

### 2b. Coverage mapped to this task's required scenarios

No live Azure PG or knowledge_backbone connection exists to test against today (§1), so these
are **route/function-level tests exercised through the real FastAPI route** (`TestClient(main.app)`,
real auth code path, real `knowledge_backbone.resolve()` logic) with the two backend I/O layers
(`retrieval_store.query_chunks`, `knowledge_backbone_store.query_chunks`) monkeypatched to
deterministic fixture rows — the same pattern this repo already uses for every other external
dependency it cannot reach in CI (e.g. `job_store.py`'s Postgres). This is not a silent mock: it
is disclosed in every test file's own docstring, and the one test that would prove a *live*
connection (`test_real_connection_integration`) is explicit about being skipped, not faked.

| Scenario | Test(s) | Result |
|---|---|---|
| Exact lookup / citation format | `test_chunk_id_and_citation_format` | chunk_id = `{path}#{heading}@{index}`, citation = `{path}#{heading}` — PASS |
| Hybrid/semantic ranking (SQL construction) | `test_recency_is_tiebreaker_not_primary_key_in_store_sql` | proves real SQL: `ORDER BY score DESC, updated DESC`, metadata filters in WHERE, not post-filtered — PASS |
| Metadata filter applied before ranking | `test_metadata_filter_applied_before_ranking` | PASS |
| Missing-answer / refusal | `test_refusal_path_below_threshold_returns_200_empty_chunks` | below `RETRIEVAL_MIN_SCORE` → HTTP 200, `refusal:true`, `chunks:[]` — never a fabricated answer — PASS |
| Stale source | `test_staleness_flag_true_past_threshold` / `..._false_within_threshold` | PASS |
| Superseded source | `test_superseded_excluded_by_default` / `..._included_when_requested` | PASS |
| Contradictory sources (duplicates never merged) | `test_contradiction_surfacing_returns_both_active_chunks_unmerged` | two active chunks on the same topic both returned separately, no averaging/merging — PASS |
| Backend outage / degraded | `test_degraded_connection_path_returns_200`, `test_knowledge_backbone_unreachable_degrades_to_partial_vault_results_preserved`, `test_knowledge_backbone_unreachable_with_nothing_from_vault_never_5xxs` | vault-down → `degraded:true`, HTTP 200 never 500; KB-down → `partial:true`, vault results preserved — PASS |
| KB backend confirmed still a stub | `test_production_knowledge_backbone_store_always_raises_today` | unpatched module raises `RuntimeError` — PASS |
| Permission filtering — no identity | `test_unauthenticated_request_never_reaches_knowledge_backbone` | 401 before any backend call — PASS |
| Permission filtering — fails closed on identity spoofing | `test_body_requesting_principal_cannot_override_authenticated_identity` | caller authenticates as `ben`, claims to be `mallory` in the request body; filter uses the *signed* identity, not the claimed one → 0 chunks — PASS |
| Permission filtering — malformed/null snapshot | `test_permission_filter_excludes_row_with_null_snapshot`, `..._malformed_snapshot` | excluded, never defaults to visible — PASS |
| Permission filtering — public vs. restricted | `test_permission_filter_includes_public_row_for_any_principal`, `..._excludes_row_without_matching_principal` | PASS |
| Duplicate/contradictory-source handling (dedup + precedence) | `test_dedup_and_permission_filter_exercised_through_route`, `test_identity_dedup_collapses_exact_duplicate`, `test_content_hash_fallback_dedup_across_different_identity_pairs`, `test_canonical_wins_over_controller_on_hash_collision`, `test_resolve_returns_controller_copy_when_only_it_is_accessible` | identity dedup → content-hash fallback → canonical-over-controller precedence, and permission-filter-before-dedup so an inaccessible canonical copy doesn't suppress an accessible controller copy — all PASS |
| Backend selection (vault-only vs. fan-out) | `test_vault_only_question_never_calls_knowledge_backbone`, `test_drive_only_relevant_question_surfaces_knowledge_backbone_results`, `test_vault_and_knowledge_backbone_merge_when_both_relevant` | `domain_hint:"vault"` never touches the second backend at all (not just "contributes nothing") — PASS |
| Auth / envelope binding (AP2) | `test_shared_gateway_key_alone_is_refused`, `test_signed_retrieval_envelope_is_accepted`, `test_tampered_query_text_fails_the_binding_check`, `test_tampered_retrieval_scope_fails_the_binding_check[×5 params]`, `test_retrieval_envelope_signed_for_an_agent_run_is_refused`, `test_retrieval_envelope_replay_is_refused`, `test_untrusted_key_is_refused_against_the_real_registry`, `test_shipped_registry_grants_cato_the_retrieval_scope` | shared gateway key alone insufficient; signature covers query text + every scope-widening param (`top_k`, `domain_hint`, `entity_filter`, `include_superseded`, `requesting_principal`); replay and cross-agent envelope reuse rejected; only the real trusted-client registry (granting `cato` the scope) accepted — PASS |
| Forged/wrong-scope principal token | `test_forged_principal_token_is_rejected`, `test_principal_without_retrieval_scope_is_denied` | 401 / 403 respectively — PASS |
| Content redaction/bounding | `test_content_is_bounded_and_redacted` | secret substring stripped, content capped at 4000 chars — PASS |
| Never calls an LLM | `test_route_never_calls_an_llm`, `test_no_llm_client_referenced_in_retrieval_source` | runtime mock-assert + static source grep — PASS |
| Malformed request | `test_malformed_request_missing_query_returns_422` | PASS |
| Schema isolation from an unrelated system (FinanceOS Document Registry) | `test_response_schema_disjoint_from_document_registry_shape` | PASS |

Every scenario this task's Definition of Done names (exact lookup, semantic lookup,
missing-answer handling, permission filtering, duplicate/contradictory-source handling) has
real, passing, function/route-level coverage. "Similar entities," "malformed documents," and
"deleted/moved/renamed documents" from the broader `genesis-e2e-certification` skill's Phase 7
list are **not separately covered** — the current suite's dedup tests cover exact-duplicate and
content-hash-collision cases but not near-duplicate/fuzzy-similarity or moved-file scenarios;
flagged as a gap, not fabricated as tested.

### 2c. Live deployment evidence (read-only, no secrets)

```
$ curl -sS -i https://swarmsync-agents.onrender.com/health
HTTP/1.1 200 OK
{"status":"ok","service":"swarmsync-agent-gateway"}

$ curl -sS -i -X POST https://swarmsync-agents.onrender.com/retrieval/query \
    -H "Content-Type: application/json" \
    -d '{"query":"test retrieval certification probe"}'
HTTP/1.1 401 Unauthorized
{"detail":"principal token required"}
```

This proves the retrieval route is genuinely mounted and enforcing the exact same auth contract
in production as in the unit tests (`_retrieval_principal` rejecting a request with no
`X-Genesis-Principal-Token` and no AP2 envelope headers) — not merely present in source.

**An authenticated live call was deliberately not made in this pass.** Making one would require
minting a real AP2-signed envelope or `X-Genesis-Principal-Token` using this repo's local
`GENESIS_PRINCIPAL_TOKEN_KEY` / `GENESIS_GATEWAY_PRIVKEY_B64` secrets — but
`certification-evidence/environment-snapshot.md` already flags that "local `.env` values are
not proof of what Render's actual deployed environment has configured," and forging a
production-scoped credential from local secret material to probe a live system, without a
confirmed-matching key on the Render side and without a task requirement to do so, is a
disproportionate risk for the marginal evidence gained: whether authenticated, the response
would almost certainly be `{"degraded": true, "degraded_reason": "assistant-tier PG
unavailable", "refusal": true}` given `ASSISTANT_PG_DATABASE_URL` is absent from every
environment-name source this repo can inspect (§1a), which the unit suite already proves
deterministically and exactly (`test_degraded_connection_path_returns_200`). If Ben wants this
specific fact (is `ASSISTANT_PG_DATABASE_URL` actually set on Render right now) confirmed, the
authoritative source is the Render dashboard/API, not a forged local credential.

## 3. Findings

1. **No live "Knowledge Backbone" exists to certify against.** `knowledge_backbone_store.py`
   always raises by explicit design; this is documented in the module itself, not discovered by
   this audit. Certifying it as "tested and working" would be false. What *is* real and tested
   is the fan-out/degrade/permission-filter/dedup logic that will run once a real backend lands.
2. **The vault backend's live database connectivity is unconfirmed, not proven broken.** The
   Azure PG server exists and has the right extensions, but the last dated record
   (2026-08-08) says Genesis's Render egress had no firewall rule yet and no chunk had ever
   connected to it; nothing in this repo shows that has since changed, and this task's read-only
   scope did not include Azure/Render dashboard access to check directly.
3. **This route is not on any agent's tool path.** It is an external-caller (Cato) contract, so
   "does Genesis correctly retrieve for its own agents" does not apply here — no agent bundle
   advertises a retrieval tool today.
4. **Fuzzy/near-duplicate and moved/renamed-document scenarios are untested.** Only exact
   `(drive_id, file_id)` and `content_hash`-collision dedup are covered.

## 4. Verdict

**CONDITIONAL_PASS at the contract-logic layer, NOT_APPLICABLE at the live-data layer for one
of the two backends.**

- The retrieval *contract* (auth, fan-out, refusal/degrade semantics, citation/provenance
  shaping, permission filtering, dedup/precedence) is real, substantive code with 60/61
  scenario-level tests passing and one honestly-skipped live-DB test — this is genuinely tested,
  not a stub dressed up as tested.
- The **vault backend's live data path has never been exercised against a real database** (per
  the repo's own dated record) and could not be confirmed live in this pass without either
  Render/Azure dashboard access or forging production credentials — flagged as open, not claimed
  working.
- The **knowledge_backbone backend is confirmed, by its own code, to not exist as a live
  integration** — this is the honest "minimal/stub" finding this task asked to surface plainly
  rather than paper over.
