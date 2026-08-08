# CHUNK_5_TESTS: Contract-compliance test suite for the retrieval route

## Summary

CHUNK_4_RETRIEVAL builds the route; this chunk proves it actually implements every row of the
E4L Retrieval Contract table (master spec §9), not just "the endpoint returns 200." Money-path
and escrow tests in this repo already run in a stripped, dependency-free CI job
(`.github/workflows/money-path-guards.yml`) specifically so a missing package never silently
skips a guard — this suite follows the same discipline: it must not require a live Postgres
server to run in CI (the assistant-tier server doesn't exist yet), so it tests against a fake/
mock `retrieval_store` query function or a fixture SQLite-shaped stand-in, exercising the
route's contract logic (formatting, filtering, refusal thresholds, response shape) independent
of the real database. A separate, explicitly-marked integration test (skipped unless
`ASSISTANT_PG_DATABASE_URL` is set) covers the real-connection path for whenever the other
workstream's PG server exists.

## Acceptance Criteria

- [ ] New test file `test_retrieval_route.py` (repo root, matching the existing flat `test_*.py` convention) covers every Retrieval Contract table row as a distinct test function: canonical chunk ID format, citation format, superseded-status filtering (default-excluded, explicit-included), contradiction surfacing (two active chunks both returned, never merged), freshness/staleness flag (`stale=true` past threshold, `stale=false` within it), hybrid ranking (metadata filter applied before scoring — assert a filtered-out row never appears even if it would out-score an included one), and the refusal path (zero chunks above threshold -> HTTP 200 structured refusal, `chunks: []`).
- [ ] A dedicated test proves the degraded-connection path from CHUNK_4_RETRIEVAL: `ASSISTANT_PG_DATABASE_URL` unset (or connection mocked to raise) -> HTTP 200 with `degraded: true`, and a separate test proves `GET /health` still returns 200 in the same unset-env condition (gateway boot safety, not just route-level safety).
- [ ] A test proves auth is enforced: `POST /retrieval/query` without a valid `GATEWAY_API_KEY` returns the same 401/403 shape every other `Depends(verify_gateway_key)` route returns (reuse the existing `test_gateway_key_guard.py` pattern/fixtures rather than reinventing auth-test scaffolding).
- [ ] A test proves the route never calls an LLM — grep-assert or mock-assert that no `call_llm_router` (or any LLM client) is invoked anywhere in `retrieval_route.py`'s or `retrieval_store.py`'s call graph during a request, matching the contract's "the LLM is not called to guess" refusal-path requirement.
- [ ] All new tests run without a live Postgres connection (mock/fixture-backed) and are added to the money-path-guards-style fast CI lane if that lane's install-nothing constraint allows it, or documented as a separate lane if `psycopg`/`fastapi` imports make that impossible — either way, the exact `pytest` invocation for this suite must be written into `AGENTS.md`'s validation gate.
- [ ] One test is explicitly marked (`@pytest.mark.skipif` on `ASSISTANT_PG_DATABASE_URL` unset) as the real-connection integration test, so it exists in the repo now and activates automatically once the assistant-tier PG server workstream finishes — this is the seam between the two workstreams, made executable rather than just documented.
- [ ] All tests pass with zero failures, run via the exact command captured in `AGENTS.md`'s `## Validation Commands` section.

## Endpoints / Interfaces

No HTTP endpoints — test-only chunk. Exercises `POST /retrieval/query` from CHUNK_4_RETRIEVAL via FastAPI's `TestClient`/`httpx` test transport.

## Database Changes

No schema changes in this chunk. Test fixtures simulate `vault_chunks`-shaped rows in-process; no real database is touched.

## Test Scenarios

- **Happy path**: full contract-compliance suite green against the mocked store.
- **Edge case**: a chunk whose `content_sha256` doesn't match its `content_text` (simulated staleness/corruption) — decide and document whether the route trusts the stored hash or recomputes it; write the test to lock in whichever behavior CHUNK_4_RETRIEVAL actually implements (do not invent a third behavior here — this chunk verifies, it does not redesign).
- **Failure case**: malformed request body (missing `query`) returns FastAPI's standard 422, proving the Pydantic schema from CHUNK_4_RETRIEVAL is actually wired to the route, not bypassed.
- **Integration**: this suite is the acceptance gate for the whole workstream — once it's green, the Genesis-side slice of Phase E is complete and ready for the other workstream (assistant-tier PG provisioning, Cato retrieval client) to integrate against, per the master spec's Phase E acceptance test ("same Ask-E4L question answered identically via cloud endpoint and via local fallback" — that end-to-end proof is out of this workstream's scope, but this suite is what makes the Genesis half of it crediblely ready to be called).

## Dependencies

- **Requires**: CHUNK_4_RETRIEVAL
- **Blocks**: None (final chunk in this workstream)

## Completion Promise

<promise>CHUNK COMPLETE: CHUNK_5_TESTS</promise>
