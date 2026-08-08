# CHUNK_2_HOSTING: Remove Render-specific host coupling from runtime code.

## Summary

Replace Render-coupled branches with Azure-safe / generic equivalents: commit env fallbacks,
hardcoded `onrender.com` public URLs, and `/var/data` artifact/session defaults. Default
deploy shape remains **single replica** (document constraint; do not rewrite in-memory stores).

## Acceptance Criteria

- [ ] Health/worker payloads prefer `GENESIS_COMMIT` / `CONTAINER_APP_REVISION` (or similar) over `RENDER_GIT_COMMIT` alone (`main.py` ~1561)
- [ ] A2A / demo / card URLs derive from `PUBLIC_BASE_URL` (or `GENESIS_PUBLIC_BASE_URL`) — no hardcoded `https://swarmsync-agents.onrender.com` in **runtime** responses when base URL is set
- [ ] Default local artifact dir is Azure-safe (e.g. `/tmp/genesis-artifacts` or `$GENESIS_LOCAL_ARTIFACT_DIR`) — not only `/var/data/...` (`artifact_store.py`, `main.py`, `conduit_sessions.py`)
- [ ] Sandbox health still reports `bwrap` vs `process` honestly; process fallback remains when bwrap missing
- [ ] Single-replica Phase-1 constraint documented in `RUNTIME_RUNBOOK.md` (or new `docs/AZURE_ACA.md`)
- [ ] All tests pass with zero failures

## Endpoints / Interfaces

Existing health endpoints only — response fields may gain azure-neutral commit keys.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Unchanged contract; host-neutral metadata |
| GET | `/health/sandbox` | Reports isolation tier |

## Database Changes

No schema changes in this chunk.

## Test Scenarios

- **Happy path**: With `PUBLIC_BASE_URL=https://example.test`, A2A card URLs use that host
- **Edge case**: Unset public URL — documented default (may still mention legacy Render for rollback window)
- **Failure case**: Missing bwrap → `isolation=process`, not crash
- **Integration**: Docker image from CHUNK_1 still healthy after code changes

## Dependencies

- **Requires**: CHUNK_1_DOCKER
- **Blocks**: CHUNK_4_TESTS (URL harness), CHUNK_5_CONFIG

## Completion Promise

<promise>CHUNK COMPLETE: CHUNK_2_HOSTING</promise>
