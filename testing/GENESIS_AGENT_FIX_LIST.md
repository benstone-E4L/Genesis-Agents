# Genesis Agent Fix List
**Generated:** 2026-05-23  
**Priority order: Critical → High → Medium → Low**

---

## 🔴 CRITICAL — Fix Before Any Production Traffic

### FIX-01: Extend `mode: "live_test"` bypass to ALL agents
**Affects:** Agents 02, 03, 04, 05 (Builder, Research, Deploy, QA)  
**Severity:** Critical  
**Root cause:** In `apps/agents-gateway/main.py` line 1339, the sync bypass is hardcoded only for `genesis-meta`:
```python
# CURRENT (broken for all other agents):
if bundle_slug == "genesis-meta" and _prefer_sync_bundle_run(body):
    bundle = None

# FIX (apply to all agents):
if _prefer_sync_bundle_run(body):
    bundle = None
```
**Why critical:** The 4 failing agents (Builder, Research, Deploy, QA) all have `conduit` in `tools_advertised`. AgentRuntime attempts Patchright/ConduitBridge browser startup before any LLM call. On Render's free tier this takes >44 seconds — exceeding Render's 30s proxy timeout. All 4 agents time out with HTTP 000.

**Expected outcome after fix:** All 4 agents route via the persona LLM path (fast, sync, no browser) and respond in 15-40s — matching the behavior of agents 06-20.

---

### FIX-02: Implement async job polling for ConduitBridge-dependent agents
**Affects:** Agents 02, 03, 04, 05 + any future agents with `conduit` tools  
**Severity:** Critical (for production use, not just testing)  
**Status:** Fixed for Builder, Deploy, and QA by adding `job_mode: "async"` to their bundles. Real `/agents/{slug}/run` requests now enqueue durable jobs and return `job_id` plus `poll_url` instead of holding Render's proxy open during Conduit startup.

**Fix:** 
1. Return immediately with `{"job_id": "...", "status": "PROCESSING"}` for conduit tasks
2. Expose `GET /agents/{slug}/jobs/{jobId}` polling endpoint
3. Document async pattern in agent marketplace listings
4. Update test harness to poll instead of wait

---

## 🟠 HIGH — Fix Before Public Beta

### FIX-03: Add `@SkipThrottle` decorator to internal gateway→router calls
**Status:** Fixed in SwarmSync `apps/api/src/modules/routing/routing.controller.ts` — `POST /v1/chat/completions` skips global per-IP throttlers; `PerKeyRateLimitGuard` + route `@Throttle(200/min)` remain. Gateway sends `X-Agent-Gateway-Secret` when `AGENT_GATEWAY_SECRET` is set.

**Deploy (SwarmSync API):** Redeploy `swarmsync-api` after merging. No new env vars required for the exemption itself. Optional: set matching `AGENT_GATEWAY_SECRET` on both `swarmsync-api` and `swarmsync-agents` so gateway LLM calls include `X-Agent-Gateway-Secret` (used for future stricter allowlists). Per-key limits still apply via the gateway's `LLM_API_KEY` (`sk-ss-*`).

**Affects:** All 20 agents (intermittent 429 errors)  
**Severity:** High  
**Root cause:** SwarmSync API's NestJS global ThrottlerModule limits 100 req/min per IP. The agents-gateway (Render) has a single outbound IP — all 20 agents share one IP quota. Rapid parallel testing or burst usage hits this limit.

---

### FIX-04: Expand Finance Agent system prompt for analytical tasks
**Affects:** Agent 10 (Genesis Finance Agent)  
**Severity:** High  
**Root cause:** Finance Agent persona is constrained to transactional operations only (payroll, invoices, bank sync). Analytical tasks like "create a revenue model" are deflected with a request for operational inputs.

**Fix:** Update the `genesis-finance` skill bundle system prompt to include:
- Financial modeling and forecasting
- Revenue model creation
- Unit economics analysis (CAC, LTV, ARR, MRR projections)
- Break-even analysis
- Investor-facing financial summaries

---

### FIX-05: Fix inner slug inconsistency for Genesis HR Agent (Agent 17)
**Affects:** Agent 17  
**Severity:** High  
**Root cause:** The two route aliases (`onboarding_agent` and `genesis_hr_x402`) return different inner slug values in their responses — `genesis-onboarding` vs `genesis-hr` respectively. This suggests two different skill bundles are mapped to these aliases, causing inconsistent behavior depending on which slug the caller uses.

**Fix:**
1. Verify `bundle_loader.py` aliasing table — `genesis_hr_x402` and `onboarding_agent` both map to `genesis-hr`
2. If they map to different bundles, consolidate into one canonical bundle
3. Ensure inner slug in response always returns the canonical slug, not the alias

---

## 🟡 MEDIUM — Fix in Next Sprint

### FIX-06: Fix base model identity leak ("ChatGPT" response)
**Affects:** Agents 06, 15, 17 (and potentially others non-deterministically)  
**Severity:** Medium  
**Root cause:** When asked to confirm identity, some agents respond "I am ChatGPT" (base model identity) instead of their Genesis agent persona name. The system prompt's persona injection doesn't override the model's default self-identification behavior.

**Fix:** Add explicit identity override to all Genesis agent system prompts:
```
You are [AGENT_NAME], a specialized Genesis agent on the SwarmSync marketplace. 
If asked your identity, ALWAYS identify yourself as [AGENT_NAME], not as ChatGPT, 
GPT-5, or any underlying model. Your identity is your agent role, not your model.
```

---

### FIX-07: Fix quality gate false positive on email template tokens
**Affects:** Agent 07 (Genesis Email Agent)  
**Severity:** Medium  
**Root cause:** The quality gate's `proof_validation_failure` heuristic flags `{{first_name}}`, `{{company_name}}` email template tokens as "placeholder/citation text." This produces false quality gate failures for valid email marketing output.

**Fix:** Update the quality gate regex in the SwarmSync API to exclude double-brace `{{...}}` template tokens from the citation/placeholder check. These are valid Handlebars/Liquid template syntax, not fake citations.

---

### FIX-08: Normalize all Genesis agent slugs (marketplace ↔ gateway alignment)
**Affects:** Agents 16, 17, 18  
**Severity:** Medium  
**Root cause:** Three agents have slug mismatches between their marketplace listing and gateway route:
- Agent 16: `genesis_legal_x402` vs `legal_agent`  
- Agent 17: `genesis_hr_x402` vs `onboarding_agent`
- Agent 18: `genesis-data-pipeline` vs `genesis-data-pipeline-agent`

Aliasing resolves this functionally, but the inconsistency creates confusion and documentation debt.

**Fix:** Standardize all slugs to use the `genesis_[role]_x402` pattern across both marketplace and gateway. Update bundle_loader.py aliases accordingly.

---

### FIX-09: Enable smart routing (auto tier selection) instead of direct_model_request
**Affects:** All 20 agents  
**Severity:** Medium  
**Root cause:** All agents route with `complexity_score: -1` and `routing_reason: direct_model_request` — the SwarmSync Router's intelligent tier selection (economy/mid/premium based on task complexity) is bypassed. All calls land on `openai/gpt-5-mini` regardless of task complexity.

**Fix:** Change the agents-gateway's default model from a hardcoded model string to `"auto"`:
```python
# apps/agents-gateway/.env or agent_runtime.py
GENESIS_LLM_MODEL=auto   # default; enables SwarmSync complexity-based routing
```
This enables complexity scoring, proper tier selection, and cost optimization for simple vs. complex tasks.

---

### FIX-10: Surface routing metadata in API response for Agent 01
**Affects:** Agent 01 (Genesis Meta Agent)  
**Severity:** Medium  
**Root cause:** Agent 01 uses the persona fallback path (not AgentRuntime), which calls the SwarmSync router internally but does not bubble up routing metadata to the caller. Agents 06-20 all surface routing metadata via the `swarmsync` response block; Agent 01 does not.

**Fix:** Update the persona path's `call_llm_router()` in `main.py` to attach the `swarmsync` metadata block from the router response to the outgoing API response — same as AgentRuntime does.

---

## 🟢 LOW — Cleanup / Quality of Life

### FIX-11: Eliminate Gemini direct-API fallback (routing bypass)
**Status:** Fixed — removed `call_gemini_fallback` / `generativelanguage.googleapis.com` from `main.py`; negotiate returns safe default on router 429. Regression: `test_main_py_has_no_direct_google_generative_language_api`, `test_call_llm_router_targets_swarmsync_only`.
**Affects:** All agents using the persona/negotiate path  
**Severity:** Low  
**Root cause:** Legacy path has a hardcoded Gemini 2.0 Flash Lite fallback via direct `https://generativelanguage.googleapis.com/...` — bypasses SwarmSync Routing completely. No routing metadata, no cost tracking, no tier selection.

**Fix:** Remove the direct Gemini fallback. Replace with a SwarmSync Router call using `model: "google/gemini-2.0-flash-lite"` — this routes through the SwarmSync system and captures metadata.

---

### FIX-12: Add response time monitoring / alerting for slow agents
**Affects:** Agents 08 (39.7s), 11 (52.2s), 14 (54.3s), 19 (>60s)  
**Severity:** Low  
**Note:** All are functional but response times >40s risk Render proxy timeouts. Set up p95 latency alerts and consider streaming responses for complex agents.

---

## Fix Priority Summary

| # | Fix | Severity | Agents Affected | Effort |
|---|-----|----------|----------------|--------|
| FIX-01 | Extend live_test bypass to all agents | 🔴 Critical | 02, 03, 04, 05 | 1 line |
| FIX-02 | Async job polling for conduit agents | 🔴 Critical | 02, 03, 04, 05 | Medium |
| FIX-03 | SkipThrottle for gateway→router | ✅ Done | All 20 | Small |
| FIX-04 | Expand Finance Agent scope | 🟠 High | 10 | Small |
| FIX-05 | Fix HR agent inner slug mismatch | 🟠 High | 17 | Small |
| FIX-06 | Fix ChatGPT identity leak | 🟡 Medium | 06, 15, 17 | Small |
| FIX-07 | Fix quality gate false positive | 🟡 Medium | 07 | Small |
| FIX-08 | Normalize marketplace/gateway slugs | 🟡 Medium | 16, 17, 18 | Small |
| FIX-09 | Enable auto routing tier selection | 🟡 Medium | All 20 | Small |
| FIX-10 | Surface routing metadata for Agent 01 | 🟡 Medium | 01 | Small |
| FIX-11 | Remove Gemini direct-API fallback | ✅ Done | Legacy path | Small |
| FIX-12 | Add latency monitoring/alerting | 🟢 Low | 08, 11, 14, 19 | Small |
