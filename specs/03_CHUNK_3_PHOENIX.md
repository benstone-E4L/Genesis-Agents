# CHUNK_3_PHOENIX: Swap LangSmith tracing for self-hosted Phoenix.

## Summary

Reuse `eval/traceable.py` call sites; replace LangSmith backend with Phoenix OTel/OpenInference
exporter. Keep the two hard guarantees: secrets never reach traces; tracing never blocks execution.
Remove `langsmith` from `eval/requirements.txt`.

## Acceptance Criteria

- [ ] `eval/traceable.py` exports spans to Phoenix when `PHOENIX_*` / OTel endpoint env is configured
- [ ] Missing/unreachable Phoenix does not fail agent calls (degrade tests updated/green)
- [ ] Secret redaction still strips keys before export (`eval/redaction.py` + tests)
- [ ] `langsmith` removed from `eval/requirements.txt`; Phoenix/OTel packages added as needed
- [ ] `test_real_langsmith.py` replaced or retired — no CI dependency on LangSmith cloud
- [ ] Eval harness docs mention Phoenix, not LangSmith, as the target
- [ ] All tests pass with zero failures

## Endpoints / Interfaces

No HTTP endpoints — internal eval/runtime instrumentation only.

## Database Changes

No schema changes in this chunk.

## Test Scenarios

- **Happy path**: With fake Phoenix exporter, a traced call records a redacted span
- **Edge case**: Empty `PHOENIX`/OTel config → no tracing, call succeeds
- **Failure case**: Exporter raises → call still returns result
- **Integration**: Money-domain block messages still contain no secrets in span payloads

## Dependencies

- **Requires**: CHUNK_1_DOCKER (optional for unit tests; required for container smoke)
- **Blocks**: CHUNK_4_TESTS

## Completion Promise

<promise>CHUNK COMPLETE: CHUNK_3_PHOENIX</promise>
