# SPEC: Genesis Agents — Azure Container Apps + Phoenix (repo-local slice)

## Metadata
- Version: 1.0 | Date: 2026-08-05 | Tier: FULL | Brownfield
- Status: Ready for Build
- Parent: `C:\Users\Work\Desktop\Energy 4 Life\E4L Finance OS\specs\SPEC-azure-phoenix-infrastructure-migration.md`
- Success measure: Genesis serves `/health` from an ACA image; conduit-heavy calls no longer need Render's 30s bypass; LLM traces go to self-hosted Phoenix; LangSmith removed from eval harness
- Open questions: inherit parent §14 Q1–Q3 (worker shape default = in-process single replica; bwrap spike; job DB stays SwarmSync-owned)

## Tech Stack
- Python 3.12 (`runtime.txt`)
- FastAPI + uvicorn (`main.py`, `requirements.txt`)
- httpx, pydantic, patchright/conduit-browser, aiohttp, psycopg, boto3, cryptography
- Eval harness: `eval/` + `eval/requirements.txt` (today: langsmith — replace with Phoenix OTel)
- Tests: pytest (`tests/`, `eval/tests/`, `test_sandbox_manager.py`)
- Target host: Azure Container Apps (single replica Phase 1)
- Observability: Arize Phoenix self-hosted (OTel/OpenInference)

## Architecture Grounding Summary
**Touched:** Dockerfile (new), `main.py` Render/URL branches, `artifact_store.py` / `conduit_sessions.py` `/var/data` defaults, `eval/traceable.py` + eval tests/requirements, `.env.example`, public URL defaults in `eval/genesis_client.py` / A2A cards.
**Not touched:** `runtime/tool_policy.py`, `agent_runtime.py`, skill bundles, 57 agent logics, SwarmSync-owned job Postgres, Cato, FinanceOS.

**Must not break:** gateway key guard; escrow containment; money-path prohibited tools; job `FOR UPDATE SKIP LOCKED`; tracing never blocks agent execution; secret redaction into traces.

---

## 1. Executive Summary
Containerize Genesis Agents for Azure Container Apps, strip Render-only assumptions, and move eval tracing from LangSmith to Phoenix so finance-adjacent traces stay on company Azure. Agent behavior is unchanged.

## 2. Scope & Do Not Build

**In scope:** Dockerfile; Render→generic/Azure code; artifact/session path defaults; Phoenix swap; tests; env docs.

### Do Not Build
- Azure portal provisioning / Key Vault / DNS cutover (HUMAN GATE)
- Multi-replica in-memory store rewrites (single-replica Phase 1)
- Agent logic / tool policy / skill bundles
- Migrating Genesis job Postgres (SwarmSync-owned)
- Cato or FinanceOS changes
- LangSmith retention

## 3–18 (abridged for ralph — full detail in parent SPEC)

Acceptance (CODE-provable subset): Docker image builds and `GET /health` works locally; no hardcoded `onrender.com` in runtime responses when `PUBLIC_BASE_URL` set; Phoenix exporter wired; LangSmith gone from `eval/requirements.txt`; degrade tests prove tracing outage does not fail agent calls; money-path + escrow + sandbox tests still pass.

### Build Phases
1. Docker image
2. Hosting/URL/artifact decoupling
3. Phoenix instrumentation swap
4. Test + harness URL updates
5. Env/docs + single-replica constraint

## Risks
- Live Render traffic — do not decommission until ACA proven (HUMAN GATE)
- bwrap may be unavailable on ACA — fall back to process tier and surface it
- Phoenix must never block requests

## Risks
See Risks above (ralphprep heading).
