# CHUNK_4_RETRIEVAL: Thin read-only E4L Retrieval Contract route, deployed alongside the gateway

## Summary

Implements the Genesis-side half of Phase E's cloud knowledge service: a read-only HTTP route,
mounted into the existing `main:app` FastAPI gateway, that answers vault-grounded knowledge
queries against the assistant-tier PostgreSQL server's pgvector/`pg_diskann` index — per the
master spec's E4L Retrieval Contract table (§9). **The PG server itself is an external
dependency of another workstream and does not exist yet.** This chunk builds the route and its
query layer against that eventual server, using the same fail-closed, lazy-connection pattern
`job_store.py` already uses for `DATABASE_URL` (see `job_store.py:19-50`) — the gateway must
keep booting and serving `/health` and `/agents/*` even when the assistant-tier PG connection
is unset or unreachable; the retrieval route itself degrades to the refusal path, never a 500
that takes the whole gateway down. This chunk hands off testable contract compliance to
CHUNK_5_TESTS; this chunk is the implementation, that one is the proof.

**Non-negotiable, from the task guardrails:** this route is a disposable, derived index. The
vault is the only source of truth. Nothing in this chunk may write to the vault, and nothing in
this chunk may be treated by any caller as authoritative over a direct vault read — the route
returns citations pointing back at vault file paths precisely so a human or agent can verify
against the real source, never trust the index blindly.

## Acceptance Criteria

- [ ] New module `retrieval_route.py` defines a FastAPI `APIRouter` (not inline in `main.py` — keep `main.py`, already 3,550+ lines, from growing further) exposing `POST /retrieval/query`, mounted into `app` in `main.py` via `app.include_router(retrieval_router)` near the other route registrations.
- [ ] The route requires the existing `verify_gateway_key` dependency (same `GATEWAY_API_KEY` auth every other non-public `/agents/*` route uses) — this is an internal Cato<->Genesis route, not public.
- [ ] Request schema (Pydantic model): `query: str` (required), `top_k: int = 8`, `entity_filter: str | None = None`, `include_superseded: bool = False`.
- [ ] Response schema includes, per the Retrieval Contract table (spec §9), for every returned chunk: `chunk_id` in the exact format `{vault-relative-path}#{heading-path}@{chunk-index}`, `content_sha256`, `citation` (`vault path + heading anchor`, e.g. `knowledge/finance/entity-structure.md#the-entity-map`), and the parsed frontmatter fields `entity`, `type`, `status`, `updated`, `supersedes`.
- [ ] Superseded filtering: chunks with `status: superseded` are excluded from the result set unless `include_superseded=true` was explicitly requested — implemented as a SQL `WHERE` clause, not a post-filter (don't fetch-then-discard rows you didn't need to fetch).
- [ ] Contradiction surfacing: the route never deduplicates or merges two `status: active` chunks that both match the query into one synthesized result — both are returned, each in its own response entry, with no averaging of scores into a single "winner." (Testable: a fixture with two active chunks on the same topic must produce two entries in the response, not one.)
- [ ] Freshness state: response includes a top-level `index_updated_at` (max `indexed_at` across returned rows, or the whole table if that's cheaper and still correct) and a `stale: bool` flag — `stale=true` when `index_updated_at` is older than a configurable threshold (`RETRIEVAL_STALENESS_THRESHOLD_HOURS` env var, default 24).
- [ ] Ranking: hybrid lexical + vector — Postgres full-text (`ts_rank` / `plainto_tsquery`) combined with vector distance (pgvector `<=>` operator against an embedding column), metadata filters (`entity_filter`, `status`) applied first in the `WHERE` clause before ranking, recency (`updated` desc) used only as a tiebreaker between equal-score rows — not as a primary sort key.
- [ ] Refusal path: when zero chunks score above `RETRIEVAL_MIN_SCORE` (env var, default `0.35`), the route returns a structured refusal object (`{"refusal": true, "reason": "no vault answer found above threshold", "chunks": []}`) with **HTTP 200**, not an error status — this is a valid, expected answer shape, not a failure. The LLM is never called to guess in this path (this route does not call an LLM at all — it is retrieval-only, callers do their own synthesis).
- [ ] Connection failure handling: if `ASSISTANT_PG_DATABASE_URL` is unset, or the connection attempt raises, the route returns the same structured refusal shape with an added `"degraded": true, "degraded_reason": "assistant-tier PG unavailable"` field and **HTTP 200** (not 500) — the caller (Cato or a Genesis agent) is expected to already have a local-fallback path per the Retrieval Contract's "Cato local index as offline fallback" clause; this route's job is to fail informatively, not to crash the gateway.
- [ ] Gateway boot is unaffected by a missing/unreachable assistant-tier PG server — `uvicorn main:app` still starts and `/health` still returns 200 with `ASSISTANT_PG_DATABASE_URL` unset (prove this the same way `job_store.py`'s existing tests prove `DATABASE_URL`-unset boot safety).
- [ ] New module `retrieval_store.py` holds the psycopg3 connection + query logic, following `job_store.py`'s existing pattern (`_database_url()`-style env resolution, `dict_row` cursor, lazy per-call connection — no global pool required for a v1 read-only route at this traffic level).
- [ ] All tests pass with zero failures.

## Endpoints / Interfaces

| Method | Path | Description |
|--------|------|-------------|
| POST | /retrieval/query | Vault-grounded hybrid retrieval against the assistant-tier pgvector/pg_diskann index. Auth: `GATEWAY_API_KEY` (same dependency as `/agents/*`). Body: `{query, top_k, entity_filter, include_superseded}`. Returns: `{chunks: [...], index_updated_at, stale, refusal, degraded?}`. |

No other endpoints are added or modified by this chunk.

## Database Changes

No schema changes made *by this chunk* — the assistant-tier PG server and its `vault_chunks`
table (or equivalent) are provisioned and populated by a different, external workstream (per
the master spec §17 Phase E: "assistant-tier PG server provisioned; pgvector/`pg_diskann`
populated by local push" is listed under repos `e4l-work-os`/Cato, not Genesis). This chunk's
`retrieval_store.py` must document, as a code comment and in `.env.example`, the minimum
column shape it expects so the two workstreams can integrate without a live handshake:
`chunk_id text, vault_path text, heading_path text, chunk_index int, content_sha256 text,
content_text text, embedding vector, entity text, type text, status text, updated date,
supersedes text[], indexed_at timestamptz`. If the real table's shape differs when it exists,
that is a follow-up integration task outside this workstream's scope — not a blocker for
building and testing this route against a fixture/mock connection now.

## Test Scenarios

- **Happy path**: with a fixture Postgres (or a mocked `retrieval_store` query function) returning 3 chunks above threshold, the route returns them ranked, with correct `chunk_id`/`citation` formatting and no superseded chunks included by default.
- **Edge case**: `include_superseded=true` returns a superseded chunk that was excluded by default; a query with only low-score matches (all below `RETRIEVAL_MIN_SCORE`) returns the refusal shape with `chunks: []`.
- **Failure case**: `ASSISTANT_PG_DATABASE_URL` unset — route returns the degraded-refusal shape with HTTP 200, gateway `/health` still returns 200, no unhandled exception in logs.
- **Integration**: CHUNK_5_TESTS exercises this route's full contract-compliance surface (chunk ID format, citation format, superseded filtering, contradiction surfacing, staleness flag, refusal path, degraded-connection path) as its own dedicated test file — this chunk only needs its own scenarios above to prove basic correctness before handing off.

## Dependencies

- **Requires**: CHUNK_1_CLEANUP (cleaner `main.py` to mount the router into)
- **Blocks**: CHUNK_5_TESTS

## Completion Promise

<promise>CHUNK COMPLETE: CHUNK_4_RETRIEVAL</promise>
