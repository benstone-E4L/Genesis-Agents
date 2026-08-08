# CHUNK_5_CONFIG: Document Azure env vars and single-replica constraints.

## Summary

Update `.env.example` and operator docs for ACA + Phoenix. Record single-replica Phase-1
constraint and HUMAN GATE checklist for provision/cutover. No agent logic changes.

## Acceptance Criteria

- [ ] `.env.example` lists `PUBLIC_BASE_URL` / `GENESIS_PUBLIC_BASE_URL`, `GENESIS_COMMIT`,
      `GENESIS_LOCAL_ARTIFACT_DIR`, `GENESIS_SESSION_VAULT_DIR`, Phoenix/OTel vars; marks
      `RENDER_*` as legacy aliases
- [ ] Docs state: Phase 1 = single ACA replica; multi-replica requires rewriting in-memory stores
- [ ] Docs state: bwrap may be unavailable — process tier is degraded mode requiring Ben sign-off (HUMAN GATE)
- [ ] Docs link parent FinanceOS Azure SPEC for shared Phoenix Container App
- [ ] No secrets committed
- [ ] All tests pass with zero failures

## Endpoints / Interfaces

No HTTP endpoints — documentation and env template only.

## Database Changes

No schema changes in this chunk.

## Test Scenarios

- **Happy path**: Fresh clone can configure from `.env.example` comments alone
- **Edge case**: LangSmith vars documented as removed/ignored
- **Failure case**: N/A (docs)
- **Integration**: Matches CHUNK_2/3 env names exactly

## Dependencies

- **Requires**: CHUNK_2_HOSTING, CHUNK_3_PHOENIX, CHUNK_4_TESTS
- **Blocks**: None

## Completion Promise

<promise>CHUNK COMPLETE: CHUNK_5_CONFIG</promise>
