# IMPLEMENTATION_PLAN.md

Project: e4l-retrieval-route (Genesis Agents repo, Phase E slice)
Code lands at the repo root (one directory above this ralph workspace), never inside
`ralph/e4l-retrieval-route-ralph/`.

## Chunk Order

1. CHUNK_1_CLEANUP — remove the dead `agent_loader.py` 45-slug registry and its `main.py` import guard/call site.
2. CHUNK_2_REGISTRY — reconcile the 3 orphaned skill bundles (genesis-domain, genesis-maintenance, genesis-pricing) into `AGENT_PERSONAS` / `BUNDLE_SLUG_ALIASES`.
3. CHUNK_3_DOCS — fix `CLAUDE.md`'s stale "20 agents" claim; add a reproducible Agent Count table.
4. CHUNK_4_RETRIEVAL — the E4L Retrieval Contract route (`retrieval_route.py` + `retrieval_store.py`), mounted into `main:app`.
5. CHUNK_5_TESTS — contract-compliance test suite (`test_retrieval_route.py`) proving every Retrieval Contract row without a live Postgres connection.
6. CHUNK_6_EVALCLEANUP — delete the stale `evals/` directory (`eval/` is canonical, untouched).

Dependency chain: 1 -> 2 -> 3; 1 -> 4 -> 5. CHUNK_6 is independent, can run anytime.

## Chunk 1: CHUNK_1_CLEANUP
### Tasks (in order)
1. Grep-confirm `agent_loader`/`load_agent` is referenced only in `main.py` (import block + one call site) and `test_gateway_error_mapping.py` (a monkeypatch on the now-removed symbol) — no other caller.
2. Delete `agent_loader.py` from the repo.
3. Remove `main.py`'s `try: from agent_loader import load_agent ... except Exception: load_agent = None ...` block (lines ~64-70).
4. Remove the `if load_agent is not None and not skip_loaded_agent: ...` call site (lines ~2007-2035) in `run_agent()`, leaving the persona-router fallback (`call_llm_router(...)`) as the only path.
5. Check `skip_loaded_agent`: it is set at line ~1732 (`skip_loaded_agent = False`) and ~1782 (`skip_loaded_agent = True`) purely to gate the block being removed — remove the assignments too since nothing else reads the variable after step 4 (re-grep to confirm before deleting).
6. Fix `test_gateway_error_mapping.py::test_hr_aliases_use_same_canonical_bundle_in_live_test` — it does `monkeypatch.setattr(main, "load_agent", fail_load_agent)`, which will raise `AttributeError` once the symbol no longer exists on the module. Remove the now-meaningless `fail_load_agent` def and that one `monkeypatch.setattr` line only; leave the rest of the test (the actual HR-alias assertions) untouched — the guarantee it checked ("legacy loader never called") is now structurally true because the code path is gone.
### Validation
- Command: `python -m compileall -q main.py retrieval_route.py retrieval_store.py bundle_loader.py && pytest test_retrieval_route.py tests/test_prohibited_tools.py tests/test_escrow_containment.py tests/test_gateway_key_guard.py -q` (retrieval files don't exist yet this chunk — run `python -m compileall -q main.py bundle_loader.py` plus the full existing repo-root test files that reference `run_agent`/`load_agent`: `pytest test_gateway_error_mapping.py -q`) and the full existing suite `pytest -q` to catch any other regression.
- Expected: exit 0, all tests green, zero references to `agent_loader`/`load_agent` remaining (`git grep -n "agent_loader\|load_agent"` returns nothing outside history).
### Promise
<promise>CHUNK COMPLETE: CHUNK_1_CLEANUP</promise>

## Chunk 2: CHUNK_2_REGISTRY
### Tasks (in order)
1. Read `skill_bundles/genesis-domain.json`, `genesis-maintenance.json`, `genesis-pricing.json` for their real `name`/`system_prompt` fields (source text, not invented).
2. Add 3 entries to `main.py`'s `AGENT_PERSONAS` dict: keys `"genesis_domain"`, `"genesis_maintenance"`, `"genesis_pricing"` (underscore form, matching the dict's existing key convention), values = `(name, system_prompt)` tuples sourced verbatim from each bundle JSON.
3. Add 3 explicit entries to `bundle_loader.py`'s `BUNDLE_SLUG_ALIASES`: `"genesis_domain": "genesis-domain"`, `"genesis_maintenance": "genesis-maintenance"`, `"genesis_pricing": "genesis-pricing"`.
4. Grep `BUNDLE_SLUG_ALIASES` values for collisions before committing (no two keys should map to a slug already claimed by an unrelated persona).
5. Verify `bundle_loader.load_bundle("genesis_domain")` (and the other two) returns non-`None` via a direct Python call (not just HTTP).
6. Confirm `capability_cards.card_for(slug)` auto-derives from the bundle (no explicit per-slug registration table in `capability_cards.py` — verified during planning read) so no further capability-card wiring is needed; still spot-check `card_for("genesis_domain")` returns a real dict, not `None`.
### Validation
- Command: `python -m compileall -q main.py bundle_loader.py && pytest test_bundle_tool_registry.py test_capability_cards.py -q` plus a direct interpreter check: `python -c "import bundle_loader as b; assert b.load_bundle('genesis_domain'); assert b.load_bundle('genesis_maintenance'); assert b.load_bundle('genesis_pricing'); print('OK')"`.
- Expected: exit 0, `OK` printed, existing bundle/persona tests unmodified and still green.
### Promise
<promise>CHUNK COMPLETE: CHUNK_2_REGISTRY</promise>

## Chunk 3: CHUNK_3_DOCS
### Tasks (in order)
1. Write the literal counting one-liner and run it for real against the post-CHUNK_2 tree: count `skill_bundles/*.json` files, count `AGENT_PERSONAS` keys in `main.py`, count catalogued slugs whose `bundle_loader.resolve_bundle_slug()` output matches a real `skill_bundles/` file, derive the unguarded remainder.
2. Replace `CLAUDE.md:3`'s "Standalone FastAPI gateway serving 20 specialised Genesis AI agents." with accurate non-stale language reflecting the real counts from step 1.
3. Add a new `## Agent Count (verify, don't trust)` section to `CLAUDE.md` with the 4 numbers, the one-liner used to derive them (copy-pasteable), and a one-sentence risk statement about dispatching finance-adjacent work to an unguarded persona-only slug, plus the guardrail list copied in from `.ralph/guardrails.md` (not just linked, per the acceptance criterion — this ralph workspace is disposable, `CLAUDE.md` is not).
### Validation
- Command: `python -m compileall -q main.py` (doc-only chunk; confirms no accidental code edits).
- Expected: exit 0. Paste the actual one-liner output into `.ralph/progress.md` as the evidence for this chunk's numbers.
### Promise
<promise>CHUNK COMPLETE: CHUNK_3_DOCS</promise>

## Chunk 4: CHUNK_4_RETRIEVAL
### Tasks (in order)
1. Create `retrieval_store.py`: psycopg3 connection/query layer following `job_store.py`'s pattern — `_database_url()` reads `ASSISTANT_PG_DATABASE_URL`, `_conn()` raises if unset, a `query_chunks(query, top_k, entity_filter, include_superseded)` function that runs the hybrid lexical+vector SQL against the documented `vault_chunks`-shaped table (columns per spec: `chunk_id, vault_path, heading_path, chunk_index, content_sha256, content_text, embedding, entity, type, status, updated, supersedes, indexed_at`), with `status='superseded'` excluded via `WHERE` unless `include_superseded`, metadata filters applied before ranking, `ts_rank`/`plainto_tsquery` + pgvector `<=>` combined, `updated DESC` only as a tiebreaker, and never deduplicating same-topic active chunks. Document the minimum column shape as a code comment.
2. Create `retrieval_route.py`: FastAPI `APIRouter` exposing `POST /retrieval/query`. Pydantic request model (`query: str`, `top_k: int = 8`, `entity_filter: str | None = None`, `include_superseded: bool = False`). Response includes per-chunk `chunk_id` (`{vault-relative-path}#{heading-path}@{chunk-index}`), `content_sha256`, `citation`, and frontmatter fields `entity/type/status/updated/supersedes`; top-level `index_updated_at`, `stale` (vs `RETRIEVAL_STALENESS_THRESHOLD_HOURS`, default 24), `refusal`/`chunks: []` when nothing scores above `RETRIEVAL_MIN_SCORE` (default 0.35) — HTTP 200 always, never calls an LLM.
3. Handle connection failure / unset `ASSISTANT_PG_DATABASE_URL`: catch in the route, return `{"refusal": true, "reason": ..., "chunks": [], "degraded": true, "degraded_reason": "assistant-tier PG unavailable"}` with HTTP 200 — never a 500, never crashes gateway boot.
4. Mount into `main.py`: `from retrieval_route import router as retrieval_router` + `app.include_router(retrieval_router, dependencies=[Depends(verify_gateway_key)])` near the other route registrations (avoids a circular import since `verify_gateway_key` lives in `main.py`).
5. Add `ASSISTANT_PG_DATABASE_URL`, `RETRIEVAL_MIN_SCORE`, `RETRIEVAL_STALENESS_THRESHOLD_HOURS` to `.env.example` with the column-shape comment.
### Validation
- Command: `python -m compileall -q main.py retrieval_route.py retrieval_store.py bundle_loader.py` plus a manual boot check: start `uvicorn main:app` with `ASSISTANT_PG_DATABASE_URL` unset and confirm `GET /health` returns 200 and `POST /retrieval/query` (with a valid `GATEWAY_API_KEY`) returns HTTP 200 with `degraded: true`.
- Expected: exit 0, gateway boots and serves both endpoints correctly in the unset-env condition.
### Promise
<promise>CHUNK COMPLETE: CHUNK_4_RETRIEVAL</promise>

## Chunk 5: CHUNK_5_TESTS
### Tasks (in order)
1. Create `test_retrieval_route.py` (repo root) using `TestClient(main.app)` + `monkeypatch` to stub `retrieval_store`'s query function (no live Postgres) — one test per Retrieval Contract row: chunk ID format, citation format, superseded filtering (default-excluded / explicit-included), contradiction surfacing (two active chunks both returned), staleness flag true/false, hybrid ranking (metadata-filtered-out row never appears even if it would out-score an included one), refusal path (HTTP 200, `chunks: []`).
2. Add the degraded-connection test (`ASSISTANT_PG_DATABASE_URL` unset or connection mocked to raise -> HTTP 200 `degraded: true`) and a companion `GET /health` still-200-in-same-condition test.
3. Add the auth test reusing `tests/test_gateway_key_guard.py`'s pattern: `POST /retrieval/query` without a valid key returns the same 401 shape.
4. Add a grep/mock-assert test proving no `call_llm_router` (or any LLM client) is invoked anywhere in `retrieval_route.py`'s/`retrieval_store.py`'s source during a request.
5. Add the `@pytest.mark.skipif(not os.getenv("ASSISTANT_PG_DATABASE_URL"), ...)` real-connection integration test stub (skipped today, activates once the external PG workstream is reachable).
6. Add the malformed-request test (missing `query` -> FastAPI 422).
7. Write the exact validation command into `AGENTS.md`'s `## Validation Commands` section (already present from scaffolding — confirm it matches the real test file list, update if the final test filenames differ).
### Validation
- Command: `python -m compileall -q main.py retrieval_route.py retrieval_store.py bundle_loader.py && pytest test_retrieval_route.py tests/test_prohibited_tools.py tests/test_escrow_containment.py tests/test_gateway_key_guard.py -q`
- Expected: exit 0, all tests green, zero live-Postgres dependency in the default run.
### Promise
<promise>CHUNK COMPLETE: CHUNK_5_TESTS</promise>

## Chunk 6: CHUNK_6_EVALCLEANUP
### Tasks (in order)
1. `git grep -i "evals/"` (excluding `eval/` hits) to confirm zero live references before deleting.
2. Delete the `evals/` directory (`run_evals.py`, `graders/`, `reports/`, `tasks/`) entirely.
3. Confirm `eval/` (singular) is untouched (no diff).
### Validation
- Command: `python -m compileall -q .` and re-run `git grep -i "evals/"` to confirm zero hits.
- Expected: exit 0, `eval/`'s own 13-file test suite still passes unmodified.
### Promise
<promise>CHUNK COMPLETE: CHUNK_6_EVALCLEANUP</promise>
