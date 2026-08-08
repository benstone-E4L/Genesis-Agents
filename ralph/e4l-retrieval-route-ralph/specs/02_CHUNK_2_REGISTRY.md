# CHUNK_2_REGISTRY: Reconcile the 3 orphaned skill bundles into the advertised catalogue

## Summary

`skill_bundles/` has 24 JSON files but only 21 are reachable from any slug in `main.py`'s
`AGENT_PERSONAS` catalogue (the public `/agents` listing) or `bundle_loader.py`'s
`BUNDLE_SLUG_ALIASES`. Three bundles — `genesis-domain.json`, `genesis-maintenance.json`,
`genesis-pricing.json` — exist on disk with real personas/system prompts but no catalogued
slug ever calls `load_bundle()` with a key that resolves to them, confirmed by the
architecture-cartographer audit ("3 of 24 bundles unreachable from the public `/agents`
catalogue", P2). `bundle_loader.resolve_bundle_slug()` already auto-converts underscores to
hyphens (`key.replace("_", "-")`), so a bare `AGENT_PERSONAS` entry keyed `"genesis_domain"`
(etc.) is sufficient to resolve to `genesis-domain.json` — no alias table change is strictly
required, but this chunk adds explicit `BUNDLE_SLUG_ALIASES` entries too for the same
defensive-clarity reason the existing aliases (`legal_agent` -> `genesis-legal`, etc.) were
added, so the mapping is documented in one place instead of relying solely on the naming
convention. This is the optional cleanup named in scope — included because it is a small,
self-contained registry fix with no schema/API surface, not scope creep.

## Acceptance Criteria

- [ ] `main.py`'s `AGENT_PERSONAS` dict gains 3 new entries — `"genesis_domain"`, `"genesis_maintenance"`, `"genesis_pricing"` (or the hyphenated slug form the catalogue already prefers elsewhere — match existing slug-casing convention in `AGENT_PERSONAS`) — each with a `(display_name, system_prompt)` tuple sourced from the corresponding bundle JSON's own persona/description fields, not invented text.
- [ ] `bundle_loader.py`'s `BUNDLE_SLUG_ALIASES` gains explicit entries for the 3 new slugs pointing at `"genesis-domain"`, `"genesis-maintenance"`, `"genesis-pricing"` respectively (defensive, even though the naming convention alone would resolve them).
- [ ] `GET /agents` now lists all 24 catalogued-and-resolvable slugs where a bundle exists (verify via the endpoint's existing response shape — do not change its schema).
- [ ] `GET /agents/{slug}/capabilities` for each of the 3 new slugs returns a real capability card (`capability_cards.py`'s `card_for()`), not a 404 — add capability-card entries if `card_for()` requires an explicit registration per slug (check `capability_cards.py` before assuming auto-derivation).
- [ ] `bundle_loader.load_bundle("genesis_domain")` (and the other two) returns the parsed JSON, not `None` — provable with a direct unit call, not just an HTTP round-trip.
- [ ] No existing slug's resolution changes — this is additive only; re-run the existing bundle/persona tests (`test_bundle_tool_registry.py`) unmodified and confirm they still pass.
- [ ] All tests pass with zero failures.

## Endpoints / Interfaces

No new HTTP endpoints. Existing endpoints affected:

| Method | Path | Change |
|--------|------|--------|
| GET | /agents | Now includes 3 previously-unlisted slugs |
| GET | /agents/{slug}/capabilities | Now resolves for the 3 new slugs instead of 404 |
| POST | /agents/{slug}/run | Now dispatches the 3 new slugs to their real bundles instead of falling through to a generic/unknown-agent path |

## Database Changes

No schema changes in this chunk.

## Test Scenarios

- **Happy path**: `bundle_loader.load_bundle("genesis_domain")` returns the parsed `genesis-domain.json` contents; same for maintenance and pricing.
- **Edge case**: a slug that intentionally has no bundle (a persona-only entry, one of the 36 unguarded personas) is unaffected — this chunk must not accidentally wire a persona slug to the wrong bundle file by a careless alias collision. Grep `BUNDLE_SLUG_ALIASES` values for uniqueness before adding.
- **Failure case**: if a bundle JSON is malformed, `load_bundle()` already logs and returns `None` (existing `except Exception` in `bundle_loader.py:57-62`) — confirm this still holds for the 3 newly-wired bundles by temporarily corrupting a copy in a test fixture, not the real file.
- **Integration**: CHUNK_3_DOCS's 24/21/36 split note is written after this chunk lands, so it reports whatever the true post-reconciliation resolve count is (verify the actual number by counting `AGENT_PERSONAS` keys whose `bundle_loader.resolve_bundle_slug()` result has a matching file in `skill_bundles/`, rather than hardcoding "21" — see CHUNK_3_DOCS for the exact verification method).

## Dependencies

- **Requires**: CHUNK_1_CLEANUP (cleaner `main.py` to patch against; not a hard functional dependency)
- **Blocks**: CHUNK_3_DOCS (doc split numbers must reflect this chunk's outcome)

## Completion Promise

<promise>CHUNK COMPLETE: CHUNK_2_REGISTRY</promise>
