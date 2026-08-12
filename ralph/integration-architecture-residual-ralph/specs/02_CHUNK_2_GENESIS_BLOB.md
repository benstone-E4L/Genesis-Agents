# CHUNK_2_GENESIS_BLOB: Replace the Genesis remote artifact backend with Azure Blob

## Repository Ownership

Work only in `C:\Users\Work\Desktop\vault\projects\My Github\Genesis Agents`. Do not edit FinanceOS or this Ralph workspace except for Ralph state and progress files.

## Summary

Replace the boto3/S3 branch in `artifact_store.py` with a Genesis-owned Azure Blob backend while preserving the artifact-store behavior used by current callers. Keep local storage only as an explicit development/test backend and fail closed when production remote storage is expected but unavailable.

## Acceptance Criteria

- [ ] Existing store, retrieve/signed-read, and list-by-job caller behavior remains compatible while internal references become provider-neutral.
- [ ] Azure Blob stores job-scoped objects with filename, content type, size, and SHA-256 integrity metadata where available.
- [ ] Local storage is explicit for development/tests; missing production Blob configuration cannot silently report a local upload as production success.
- [ ] Tests cover upload, download/read reference, list, empty list, missing configuration, transient/provider failure, partial upload, integrity mismatch, and secret redaction.
- [ ] boto3 and obsolete `GENESIS_S3_*` production configuration are removed only after callers and tests use the new backend; `azure-storage-blob` is pinned in `requirements.txt`.
- [ ] Trigger.dev behavior and Genesis hosting remain unchanged.
- [ ] The complete Genesis bounded validation command passes with zero failures.

## Endpoints / Interfaces

No new HTTP endpoints — preserve the public operations exposed by `artifact_store.py` and the existing artifact-serving gateway contract.

## Database Changes

No schema changes in this chunk.

## Test Scenarios

- **Happy path**: a fake Blob client uploads an artifact, preserves metadata, returns a provider-neutral reference, and lists it under the correct job.
- **Edge case**: listing a job with no objects returns the existing empty-result shape.
- **Failure case**: missing production configuration, provider errors, partial upload, or integrity mismatch returns a clear redacted failure without local fallback.
- **Integration**: existing Genesis callers and artifact-record persistence consume the compatible result shape without Trigger.dev changes.

## Dependencies

- **Requires**: None
- **Blocks**: CHUNK_4_GENESIS_VERIFY

## Completion Promise

<promise>CHUNK COMPLETE: CHUNK_2_GENESIS_BLOB</promise>
