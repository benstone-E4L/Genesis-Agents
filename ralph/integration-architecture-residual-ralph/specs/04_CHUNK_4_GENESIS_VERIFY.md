# CHUNK_4_GENESIS_VERIFY: Prove the Genesis integrations preserve policy and financial boundaries

## Repository Ownership

Work only in `C:\Users\Work\Desktop\vault\projects\My Github\Genesis Agents`. Do not edit FinanceOS or this Ralph workspace except for Ralph state and progress files.

## Summary

Run and strengthen the combined Genesis regression contract after the Blob and Composio chunks. This chunk closes code verification with policy, redaction, failure-mode, structural-search, and exact-HEAD proof while keeping live credential tests as separate operational gates.

## Acceptance Criteria

- [ ] Combined tests prove Blob failures cannot become false success and Composio failures cannot authorize fallback financial connectors or mutate FinanceOS.
- [ ] Secret-redaction tests cover Azure connection material, SAS/signed URLs, Composio credentials, authorization headers, and provider payload secrets.
- [ ] Structural checks find no active boto3, `GENESIS_S3_*`, or `s3://` artifact-backend usage and no direct Composio SDK access outside the adapter.
- [ ] Existing prohibited-tool, payment-risk, Trigger.dev, artifact-store caller, and retrieval-route tests remain green.
- [ ] Dependency changes are pinned and pass the repository's available dependency/security checks without adding application hosting work.
- [ ] The exact Genesis HEAD, clean/scoped diff, commands, exit codes, and test counts are appended to `.ralph/progress.md` with no secrets.
- [ ] Real Azure Blob, Composio, and Airtable smoke tests remain explicitly unverified until separate operational evidence exists.
- [ ] The complete Genesis bounded validation command passes with zero failures.

## Endpoints / Interfaces

No new endpoint — combined contract and regression verification only.

## Database Changes

No schema changes in this chunk.

## Test Scenarios

- **Happy path**: both integrations pass the complete existing Genesis suite and their focused contract tests.
- **Edge case**: normalized financial-provider aliases, empty artifact listings, and actor isolation remain correct together.
- **Failure case**: provider outages, invalid integrity metadata, policy denial, and secret-bearing exceptions fail closed and redact output.
- **Integration**: exact-HEAD evidence demonstrates Genesis behavior while the independent FinanceOS boundary suite remains green under the workspace validation gate.

## Dependencies

- **Requires**: CHUNK_2_GENESIS_BLOB and CHUNK_3_GENESIS_COMPOSIO
- **Blocks**: None

## Completion Promise

<promise>CHUNK COMPLETE: CHUNK_4_GENESIS_VERIFY</promise>
