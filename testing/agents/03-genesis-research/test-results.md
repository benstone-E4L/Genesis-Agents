# Agent 03 — Genesis Research Agent

## Endpoint

- **Endpoint:** `POST https://swarmsync-agents.onrender.com/agents/genesis_research_x402/run`
- **Marketplace slug:** `genesis_research_x402`
- **Gateway slug:** `genesis-research` (bundle slug)

## Async job flow (current)

`genesis-research` bundle has `job_mode: "async"` (same as Builder, Deploy, QA, Meta). Real `/run` requests **without** `mode: "live_test"` enqueue a durable job and return immediately:

```json
{
  "status": "QUEUED",
  "slug": "genesis-research",
  "job_id": "<uuid>",
  "poll_url": "/agents/jobs/<uuid>"
}
```

Poll `GET /agents/jobs/{job_id}` until `DELIVERED` or `FAILED`. The worker runs Conduit/browser work off the Render proxy timeout path.

## Regression coverage

- `test_gateway_error_mapping.py::test_conduit_heavy_run_requests_enqueue_async_jobs` includes `genesis_research_x402`
- `skill_bundles/genesis-research.json` — `job_mode: "async"`
- `test_agent_runtime.py` — bundle load for `genesis-research`

## Live test notes

| Mode | Behavior |
|------|----------|
| Default (real task) | Async queue — expect `job_id` + `poll_url`, not a full answer in 30s |
| `mode: "live_test"` | Fast persona router only — skips job queue **and** AgentRuntime (no browser) |

Earlier reports of HTTP 000 / timeout were from holding the sync path open during Conduit startup. That path is no longer used for production-shaped calls.

## Deploy checklist

1. `job_store` / `GENESIS_JOB_DATABASE_URL` configured on gateway + worker
2. Worker service running (`worker.py` polls `genesis_jobs`)
3. SwarmSync API FIX-03 deployed so shared Render IP does not 429 the router
