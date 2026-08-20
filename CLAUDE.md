# Genesis Agents — Claude Guide

Standalone FastAPI gateway serving 38 bundle-backed agents; see the Agent Count table below for
the catalogue split (38 bundle-backed / 74 catalogued slugs / 36 unguarded persona-only).

**Deployed:** `https://swarmsync-agents.onrender.com`  
**Entry point:** `main.py`  
**Endpoint:** `POST /agents/{slug}/run`

## Key files
- `main.py` — FastAPI app, routing, persona fallback path
- `agent_runtime.py` — AgentRuntime class (ConduitBridge + LLM orchestration)
- `bundle_loader.py` — loads skill bundles from `skill_bundles/`
- `skill_bundles/*.json` — agent persona, system prompt, tools, budget per agent
- `conduit_browser.py` — repository-owned restricted Patchright bridge

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

## Browser runtime

Install Patchright from `requirements.txt`, then run `python -m patchright install chromium`.
The removed Conduit submodule and `conduit-browser` package are not runtime dependencies.

## Running locally
```bash
pip install -r requirements.txt
python -m patchright install chromium
uvicorn main:app --reload --port 8000
```

## Agent Count (verify, don't trust)

As of 2026-08-20 (14 guarded E4L accounting specialists added):

| Count | Number | What it means |
|---|---|---|
| Bundle files on disk | 38 | `skill_bundles/*.json` — real personas + tool whitelists that exist |
| Catalogued `/agents` slugs | 74 | Keys in `main.py`'s `AGENT_PERSONAS` dict — everything the public `GET /agents` listing advertises |
| Guarded (bundle-backed) | 38 | Catalogued slugs whose `bundle_loader.resolve_bundle_slug()` output matches a real `skill_bundles/` file — these route through the multi-turn `AgentRuntime` with a tool whitelist, token/conduit budget caps, and escrow checks |
| Unguarded (persona-only) | 36 | Catalogued slugs with **no** matching bundle — these fall straight through to the single-turn persona/router path: no tool-loop, no budget cap, no escrow check |

Reproduce these numbers yourself in under a minute — do not trust this table blindly, it goes
stale the moment a bundle or persona entry is added or removed:

```bash
python -c "import main, bundle_loader; bf=sorted(p.stem for p in bundle_loader.BUNDLES_DIR.glob('*.json')); ap=list(main.AGENT_PERSONAS.keys()); g=[s for s in ap if bundle_loader.resolve_bundle_slug(s) in bf]; print(f'bundles={len(bf)} personas={len(ap)} guarded={len(g)} unguarded={len(ap)-len(g)}')"
```
Output as of this writing: `bundles=38 personas=74 guarded=38 unguarded=36`.

**Risk:** dispatching finance-adjacent work (payroll, invoicing, refunds, pricing/billing
spend) to one of the 36 unguarded slugs bypasses `escrow_guard.py` entirely — the unguarded
persona/router path has no tool-loop, no budget cap, and no escrow containment check, so a
prompt that gets an unguarded slug to describe taking a financial action is not actually
gated by anything.

**Enforced allowlist (copied here so it survives after the originating ralph workspace is
archived — see `ralph/e4l-retrieval-route-ralph/.ralph/guardrails.md` for the full history):**
the canonical answer to "which Genesis slugs are safe to call for finance-adjacent work" lives
in the Cato repo, not here: `cato/tools/genesis.py::GENESIS_AGENTS` — 20 hand-curated,
verified bundle-backed slugs, with its own independent, config-proof `MONEY_DOMAIN_AGENTS`
denylist. This repo's own 74-slug `/agents` catalogue and 38-file bundle registry are the
underlying ground truth Cato's list was curated from; "resolvable" (bundle-backed) here does
**not** by itself mean "safe for finance work" — always check Cato's allowlist, not just this
repo's guarded/unguarded split, before routing anything money-adjacent to a Genesis slug.
