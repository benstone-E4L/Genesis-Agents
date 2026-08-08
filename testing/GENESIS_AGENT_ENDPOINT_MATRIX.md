# Genesis Agent Endpoint Matrix
**Generated:** 2026-05-23  
**Base URL:** `https://swarmsync-agents.onrender.com`

---

| # | Agent Name | Marketplace Slug | Gateway Slug | Tested Endpoint | Alt Slug Works? | Working Payload | HTTP Status | Exec Score | Routing Score | Final Verdict |
|---|-----------|-----------------|-------------|----------------|-----------------|----------------|------------|-----------|--------------|--------------|
| 01 | Genesis Meta Agent | `genesis_meta_agent` | `genesis_meta_agent` | `/agents/genesis_meta_agent/run` | N/A | `{input, task, mode: "live_test"}` | 200 | 5/5 | 1/5 | LIVE AND FUNCTIONAL |
| 02 | Genesis Builder Agent | `genesis_builder_x402` | `genesis-builder` | `/agents/genesis_builder_x402/run` | N/A | `{job_id, poll_url}` for real tasks | 202-style payload | N/A | N/A | ASYNC JOB FLOW |
| 03 | Genesis Research Agent | `genesis_research_x402` | `genesis-research` | `/agents/genesis_research_x402/run` | N/A | `{job_id, poll_url}` for real tasks | 202-style payload | N/A | N/A | ASYNC JOB FLOW |
| 04 | Genesis Deploy Agent | `genesis_deploy_x402` | `genesis-deploy` | `/agents/genesis_deploy_x402/run` | N/A | `{job_id, poll_url}` for real tasks | 202-style payload | N/A | N/A | ASYNC JOB FLOW |
| 05 | Genesis QA Agent | `genesis_qa_x402` | `genesis-qa` | `/agents/genesis_qa_x402/run` | N/A | `{job_id, poll_url}` for real tasks | 202-style payload | N/A | N/A | ASYNC JOB FLOW |
| 06 | Genesis Content Agent | `genesis_content_x402` | `genesis-content` | `/agents/genesis_content_x402/run` | N/A | `{input, task, mode}` | 200 | 5/5 | 5/5 | LIVE AND FUNCTIONAL |
| 07 | Genesis Email Agent | `genesis_email_x402` | `genesis-email` | `/agents/genesis_email_x402/run` | N/A | `{input, task, mode}` | 200 | 5/5 | 5/5 | LIVE AND FUNCTIONAL |
| 08 | Genesis Commerce Agent | `genesis_commerce_x402` | `genesis-commerce` | `/agents/genesis_commerce_x402/run` | N/A | `{input, task, mode}` | 200 | 5/5 | 5/5 | LIVE AND FUNCTIONAL |
| 09 | Genesis Support Agent | `genesis_support_x402` | `genesis-support` | `/agents/genesis_support_x402/run` | N/A | `{input, task, mode}` | 200 | 5/5 | 5/5 | LIVE AND FUNCTIONAL |
| 10 | Genesis Finance Agent | `genesis_finance_x402` | `genesis-finance` | `/agents/genesis_finance_x402/run` | N/A | `{message}` (shape 2) | 200 | 1/5 | 5/5 | ENDPOINT LIVE BUT NOT EXECUTING |
| 11 | Genesis Security Agent | `genesis_security_x402` | `genesis-security` | `/agents/genesis_security_x402/run` | N/A | `{input, task, mode}` | 200 | 5/5 | 5/5 | LIVE AND FUNCTIONAL |
| 12 | Genesis Billing Agent | `genesis_billing_x402` | `genesis-billing` | `/agents/genesis_billing_x402/run` | N/A | `{input, task, mode}` | 200 | 5/5 | 5/5 | LIVE AND FUNCTIONAL |
| 13 | Genesis Analyst Agent | `genesis_analyst_x402` | `genesis-analyst` | `/agents/genesis_analyst_x402/run` | N/A | `{input, task, mode}` | 200 | 5/5 | 5/5 | LIVE AND FUNCTIONAL |
| 14 | Genesis Marketing Agent | `genesis_marketing_x402` | `genesis-marketing` | `/agents/genesis_marketing_x402/run` | N/A | `{input, task, mode}` | 200 | 5/5 | 4/5 | LIVE AND FUNCTIONAL |
| 15 | Genesis SEO Agent | `genesis_seo_x402` | `genesis-seo` | `/agents/genesis_seo_x402/run` | N/A | `{input, task, mode}` | 200 | 5/5 | 5/5 | LIVE AND FUNCTIONAL |
| 16 | Genesis Legal Agent | `genesis_legal_x402` | `legal_agent` ⚠️ | `/agents/legal_agent/run` | ✅ 200 | `{input, task, mode}` | 200 | 5/5 | 5/5 | LIVE AND FUNCTIONAL |
| 17 | Genesis HR Agent | `genesis_hr_x402` | `onboarding_agent` | `/agents/onboarding_agent/run` | ✅ 200 (same inner slug: `genesis-hr`) | `{input, task, mode}` | 200 | 4/5 | 5/5 | FUNCTIONAL |
| 18 | Genesis Data Pipeline | `genesis-data-pipeline` | `genesis-data-pipeline-agent` ⚠️ | `/agents/genesis-data-pipeline-agent/run` | ✅ 200 | `{input, task, mode}` | 200 | 5/5 | 5/5 | LIVE AND FUNCTIONAL |
| 19 | Genesis Workflow Automator | `genesis-workflow-automator` | `genesis-workflow-automator` | `/agents/genesis-workflow-automator/run` | N/A | `{input, task, mode}` | 200 | 5/5 | 5/5 | LIVE AND FUNCTIONAL |
| 20 | Genesis AI Vision API | `genesis-ai-vision-api` | `genesis-ai-vision-api` | `/agents/genesis-ai-vision-api/run` | N/A | `{input, task, mode}` | 200 | 5/5 | 5/5 | LIVE AND FUNCTIONAL |

---

## Routing Architecture Summary

| Field | Value |
|-------|-------|
| LLM Router URL | `https://api.swarmsync.ai/v1/chat/completions` |
| Default model | `auto` (SwarmSync complexity routing) |
| Default tier | `mid` |
| Routing mode | `auto` (SwarmSync complexity scoring enabled) |
| Quality gate mode | `shadow` (monitoring only, not blocking) |
| Auth required | `X-Agent-Api-Key` header (GATEWAY_API_KEY) |
| Known bypass | None — persona/negotiate stay on SwarmSync `/v1/chat/completions` (regression: `test_gateway_error_mapping.py`) |

## Slug Discrepancy Summary

| Agent | Marketplace Slug | Gateway Slug | Resolution |
|-------|-----------------|-------------|-----------|
| 16 Legal | `genesis_legal_x402` | `legal_agent` | Aliased — both work |
| 17 HR | `genesis_hr_x402` | `onboarding_agent` | Aliased — both resolve to `genesis-hr` |
| 18 Data Pipeline | `genesis-data-pipeline` | `genesis-data-pipeline-agent` | Aliased — both work |

## Pass/Fail Summary

| Status | Count | Agents |
|--------|-------|--------|
| LIVE AND FUNCTIONAL | 14 | 01, 06, 07, 08, 09, 11, 12, 13, 14, 15, 16, 18, 19, 20 |
| PARTIALLY FUNCTIONAL | 1 | 17 |
| ENDPOINT LIVE BUT NOT EXECUTING | 5 | 02, 03, 04, 05, 10 |
| BROKEN | 0 | — |
| WRONG SLUG / WRONG ROUTE | 0 | — (aliasing resolved 16, 17, 18) |
