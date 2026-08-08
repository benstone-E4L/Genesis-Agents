# CHUNK_1_DOCKER: Ship a Dockerfile that runs the FastAPI gateway.

## Summary

Reverse-engineer the undocumented Render build/start into a committed `Dockerfile` from
`requirements.txt`, `runtime.txt`, and uvicorn entry (`main:app`). First chunk so hosting
and Phoenix work can be validated in a container. Hands CHUNK_2_HOSTING a runnable image.

## Acceptance Criteria

- [ ] `Dockerfile` installs Python 3.12 deps from `requirements.txt` and starts uvicorn on `$PORT` (default 8000) bound to `0.0.0.0`
- [ ] `.dockerignore` excludes `.env`, caches, and bulky test artifacts without breaking conduit/patchright needs documented in comments
- [ ] `docker build` succeeds on a clean tree
- [ ] Container `GET /health` returns 200 with `{"status":"ok",...}` when required gateway env is provided (or documented boot-open mode)
- [ ] README or `RUNTIME_RUNBOOK` snippet documents the exact `docker run` / ACA start command
- [ ] All tests pass with zero failures

## Endpoints / Interfaces

No new HTTP endpoints — packaging only. Existing `GET /health` must remain.

## Database Changes

No schema changes in this chunk.

## Test Scenarios

- **Happy path**: Built image serves `/health`
- **Edge case**: Missing optional S3 env still boots (local artifact dir creatable)
- **Failure case**: Image build fails closed if `requirements.txt` install fails (no silent empty image)
- **Integration**: Image is the runtime used for later chunk smoke checks

## Dependencies

- **Requires**: None
- **Blocks**: CHUNK_2_HOSTING, CHUNK_3_PHOENIX

## Completion Promise

<promise>CHUNK COMPLETE: CHUNK_1_DOCKER</promise>
