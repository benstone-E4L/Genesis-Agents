# CHUNK_3_GENESIS_COMPOSIO: Add a policy-gated non-financial Composio adapter to Genesis

## Repository Ownership

Work only in `C:\Users\Work\Desktop\vault\projects\My Github\Genesis Agents`. Do not edit FinanceOS or this Ralph workspace except for Ralph state and progress files.

## Summary

Add one Genesis-owned adapter for explicitly approved non-financial Composio actions. Every request must pass the existing `runtime/tool_policy.py` decision before SDK resolution or invocation, with normalized default-deny handling and actor-scoped sessions.

## Acceptance Criteria

- [ ] The current supported Composio Python package/API is verified during implementation, pinned using Genesis conventions, and isolated behind one adapter.
- [ ] Gmail, Google Drive, Slack, Monday.com, Google Calendar, and GitHub require an explicit provider-and-action allowlist entry; unknown providers/actions default deny.
- [ ] Xero, Stripe, Gusto, Expensify, banking, cards, payments, payroll, tax filing, bookkeeping, and aliases are denied before any SDK call.
- [ ] An approved action is still denied when `runtime/tool_policy.py` denies it; an approved and policy-authorized action reaches the adapter exactly once.
- [ ] Connections/sessions bind to the existing actor/user context and never use one unrestricted global session.
- [ ] Logs and raised errors redact fake access tokens, authorization headers, provider secrets, and signed links.
- [ ] FinanceOS, Trigger.dev scheduling, and Genesis hosting remain unchanged.
- [ ] The complete Genesis bounded validation command passes with zero failures.

## Endpoints / Interfaces

No new public HTTP endpoint — agent runtime calls the Genesis-owned adapter only after the existing policy decision.

## Database Changes

No schema changes in this chunk.

## Test Scenarios

- **Happy path**: an actor-scoped, allowlisted non-financial action passes policy and invokes the mocked adapter once.
- **Edge case**: case, punctuation, aliases, and provider-qualified names cannot bypass financial-provider denial.
- **Failure case**: unknown actions, policy denial, SDK errors, and missing credentials fail clearly without FinanceOS mutation or secret leakage.
- **Integration**: repository search and unit tests prove application code reaches Composio through the adapter only.

## Dependencies

- **Requires**: None
- **Blocks**: CHUNK_4_GENESIS_VERIFY

## Completion Promise

<promise>CHUNK COMPLETE: CHUNK_3_GENESIS_COMPOSIO</promise>
