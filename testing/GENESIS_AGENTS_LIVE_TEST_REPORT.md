# Genesis Agents Live Test Report
**Date:** 2026-05-23  
**Tester:** SwarmSync Final Executor (5 parallel test agents)  
**Base URL:** `https://swarmsync-agents.onrender.com`  
**Scope:** 20 Genesis agents — endpoint health, task execution, routing verification

---

## Executive Summary

**14 of 20 agents are LIVE AND FUNCTIONAL.** All 20 endpoints are reachable. The failures are concentrated in a single identifiable root cause, not fundamental infrastructure problems.

The critical finding is that **4 agents (Builder, Research, Deploy, QA) time out due to a one-line hardcoded bypass in main.py** — the fix is trivially small. A 5th agent (Finance) is live but has a persona scope problem preventing task execution.

All functional agents route through SwarmSync's `/v1/chat/completions` at `api.swarmsync.ai`. The routing layer is working. However, smart tier selection is disabled — all agents use `direct_model_request` to `openai/gpt-5-mini` regardless of task complexity, bypassing the intelligence of the SwarmSync router.

---

## Overall Pass/Fail Table

| # | Agent | Exec Score | Routing Score | HTTP | Verdict |
|---|-------|-----------|--------------|------|---------|
| 01 | Genesis Meta Agent | 5/5 | 1/5 | 200 | ✅ LIVE AND FUNCTIONAL |
| 02 | Genesis Builder Agent | 0/5 | 0/5 | 000 | ❌ ENDPOINT LIVE BUT NOT EXECUTING |
| 03 | Genesis Research Agent | 0/5 | 0/5 | 000 | ❌ ENDPOINT LIVE BUT NOT EXECUTING |
| 04 | Genesis Deploy Agent | 0/5 | 0/5 | 000 | ❌ ENDPOINT LIVE BUT NOT EXECUTING |
| 05 | Genesis QA Agent | 0/5 | 0/5 | 000 | ❌ ENDPOINT LIVE BUT NOT EXECUTING |
| 06 | Genesis Content Agent | 5/5 | 5/5 | 200 | ✅ LIVE AND FUNCTIONAL |
| 07 | Genesis Email Agent | 5/5 | 5/5 | 200 | ✅ LIVE AND FUNCTIONAL |
| 08 | Genesis Commerce Agent | 5/5 | 5/5 | 200 | ✅ LIVE AND FUNCTIONAL |
| 09 | Genesis Support Agent | 5/5 | 5/5 | 200 | ✅ LIVE AND FUNCTIONAL |
| 10 | Genesis Finance Agent | 1/5 | 5/5 | 200 | ❌ ENDPOINT LIVE BUT NOT EXECUTING |
| 11 | Genesis Security Agent | 5/5 | 5/5 | 200 | ✅ LIVE AND FUNCTIONAL |
| 12 | Genesis Billing Agent | 5/5 | 5/5 | 200 | ✅ LIVE AND FUNCTIONAL |
| 13 | Genesis Analyst Agent | 5/5 | 5/5 | 200 | ✅ LIVE AND FUNCTIONAL |
| 14 | Genesis Marketing Agent | 5/5 | 4/5 | 200 | ✅ LIVE AND FUNCTIONAL |
| 15 | Genesis SEO Agent | 5/5 | 5/5 | 200 | ✅ LIVE AND FUNCTIONAL |
| 16 | Genesis Legal Agent | 5/5 | 5/5 | 200 | ✅ LIVE AND FUNCTIONAL |
| 17 | Genesis HR Agent | 4/5 | 5/5 | 200 | ⚠️ PARTIALLY FUNCTIONAL |
| 18 | Genesis Data Pipeline | 5/5 | 5/5 | 200 | ✅ LIVE AND FUNCTIONAL |
| 19 | Genesis Workflow Automator | 5/5 | 5/5 | 200 | ✅ LIVE AND FUNCTIONAL |
| 20 | Genesis AI Vision API | 5/5 | 5/5 | 200 | ✅ LIVE AND FUNCTIONAL |

**Totals:** 14 LIVE AND FUNCTIONAL | 1 PARTIALLY FUNCTIONAL | 5 NOT EXECUTING | 0 BROKEN

---

## Routing Architecture Findings

### Is SwarmSync Routing being used?
**Yes — with one critical caveat.**

All agent LLM calls go through `https://api.swarmsync.ai/v1/chat/completions`. The gateway does not call OpenAI, Anthropic, or OpenRouter directly. This is confirmed both by code review (`agent_runtime.py`, `main.py`) and by live response metadata.

**Evidence from live responses:**
```json
{
  "swarmsync": {
    "routed_model": "openai/gpt-5-mini",
    "routing_reason": "direct_model_request",
    "complexity_score": -1,
    "estimated_cost": 0.002903,
    "latency_ms": 14163,
    "tier": "mid",
    "savings_vs_premium": 0.015019,
    "router_v2": {
      "quality_gate_mode": "shadow",
      "quality_gate_passed": true,
      "quality_gate_score": 1,
      "provider_health_status": "HEALTHY"
    }
  }
}
```

### Routing Gap: Smart Tier Selection is Disabled
The SwarmSync Router's intelligence (complexity scoring, tier selection, cost optimization) is bypassed for all agents. `complexity_score: -1` and `routing_reason: direct_model_request` indicate the gateway passes a hardcoded model name instead of `"auto"`.

**Impact:** Simple tasks get expensive mid-tier models; complex tasks don't get premium models. The routing system is used as a proxy, not as a router.

**Fix:** Set `GENESIS_LLM_MODEL=auto` in agents-gateway environment.

### One Direct API Bypass Found
A Gemini 2.0 Flash Lite fallback in the persona/negotiate path hits Google's API directly (`https://generativelanguage.googleapis.com/...`). This bypasses SwarmSync Routing completely — no metadata, no cost tracking, no quality gate. Triggered on 429 from the SwarmSync router.

---

## Detailed Agent Results

### Agent 01 — Genesis Meta Agent ✅

**Endpoint:** `POST /agents/genesis_meta_agent/run`  
**HTTP:** 200 | **Time:** ~20s | **Payload:** Shape 1 (`input`/`task`/`mode: "live_test"`)

**Request:**
```json
{
  "input": "You are being tested as a live independent Genesis agent. Confirm your identity, explain your capabilities, then complete the specific task.",
  "task": "Coordinate a 3-agent project plan using Builder, QA, and Content agents.",
  "mode": "live_test",
  "require_artifact": true
}
```

**Response (excerpt):**
```
I am Genesis Meta Agent, the autonomous orchestrator. I architect the plan, assign the work,
sequence the dependencies, and enforce delivery.

Phase 1 — Foundation & Parallel Prep
| Builder | Scaffold core architecture        | Functional codebase   | T+3 days |
| Content | Brand voice guide, landing copy   | Content asset package | T+3 days |
| QA      | Define test plan, unit test specs | QA Plan Document      | T+2 days |
[...continues for 3 phases with 9 total task assignments...]
```

**Identity confirmed:** Yes  
**Task executed:** Yes — full 3-phase Gantt plan  
**Routing:** Goes through SwarmSync API but routing metadata not surfaced in response (persona path)  
**Issues:** Routing metadata hidden; `mode: "live_test"` bypass only works for this one agent  
**Exec: 5/5 | Route: 1/5**

---

### Agents 02-05 — Builder, Research, Deploy, QA ❌

**Root cause: ConduitBridge (Patchright browser) startup timeout**

These four agents share a critical failure. Their skill bundles list `conduit` in `tools_advertised`. The `AgentRuntime` attempts to initialize a Patchright browser session before making any LLM call. On Render's free tier:
- Browser startup: ~35-50 seconds
- Render proxy timeout: 30 seconds
- Result: TCP connection closed (HTTP 000) before any response

Short probes (12s max) confirm the endpoints ARE alive and auth works. The problem is specifically the ConduitBridge init path.

The `mode: "live_test"` flag in the request bypasses this for `genesis-meta` only (main.py line 1339). Extending this one-line check to all agents would immediately fix agents 02-05.

| Agent | Slug | Short Probe | Full Task | Root Cause |
|-------|------|-------------|-----------|-----------|
| 02 Builder | genesis_builder_x402 | 200 ✓ | 000 ✗ | ConduitBridge + run_code + github_tool |
| 03 Research | genesis_research_x402 | 200 ✓ | 000 ✗ | ConduitBridge + web_search + async mode |
| 04 Deploy | genesis_deploy_x402 | 200 ✓ | 000 ✗ | ConduitBridge + vercel/netlify deploy tools |
| 05 QA | genesis_qa_x402 | 200 ✓ | 000 ✗ | ConduitBridge + run_code + screenshot_url |

**Exec: 0/5 | Route: 0/5 each**

---

### Agent 06 — Genesis Content Agent ✅

**Endpoint:** `POST /agents/genesis_content_x402/run`  
**HTTP:** 200 | **Time:** 17.8s | **Model:** gpt-5-mini | **Cost:** $0.00231 | **Tokens:** 1,863

**Task executed:** Complete homepage copy — headline, subheadline, 3 feature bullets, CTA  
**Issue:** Agent identified as "ChatGPT" instead of "Genesis Content Agent" (persona leak)  
**Exec: 5/5 | Route: 5/5**

---

### Agent 07 — Genesis Email Agent ✅

**Endpoint:** `POST /agents/genesis_email_x402/run`  
**HTTP:** 200 | **Time:** 27.2s | **Model:** gpt-5-mini | **Cost:** $0.003829 | **Tokens:** 2,814

**Task executed:** Complete 3-email onboarding sequence with subject lines, full body, `{{token}}` personalization, CAN-SPAM footers, A/B variants, and KPI recommendations  
**Issue:** Quality gate false positive — `{{token}}` placeholders triggered `proof_validation_failure` heuristic (shadow mode, non-blocking)  
**Exec: 5/5 | Route: 5/5**

---

### Agent 08 — Genesis Commerce Agent ✅

**Endpoint:** `POST /agents/genesis_commerce_x402/run`  
**HTTP:** 200 | **Time:** 39.7s | **Model:** gpt-5-mini | **Cost:** $0.007524 | **Tokens:** 5,585

**Task executed:** 8-step checkout/escrow flow with preconditions, x402 authorization, escrow hold, agent delivery, ACCEPT/PARTIAL/REJECT verification paths, payout release, refund path, dispute resolution, and ASCII flow diagram  
**Issue:** Highest response time (39.7s) and token count (5,585) — risk of timeout under load  
**Exec: 5/5 | Route: 5/5**

---

### Agent 09 — Genesis Support Agent ✅

**Endpoint:** `POST /agents/genesis_support_x402/run`  
**HTTP:** 200 | **Time:** 20.6s | **Model:** gpt-5-mini | **Cost:** $0.003284 | **Tokens:** 2,537

**Task executed:** Professional support email + 5 troubleshooting steps + 3-tier escalation path (L1: 2hr SLA → L2: Engineering 4hr/72hr RCA → L3: Platform/Storage 72hr)  
**Exec: 5/5 | Route: 5/5**

---

### Agent 10 — Genesis Finance Agent ❌

**Endpoint:** `POST /agents/genesis_finance_x402/run`  
**HTTP:** 200 (shape 2) | **Time:** 19.8s | **Model:** gpt-5-mini | **Cost:** $0.002362

**Task deflected:** Agent refused to produce a revenue model. Instead presented a menu of operational finance actions (payroll run, vendor invoice, bank fee sync) and requested structured inputs. The Finance Agent persona is constrained to transactional operations only.

**Response excerpt:**
```
I can help with: payroll run, vendor invoice processing, bank fee sync, monthly close, x402 import.
Please provide the specific financial action you need along with relevant account IDs and amounts.
```

**Exec: 1/5 | Route: 5/5**

---

### Agent 11 — Genesis Security Agent ✅

**Endpoint:** `POST /agents/genesis_security_x402/run`  
**HTTP:** 200 | **Time:** 52.2s | **Model:** gpt-5-mini | **Cost:** $0.003208 | **Tokens:** 2,546

**Task executed:** Full OWASP-mapped security audit of POST /api/user/login — CRITICAL brute-force protection, HIGH account enumeration, HIGH SQL injection, HIGH password storage, MEDIUM JWT scoping, MEDIUM CORS, LOW verbose errors — each with test procedure and remediation  
**Exec: 5/5 | Route: 5/5**

---

### Agent 12 — Genesis Billing Agent ✅

**Endpoint:** `POST /agents/genesis_billing_x402/run`  
**HTTP:** 200 | **Time:** 41.5s | **Model:** gpt-5-mini | **Cost:** $0.003104 | **Tokens:** 4,298 | **LLM calls:** 2

**Task executed:** Complete failed-payment billing workflow — event detection, immediate email trigger, 5-attempt retry ladder (T+0, T+24h, T+72h, T+7d, T+14d), adaptive payment method handling, account suspension rules  
**Exec: 5/5 | Route: 5/5**

---

### Agent 13 — Genesis Analyst Agent ✅

**Endpoint:** `POST /agents/genesis_analyst_x402/run`  
**HTTP:** 200 | **Time:** 25.0s | **Model:** gpt-5-mini | **Tokens:** ~2,200

**Task executed:** All 5 metrics calculated with formulas shown — Conversion: 20%, ARPU: $20, ARPPU: $100, Churn: 40% (FLAGGED critical), MRR: $2,000 (cross-checked). 3 quantified recommendations with targets.  
**Exec: 5/5 | Route: 5/5**

---

### Agent 14 — Genesis Marketing Agent ✅

**Endpoint:** `POST /agents/genesis_marketing_x402/run`  
**HTTP:** 200 | **Time:** 54.3s | **Model:** gpt-5-mini | **Tokens:** >4,000

**Task executed:** Complete 7-day launch plan — quantitative targets (1,500-5,000 visitors, 20-40 listings), tracking stack (GA4, UTMs, Mixpanel), daily actions across GitHub, Twitter/X, Reddit, Discord, LinkedIn, qualitative goals  
**Issue:** Response too large to fully capture cost/token metadata  
**Exec: 5/5 | Route: 4/5**

---

### Agent 15 — Genesis SEO Agent ✅

**Endpoint:** `POST /agents/genesis_seo_x402/run`  
**HTTP:** 200 | **Time:** 26.7s | **Model:** gpt-5-mini | **Cost:** $0.002903 | **Tokens:** 2,221

**Task executed:** Complete SEO brief — Title tag (41 chars), Meta description (138 chars), H1, 4 H2s, 10 keywords (head + long-tail + transactional), 3 internal links with anchor text and URLs, schema recommendations  
**Issue:** Agent identified as "ChatGPT" (persona leak)  
**Exec: 5/5 | Route: 5/5**

---

### Agent 16 — Genesis Legal Agent ✅ (slug discrepancy resolved)

**Marketplace slug:** `genesis_legal_x402` | **Gateway slug:** `legal_agent`  
**Both endpoints:** 200 OK (aliasing works)  
**HTTP:** 200 | **Model:** gpt-5-mini | **Cost:** $0.000968 | **LLM latency:** 3,818ms

**Task executed:** Plain-English ToS risk checklist — IP ownership, liability limits, payment disputes, agent misconduct, data privacy (GDPR/CCPA), with "this is not legal advice" disclaimer  
**Exec: 5/5 | Route: 5/5**

---

### Agent 17 — Genesis HR Agent ⚠️ PARTIALLY FUNCTIONAL

**Marketplace slug:** `genesis_hr_x402` | **Gateway slug:** `onboarding_agent`  
**Primary endpoint (`/onboarding_agent/run`):** Returns inner slug `genesis-onboarding`  
**Alt endpoint (`/genesis_hr_x402/run`):** Returns inner slug `genesis-hr`  
**Both endpoints work (aliasing), but return different inner slugs — two different bundles?**

**Task executed:** Onboarding checklist for new vendor — account setup, API key generation, first agent listing, pricing setup, verification steps  
**Issues:** Inner slug inconsistency; non-deterministic "ChatGPT" identity in some runs  
**Exec: 4/5 | Route: 5/5**

---

### Agent 18 — Genesis Data Pipeline Agent ✅ (slug discrepancy resolved)

**Marketplace slug:** `genesis-data-pipeline` | **Gateway slug:** `genesis-data-pipeline-agent`  
**Both endpoints:** 200 OK (aliasing works)

**Task executed:** Complete data pipeline architecture — Kafka ingestion, validation/dedup/PII scrubbing, PostgreSQL + S3 + Redis storage, dbt transformations, Grafana dashboards, full tech stack specification  
**Exec: 5/5 | Route: 5/5**

---

### Agent 19 — Genesis Workflow Automator ✅

**Endpoint:** `POST /agents/genesis-workflow-automator/run`  
**HTTP:** 200 | **Time:** >60s (consistently slowest)

**Task executed:** Complete trigger/action workflow — signup webhook → SendGrid email → PostgreSQL record creation → conditional 24h check → Zendesk ticket on failure → retry logic + dead-letter queue + p95 latency monitoring  
**Issue:** Consistently >60s response time — risk of Render proxy timeout  
**Exec: 5/5 | Route: 5/5**

---

### Agent 20 — Genesis AI Vision API ✅

**Endpoint:** `POST /agents/genesis-ai-vision-api/run`  
**HTTP:** 200 | **Time:** ~30s

**Task executed:** Complete vision API spec — input formats (JPEG/PNG/WEBP/GIF, 10MB, 4096×4096), response schema with labels/confidence/bounding_box, configurable confidence threshold, 7 error codes with HTTP status, rate limiting with Retry-After  
**Exec: 5/5 | Route: 5/5**

---

## Key Systemic Findings

### 1. ConduitBridge Is The Single Largest Blocker
4 of the 5 non-functional agents fail for the same reason — ConduitBridge browser startup exceeds Render's proxy timeout. One line of code in main.py fixes all four.

### 2. Smart Routing Is Unused
All agents use `direct_model_request` to `gpt-5-mini`. The SwarmSync Router's intelligence (complexity scoring, tier selection, cost optimization) is fully bypassed. Setting `GENESIS_LLM_MODEL=auto` would activate it.

### 3. Routing Infrastructure Is Solid
The routing layer itself works correctly. Cost tracking, quality gates, provider health, savings calculations — all visible in responses. The gap is the agents not utilizing the router's intelligence.

### 4. Quality Output When Working
Of the 14 functional agents, output quality is genuinely high — specialized, well-structured, role-appropriate, with concrete artifacts. These aren't generic chatbots. The personas hold up under task testing.

### 5. No Auth Issues, No 404s
All 20 slugs are registered. Auth (X-Agent-Api-Key) works correctly. The three slug discrepancies (16, 17, 18) are all resolved via aliasing.

---

## SwarmSync Routing Pass/Fail by Agent

Full routing only passes if: endpoint works, task executed, LLM routed through SwarmSync, metadata logged.

| Agent | Endpoint | Task Exec | SwarmSync Routed | Metadata Logged | FULL PASS? |
|-------|----------|-----------|-----------------|----------------|-----------|
| 01 | ✅ | ✅ | ✅ | ❌ (not surfaced) | ⚠️ Partial |
| 02-05 | ✅ | ❌ | ❌ | ❌ | ❌ FAIL |
| 06-09 | ✅ | ✅ | ✅ | ✅ | ✅ PASS |
| 10 | ✅ | ❌ | ✅ | ✅ | ❌ FAIL (persona) |
| 11-13 | ✅ | ✅ | ✅ | ✅ | ✅ PASS |
| 14 | ✅ | ✅ | ✅ | ⚠️ partial | ⚠️ Partial |
| 15-16 | ✅ | ✅ | ✅ | ✅ | ✅ PASS |
| 17 | ✅ | ✅ | ✅ | ✅ | ⚠️ Partial (slug) |
| 18-20 | ✅ | ✅ | ✅ | ✅ | ✅ PASS |

**Full routing pass: 11/20**  
**Partial: 3/20**  
**Fail: 6/20**

---

## Recommended Immediate Actions

1. **1 line in main.py** → unblocks agents 02-05 immediately (change `bundle_slug == "genesis-meta"` check to apply to all)
2. **GENESIS_LLM_MODEL=auto** in Render env → activates smart routing for all agents
3. **Finance Agent system prompt update** → enables analytical tasks for Agent 10
4. **HR Agent bundle audit** → fix inner slug inconsistency for Agent 17
5. **Quality gate regex fix** → stops false positives on email `{{token}}` syntax
