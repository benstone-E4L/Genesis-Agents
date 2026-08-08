# Genesis Agents — Claude Guide

Standalone FastAPI gateway serving 20 specialised Genesis AI agents.

**Deployed:** `https://swarmsync-agents.onrender.com`  
**Entry point:** `main.py`  
**Endpoint:** `POST /agents/{slug}/run`

## Key files
- `main.py` — FastAPI app, routing, persona fallback path
- `agent_runtime.py` — AgentRuntime class (ConduitBridge + LLM orchestration)
- `bundle_loader.py` — loads skill bundles from `skill_bundles/`
- `skill_bundles/*.json` — agent persona, system prompt, tools, budget per agent
- `conduit/` — git submodule: browser automation layer (Patchright)

## Routing
All agents call the SwarmSync router at `$LLM_API_URL` (default: `https://api.swarmsync.ai/v1/chat/completions`).
`GENESIS_LLM_MODEL` defaults to `auto`, which is passed through to SwarmSync Routing so complexity scoring can choose the model tier. Specific model strings bypass complexity scoring.

## Live test bypass
`mode: "live_test"` or `testContext` in the request body skips AgentRuntime (no ConduitBridge startup)
and routes through the fast persona LLM path. Required on Render free tier (30s proxy timeout).

## Async conduit agents
Builder, Research, Deploy, QA, and Meta use `job_mode: "async"`. Real `/agents/{slug}/run` calls enqueue a durable job and return a JSON response string containing `job_id` and `poll_url`; clients poll `GET /agents/jobs/{job_id}` while `worker.py` runs the browser-heavy task.

## Environment variables
See `.env.example`. Critical: `LLM_API_KEY`, `LLM_API_URL`, `GENESIS_LLM_MODEL`, `AGENT_GATEWAY_SECRET`.

## Conduit submodule
```bash
git submodule update --init --recursive
```

## Running locally
```bash
pip install -r requirements.txt
python -m patchright install chromium
uvicorn main:app --reload --port 8000
```
