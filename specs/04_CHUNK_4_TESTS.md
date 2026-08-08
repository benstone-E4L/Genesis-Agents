# CHUNK_4_TESTS: Keep money-path, sandbox, and Phoenix-safe harness tests green.

## Summary

Update eval isolation/degradation/redaction tests for Phoenix env vars; point live/eval clients
at `GENESIS_BASE_URL` / `PUBLIC_BASE_URL` instead of a hard-coded Render default. Prove the
validation gate still protects escrow/money paths after hosting changes.

## Acceptance Criteria

- [ ] `eval/genesis_client.py` default base URL reads env (no sole hard dependency on onrender.com)
- [ ] `testing/live_*.py` prefer `GENESIS_BASE_URL` (keep `RENDER_SERVICE_URL` as alias during overlap)
- [ ] Eval conftest forbids shipping traces to a real third-party endpoint in unit tests
- [ ] `pytest tests/test_prohibited_tools.py tests/test_escrow_containment.py` pass
- [ ] `pytest test_sandbox_manager.py` pass
- [ ] Updated Phoenix degradation + secret-redaction tests pass
- [ ] All tests pass with zero failures

## Endpoints / Interfaces

No new endpoints — test harness only.

## Database Changes

No schema changes in this chunk.

## Test Scenarios

- **Happy path**: Full validation gate exits 0
- **Edge case**: Legacy `RENDER_SERVICE_URL` still accepted as alias
- **Failure case**: Accidental production Phoenix/LangSmith endpoint in unit tests is blocked
- **Integration**: Gate is what ralph uses every iteration

## Dependencies

- **Requires**: CHUNK_2_HOSTING, CHUNK_3_PHOENIX
- **Blocks**: CHUNK_5_CONFIG (docs must match final env names)

## Completion Promise

<promise>CHUNK COMPLETE: CHUNK_4_TESTS</promise>
