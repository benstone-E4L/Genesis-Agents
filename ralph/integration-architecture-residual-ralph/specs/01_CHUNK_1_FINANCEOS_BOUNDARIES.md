# CHUNK_1_FINANCEOS_BOUNDARIES: Protect the established FinanceOS architecture with regression tests

## Repository Ownership

Work only in `C:\Users\Work\Desktop\vault\projects\E4L-FinanceOS\app`. Do not edit Genesis or this Ralph workspace except for Ralph state and progress files.

## Summary

Add an automated boundary-test pack to the existing FinanceOS suite. This chunk protects the already-built ownership rules without rebuilding Blob, Registry, Airtable, Metabase, Phoenix, or any financial connector.

## Acceptance Criteria

- [ ] A focused test fails if FinanceOS production source or production dependencies introduce Trigger.dev or Composio.
- [ ] Tests prove FinanceOS exposes no Composio path to Xero, Stripe, Gusto, or Expensify.
- [ ] Tests protect Airtable as a workbench/document-reference sync rather than a direct financial-state writer.
- [ ] Covered Phoenix instrumentation remains non-blocking, and existing payment/financial-write approval tests remain green.
- [ ] Tests use no live secrets, make no external network calls, and duplicate no completed implementation.
- [ ] `npm run verify` passes with zero failures at the exact FinanceOS HEAD.

## Endpoints / Interfaces

No HTTP endpoints — test-only architecture boundary coverage.

## Database Changes

No schema changes in this chunk.

## Test Scenarios

- **Happy path**: the current approved FinanceOS source passes every boundary assertion.
- **Edge case**: prohibited names in comments, fixtures, or development-only files do not cause an unjustified production-boundary result.
- **Failure case**: a fixture that simulates a forbidden production dependency or connector path makes the boundary test fail clearly.
- **Integration**: the test is included in the normal `npm run verify` gate and preserves the existing approval suite.

## Dependencies

- **Requires**: None
- **Blocks**: None; the Genesis chunks are independently buildable.

## Completion Promise

<promise>CHUNK COMPLETE: CHUNK_1_FINANCEOS_BOUNDARIES</promise>
