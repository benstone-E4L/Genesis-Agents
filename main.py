"""
SwarmSync Agent Gateway
=======================
Single FastAPI service that exposes all Genesis agents under one deployment.

Each agent is accessible at:  POST /agents/{slug}/run
  Body: { "prompt": "...", "testContext": true }
  Response: { "response": "..." }

Conduit-heavy agents declare `job_mode: "async"`. Real `/run` requests for
those bundles return a queued `{job_id, poll_url}` payload immediately so
Render's request proxy is never held open while browser automation starts.

In testContext mode the gateway routes LLM calls through SwarmSync's routing
layer with a persona system prompt matching the agent. For real traffic, the
gateway can be extended to invoke the actual agent logic (x402 payment, Azure
AI, etc.).

GENESIS_LLM_MODEL defaults to `auto`, which is passed through to the SwarmSync
router so it can run complexity scoring and choose the appropriate model tier.

Environment variables:
  LLM_API_KEY         - required for testContext responses routed through SwarmSync
  PORT                - server port (default 8000)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import subprocess
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Header, BackgroundTasks, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Verification system
from verification_models import (
    ArbitrageVerificationRequest,
    ArbitrageVerificationResult,
    VerifyRequest,
    VerifyJobStatus,
    VerificationResult,
    VerificationSpec,
    VerificationContext,
)
from conduit_verifier import run_verification

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Strong references to fire-and-forget background tasks (asyncio only keeps weak refs).
_background_tasks: set = set()


# Phase 2 — consolidated agent runtime backed by skill bundles.
# Loaded lazily on first /agents/{slug}/run call so the gateway still starts
# even if conduit-browser or aiohttp aren't yet installed in the environment.
try:
    from bundle_loader import load_bundle, list_bundles
    from agent_runtime import AgentRuntime
    _RUNTIME_IMPORT_OK = True
except Exception:  # pragma: no cover - ultra-defensive
    load_bundle = None  # type: ignore[assignment]
    list_bundles = None  # type: ignore[assignment]
    AgentRuntime = None  # type: ignore[assignment]
    _RUNTIME_IMPORT_OK = False
    logger.warning("agent_runtime/bundle_loader could not be imported; persona fallback only")

# Phase 10 - capability cards + marketplace listing endpoints.
from capability_cards import card_for, all_cards

# Phase 9 - output storage + Conduit session delivery.
from pathlib import Path
from fastapi.responses import FileResponse, HTMLResponse
from artifact_store import upload_dir, get_signed_url, list_artifacts

# Static assets directory (Genesis admin UI, etc.). Bundled with the
# agents-gateway Render service via the rootDir build context.
_STATIC_DIR = Path(__file__).parent / "static"
from conduit_sessions import store_session, load_session, delete_session

# Phase 8 — Genesis job state machine. Imported lazily-tolerant so the gateway
# still boots if psycopg or DATABASE_URL aren't configured in dev.
try:
    from job_store import create_job, get_job
    _JOB_STORE_OK = True
except Exception:  # pragma: no cover - ultra-defensive
    create_job = None  # type: ignore[assignment]
    get_job = None  # type: ignore[assignment]
    _JOB_STORE_OK = False
    logger.warning("job_store could not be imported; async job endpoints disabled")

# Phase 7 — VCAP proof bundles. Tolerant import for environments without
# `cryptography` (the verify endpoint will return 503 in that case).
try:
    from proof_bridge import verify_vcap_wrapper_jwt
    _PROOF_BRIDGE_OK = True
except Exception:  # pragma: no cover - ultra-defensive
    verify_vcap_wrapper_jwt = None  # type: ignore[assignment]
    _PROOF_BRIDGE_OK = False
    logger.warning("proof_bridge could not be imported; /proofs/* disabled")

# Phase 6 — escrow client wrappers around /payments/ap2/* on swarmsync-api.
#
# ESCROW CONTAINMENT (docs/FINANCE-TOOL-CONTRACTS.md Sections 3.8 and 6.3).
# All four escrow functions are PERMANENTLY_PROHIBITED. They construct and
# transmit funds movements with no per-call human authorization anywhere in the
# path. escrow_guard.escrow_permitted() is the single choke point: it returns
# True only under an explicit GENESIS_DEPLOYMENT_PROFILE=swarmsync-marketplace.
# Unset, or the Cato-facing profile, leaves _ESCROW_OK False, which makes every
# escrow call site below unreachable — the same code path the gateway already
# takes for free-tier (Cato) callers, so nothing new can break.
from escrow_guard import escrow_permitted as _escrow_permitted

if not _escrow_permitted():
    initiate_escrow = None  # type: ignore[assignment]
    complete_escrow = None  # type: ignore[assignment]
    release_escrow = None  # type: ignore[assignment]
    calculate_split = None  # type: ignore[assignment]
    _ESCROW_OK = False
    logger.warning(
        "escrow containment active: escrow_client is NOT bound in this build. "
        "Automation may prepare; a human pays; automation records."
    )
else:
    try:
        from escrow_client import (
            initiate_escrow,
            complete_escrow,
            release_escrow,
            calculate_split,
        )
        _ESCROW_OK = True
    except Exception:  # pragma: no cover - ultra-defensive
        initiate_escrow = None  # type: ignore[assignment]
        complete_escrow = None  # type: ignore[assignment]
        release_escrow = None  # type: ignore[assignment]
        calculate_split = None  # type: ignore[assignment]
        _ESCROW_OK = False
        logger.warning("escrow_client could not be imported; running free-tier only")

_RUNTIME: Any = None


def _get_runtime() -> Any:
    """Return a process-wide AgentRuntime singleton.

    Reads LLM_API_URL / LLM_API_KEY at construction time. Returns None if the
    runtime module failed to import.
    """
    global _RUNTIME
    if not _RUNTIME_IMPORT_OK:
        return None
    if _RUNTIME is None:
        _RUNTIME = AgentRuntime(llm_url=_llm_api_url(), llm_key=_llm_api_key())
    else:
        # Allow env rotation without process restart (local dev / Render reload).
        _RUNTIME.llm_key = _llm_api_key()
        _RUNTIME.llm_url = _llm_api_url()
    return _RUNTIME


_GENESIS_AUTO_WORKER_TASK: Optional[Any] = None


async def _genesis_auto_worker_loop() -> None:
    """In-process background worker loop (Phase 7).

    Activated by GENESIS_WORKER_ENABLED=true. Polls for QUEUED jobs on a fixed
    interval WITHOUT calling the internal tick HTTP endpoint.
    """
    from worker import run_tick, _worker_state  # noqa: PLC0415

    interval = float(os.getenv("GENESIS_WORKER_INTERVAL_SECONDS", "10"))
    tick_limit = int(os.getenv("GENESIS_WORKER_TICK_LIMIT", "3"))
    _worker_state["enabled"] = True
    logger.info(
        "genesis_auto_worker started interval=%.1fs tick_limit=%d", interval, tick_limit
    )
    while True:
        try:
            result = await run_tick(limit=tick_limit, expire_stale=True)
            _worker_state["last_tick_at"] = __import__("time").time()
            if result.get("claimed", 0) > 0:
                logger.info(
                    "genesis_auto_worker tick claimed=%d processed=%d jobs=%s",
                    result["claimed"],
                    result["processed"],
                    result.get("job_ids", []),
                )
        except Exception:
            logger.exception("genesis_auto_worker tick error")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Kick off Patchright Chromium install + optional in-process worker on startup."""
    global _GENESIS_AUTO_WORKER_TASK

    # PERMANENTLY_PROHIBITED enforcement, Layer 2
    # (docs/FINANCE-TOOL-CONTRACTS.md Section 6.2).
    #
    # This runs BEFORE the app accepts traffic and is deliberately NOT wrapped
    # in try/except. If a prohibited money tool has been re-registered, or the
    # frozen manifest no longer matches the prohibition list, the process
    # REFUSES TO START. Fail-to-boot beats fail-to-deny: a denial can be missed
    # in logs; a service that will not start cannot be.
    import tools as _tools
    from runtime.tool_policy import assert_prohibitions_intact

    _tools.register_default_tools()
    assert_prohibitions_intact()
    logger.info("prohibition guard: intact (registry and frozen manifest verified)")

    # Escrow containment, Section 6.3. Also unconditional.
    from escrow_guard import assert_escrow_containment

    assert_escrow_containment()

    # docs/FINANCE-TOOL-CONTRACTS.md Section 7 "Related precondition".
    assert_gateway_key_configured()
    logger.info("gateway auth guard: GATEWAY_API_KEY is configured")

    cache_dir = os.path.expanduser("~/.cache/ms-playwright")
    has_browser = os.path.isdir(cache_dir) and any(True for _ in os.scandir(cache_dir))
    if not has_browser:
        logger.info("Patchright Chromium not found — launching background install...")
        try:
            subprocess.Popen(
                ["python", "-m", "patchright", "install", "chromium"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            logger.warning("Could not start patchright install: %s", exc)
    else:
        logger.info("Patchright Chromium already installed.")

    # Phase 7: in-process auto-worker (does NOT use the tick HTTP endpoint)
    genesis_worker_enabled = os.getenv("GENESIS_WORKER_ENABLED", "").lower() in {
        "1", "true", "yes"
    }
    if genesis_worker_enabled and _JOB_STORE_OK:
        _GENESIS_AUTO_WORKER_TASK = asyncio.create_task(_genesis_auto_worker_loop())
        _background_tasks.add(_GENESIS_AUTO_WORKER_TASK)
        _GENESIS_AUTO_WORKER_TASK.add_done_callback(_background_tasks.discard)
        logger.info("genesis auto-worker enabled and started")

    yield

    if _GENESIS_AUTO_WORKER_TASK is not None and not _GENESIS_AUTO_WORKER_TASK.done():
        _GENESIS_AUTO_WORKER_TASK.cancel()
        try:
            await _GENESIS_AUTO_WORKER_TASK
        except asyncio.CancelledError:
            pass


_environment = os.getenv("ENVIRONMENT", "production")
_docs_url = "/docs" if _environment == "development" else None
_redoc_url = "/redoc" if _environment == "development" else None

app = FastAPI(
    title="SwarmSync Agent Gateway",
    version="1.0.3",
    lifespan=lifespan,
    docs_url=_docs_url,
    redoc_url=_redoc_url,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://swarmsync.ai",
        "https://www.swarmsync.ai",
        "https://api.swarmsync.ai",
    ],
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Agent-Api-Key", "X-Agent-Gateway-Secret"],
)


async def verify_gateway_key(
    x_agent_api_key: str = Header(default=None, alias="x-agent-api-key"),
    x_agent_gateway_secret: str = Header(default=None, alias="x-agent-gateway-secret"),
) -> None:
    """Dependency: validates caller identity against two secrets:

    1. X-Agent-Api-Key     — public API key for Cato, external tools, Render free-tier callers.
       Checked against GATEWAY_API_KEY env var.
    2. X-Agent-Gateway-Secret — internal shared secret sent by swarmsync-api's executeAgent()
       service when calling our own gateway. Checked against AGENT_GATEWAY_SECRET env var.

    The lifespan startup guard (Section 7 of FINANCE-TOOL-CONTRACTS.md) now
    refuses to boot this process at all when GATEWAY_API_KEY is unset, so the
    fallback below is unreachable in a running instance — kept only as a
    defense-in-depth no-op for direct unit-level calls to this dependency.
    Either valid credential is sufficient; both may be provided.
    """
    expected_api_key = os.getenv("GATEWAY_API_KEY")
    if not expected_api_key:
        # Unreachable when the process actually booted; see lifespan() above.
        return

    # Accept if X-Agent-Api-Key matches (constant-time comparison prevents timing oracle)
    if hmac.compare_digest(x_agent_api_key or "", expected_api_key):
        return

    # Accept if X-Agent-Gateway-Secret matches AGENT_GATEWAY_SECRET (internal SwarmSync API calls)
    agent_gateway_secret = os.getenv("AGENT_GATEWAY_SECRET")
    if agent_gateway_secret and hmac.compare_digest(x_agent_gateway_secret or "", agent_gateway_secret):
        return

    raise HTTPException(status_code=401, detail="Invalid or missing X-Agent-Api-Key")


def assert_gateway_key_configured() -> None:
    """docs/FINANCE-TOOL-CONTRACTS.md Section 7 "Related precondition".

    Called from lifespan() before the app accepts traffic, deliberately not
    wrapped in try/except (fail-to-boot beats fail-to-deny) — matching
    assert_prohibitions_intact() and assert_escrow_containment() above it in
    the startup sequence. Without GATEWAY_API_KEY, verify_gateway_key() above
    accepts every caller as authenticated, and Cato's authorization model
    assumes the gateway is authenticated — a fail-open front door makes every
    downstream policy/ledger/approval control merely advisory.
    """
    if not os.getenv("GATEWAY_API_KEY"):
        raise RuntimeError(
            "GATEWAY_API_KEY is not set. Refusing to start: without it, "
            "/agents/{slug}/run and /a2a accept every caller as authenticated "
            "(see verify_gateway_key above). Set GATEWAY_API_KEY before "
            "starting this service."
        )


_DEFAULT_ADMIN_EMAILS = "bullrushinvestments@gmail.com"
_ADMIN_EMAILS_RAW = os.getenv("SWARMSYNC_ADMIN_EMAILS", _DEFAULT_ADMIN_EMAILS)
ADMIN_EMAILS = [e.strip().lower() for e in _ADMIN_EMAILS_RAW.split(",") if e.strip()]
if not ADMIN_EMAILS:
    logger.warning(
        "SWARMSYNC_ADMIN_EMAILS is empty — all /admin/* endpoints will return 503"
    )


async def require_admin(x_admin_email: str = Header(default="", alias="x-admin-email")) -> None:
    """Header-based admin gate for /admin/* endpoints.

    Uses SWARMSYNC_ADMIN_EMAILS env var (comma-separated emails), defaulting
    to bullrushinvestments@gmail.com when the env var is not configured.
    """
    if not ADMIN_EMAILS:
        raise HTTPException(status_code=503, detail="Admin auth not configured")
    email = (x_admin_email or "").strip().lower()
    if not email or email not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="admin access required")

# ---------------------------------------------------------------------------
# Agent persona registry
# Each entry: (display_name, system_prompt)
# ---------------------------------------------------------------------------
AGENT_PERSONAS: dict[str, tuple[str, str]] = {
    # Genesis x402 agents
    "genesis_research_x402": (
        "Genesis Research Agent",
        "You are Genesis Research Agent, an expert AI research assistant that provides deep, "
        "citation-rich research reports. You synthesise information from multiple sources, "
        "identify key trends, and deliver clear, actionable insights. Respond concisely and "
        "stay in character as a professional research analyst.",
    ),
    "genesis_builder_x402": (
        "Genesis Builder Agent",
        "You are Genesis Builder Agent, an elite AI software engineer specialising in "
        "TypeScript, React, and modern full-stack development. You generate production-ready "
        "code, UI components, and architectural blueprints. Respond concisely and stay in "
        "character as a senior software architect.",
    ),
    "genesis_deploy_x402": (
        "Genesis Deploy Agent",
        "You are Genesis Deploy Agent, an expert in cloud infrastructure and CI/CD pipelines. "
        "You handle deployments to AWS, GCP, Azure, Render, and Netlify using best practices "
        "for reliability and security. Respond concisely and stay in character as a DevOps "
        "engineer.",
    ),
    "genesis_content_x402": (
        "Genesis Content Agent",
        "You are Genesis Content Agent, a professional AI content strategist and writer. "
        "You create compelling blog posts, whitepapers, and social media copy that ranks "
        "well on search engines and resonates with target audiences. Respond concisely and "
        "stay in character as a senior content director.",
    ),
    "genesis_email_x402": (
        "Genesis Email Agent",
        "You are Genesis Email Agent, an expert email marketing specialist. You design "
        "high-converting email sequences, drip campaigns, and transactional templates that "
        "drive engagement and revenue. Respond concisely and stay in character as a "
        "conversion-focused email strategist.",
    ),
    "genesis_commerce_x402": (
        "Genesis Commerce Agent",
        "You are Genesis Commerce Agent, an e-commerce growth specialist with expertise "
        "in Shopify, WooCommerce, and multi-channel retail. You optimise product listings, "
        "checkout flows, and marketplace integrations. Respond concisely and stay in "
        "character as a senior e-commerce consultant.",
    ),
    "genesis_qa_x402": (
        "Genesis QA Agent",
        "You are Genesis QA Agent, a meticulous quality assurance engineer. You design "
        "comprehensive test suites, identify edge cases, and ensure software reliability "
        "using modern testing frameworks like Jest, Pytest, and Playwright. Respond "
        "concisely and stay in character as a QA lead.",
    ),
    "genesis_support_x402": (
        "Genesis Support Agent",
        "You are Genesis Support Agent, a customer success specialist who designs "
        "knowledge bases, FAQ systems, and support playbooks. You help companies "
        "deliver exceptional customer experiences at scale. Respond concisely and "
        "stay in character as a head of customer success.",
    ),
    "genesis_finance_x402": (
        "Genesis Finance Agent",
        "You are Genesis Finance Agent, a financial analyst and strategist. You produce "
        "financial models, cash-flow projections, and strategic finance plans for "
        "startups and growth-stage companies. Respond concisely and stay in character "
        "as a CFO-level advisor.",
    ),
    "genesis_security_x402": (
        "Genesis Security Agent",
        "You are Genesis Security Agent, a cybersecurity expert specialising in "
        "vulnerability assessment, penetration testing, and OWASP Top 10 remediation. "
        "You deliver security audits and actionable hardening recommendations. Respond "
        "concisely and stay in character as a senior security engineer.",
    ),
    "genesis_billing_x402": (
        "Genesis Billing Agent",
        "You are Genesis Billing Agent, a revenue operations specialist. You design "
        "billing systems, automate invoice workflows, and optimise subscription revenue. "
        "Respond concisely and stay in character as a RevOps director.",
    ),
    "genesis_analyst_x402": (
        "Genesis Analyst Agent",
        "You are Genesis Analyst Agent, a business intelligence and data analytics expert. "
        "You transform raw data into strategic insights using statistical analysis and "
        "visualisation. Respond concisely and stay in character as a senior data analyst.",
    ),
    "genesis_marketing_x402": (
        "Genesis Marketing Agent",
        "You are Genesis Marketing Agent, a full-stack marketing strategist. You create "
        "multi-channel campaigns, growth funnels, and go-to-market strategies that drive "
        "measurable results. Respond concisely and stay in character as a CMO-level advisor.",
    ),
    "genesis_seo_x402": (
        "Genesis SEO Agent",
        "You are Genesis SEO Agent, an SEO specialist who delivers keyword research, "
        "technical audits, and 12-month content strategies. You optimise sites for "
        "top SERP rankings and organic growth. Respond concisely and stay in character "
        "as a senior SEO strategist.",
    ),
    "genesis_meta_x402": (
        "Genesis Meta Agent",
        "You are Genesis Meta Agent, the flagship AI orchestrator for SwarmSync. You "
        "coordinate specialised agents to deliver complete business solutions — from "
        "ideation through to deployment — in hours instead of weeks. Respond concisely "
        "and stay in character as an autonomous business-generation system.",
    ),
    "genesis_meta_agent": (
        "Genesis Meta Agent",
        "You are Genesis Meta Agent, the autonomous business-generation orchestrator. "
        "You coordinate Builder, Research, Deploy, Content, QA, and other specialist "
        "agents to build complete businesses from idea to launch. Respond concisely and "
        "stay in character.",
    ),
    # AP2-style agents
    "builder_agent": (
        "Builder Agent",
        "You are Builder Agent, a software construction specialist using modern TypeScript "
        "and React. You build features from specifications with clean, testable code. "
        "Respond concisely and stay in character as a staff engineer.",
    ),
    "builder_agent_enhanced": (
        "Builder Agent Enhanced",
        "You are Builder Agent Enhanced, an advanced software builder with sophisticated "
        "planning, multi-step implementation, and architectural reasoning. Respond concisely "
        "and stay in character as a principal engineer.",
    ),
    "deploy_agent": (
        "Deploy Agent",
        "You are Deploy Agent, a cloud deployment and infrastructure specialist. You "
        "manage CI/CD pipelines and multi-cloud deployments reliably. Respond concisely "
        "and stay in character as a platform engineer.",
    ),
    "qa_agent": (
        "QA Agent",
        "You are QA Agent, a quality assurance specialist who designs and executes test "
        "plans to ensure software correctness. Respond concisely and stay in character "
        "as a QA engineer.",
    ),
    "research_discovery_agent": (
        "Research Discovery Agent",
        "You are Research Discovery Agent, a research specialist who gathers, synthesises, "
        "and distils information to drive informed decisions. Respond concisely and stay "
        "in character as a research lead.",
    ),
    "spec_agent": (
        "Spec Agent",
        "You are Spec Agent, a technical writer and architect who creates precise product "
        "specifications and API documentation. Respond concisely and stay in character "
        "as a solutions architect.",
    ),
    "security_agent": (
        "Security Agent",
        "You are Security Agent, a cybersecurity specialist focused on threat modelling, "
        "code review, and compliance. Respond concisely and stay in character as a "
        "security engineer.",
    ),
    "maintenance_agent": (
        "Maintenance Agent",
        "You are Maintenance Agent, an operational reliability specialist who monitors, "
        "patches, and improves software systems. Respond concisely and stay in character "
        "as an SRE.",
    ),
    "seo_agent": (
        "SEO Agent",
        "You are SEO Agent, a search optimisation specialist delivering keyword strategy "
        "and technical SEO fixes. Respond concisely and stay in character as an SEO expert.",
    ),
    "content_agent": (
        "Content Agent",
        "You are Content Agent, a content creation specialist producing high-quality "
        "written assets for marketing and communications. Respond concisely and stay "
        "in character as a senior copywriter.",
    ),
    "marketing_agent": (
        "Marketing Agent",
        "You are Marketing Agent, a growth marketing specialist who designs campaigns "
        "and funnels. Respond concisely and stay in character as a growth marketer.",
    ),
    "support_agent": (
        "Support Agent",
        "You are Support Agent, a customer experience specialist who resolves issues "
        "and builds support systems. Respond concisely and stay in character as a "
        "customer success manager.",
    ),
    "analyst_agent": (
        "Analyst Agent",
        "You are Analyst Agent, a data and business analyst who surfaces insights from "
        "data. Respond concisely and stay in character as a business analyst.",
    ),
    "finance_agent": (
        "Finance Agent",
        "You are Finance Agent, a financial modelling and strategy specialist. Respond "
        "concisely and stay in character as a financial analyst.",
    ),
    "pricing_agent": (
        "Pricing Agent",
        "You are Pricing Agent, a pricing strategy specialist who optimises monetisation "
        "models. Respond concisely and stay in character as a pricing expert.",
    ),
    "email_agent": (
        "Email Agent",
        "You are Email Agent, an email marketing specialist creating campaigns that "
        "convert. Respond concisely and stay in character as an email strategist.",
    ),
    "billing_agent": (
        "Billing Agent",
        "You are Billing Agent, a revenue operations specialist managing billing workflows "
        "and subscription systems. Respond concisely and stay in character as a billing "
        "engineer.",
    ),
    "commerce_agent": (
        "Commerce Agent",
        "You are Commerce Agent, an e-commerce specialist optimising online stores and "
        "marketplaces. Respond concisely and stay in character as an e-commerce consultant.",
    ),
    "darwin_agent": (
        "Darwin Agent",
        "You are Darwin Agent, an evolutionary AI that applies genetic algorithms and "
        "adaptive learning to optimise solutions iteratively. Respond concisely and stay "
        "in character as a machine-learning researcher.",
    ),
    "domain_name_agent": (
        "Domain Name Agent",
        "You are Domain Name Agent, a brand naming specialist who generates creative, "
        "available domain names for new ventures. Respond concisely and stay in character "
        "as a brand strategist.",
    ),
    "legal_agent": (
        "Legal Agent",
        "You are Legal Agent, an AI legal assistant that drafts contracts, reviews "
        "agreements, and provides guidance on compliance. Respond concisely and stay "
        "in character as a legal counsel. Note: not a substitute for qualified legal advice.",
    ),
    "onboarding_agent": (
        "Onboarding Agent",
        "You are Onboarding Agent, a user experience specialist who designs smooth "
        "onboarding flows for products. Respond concisely and stay in character as a "
        "UX designer.",
    ),
    "reflection_agent": (
        "Reflection Agent",
        "You are Reflection Agent, a meta-cognitive AI that reviews, critiques, and "
        "improves the outputs of other agents. Respond concisely and stay in character "
        "as a quality reviewer.",
    ),
    "waltzrl_conversation_agent": (
        "WaltzRL Conversation Agent",
        "You are WaltzRL Conversation Agent, a reinforcement-learning-powered dialogue "
        "agent that learns from feedback to improve conversation quality. Respond "
        "concisely and stay in character.",
    ),
    "waltzrl_feedback_agent": (
        "WaltzRL Feedback Agent",
        "You are WaltzRL Feedback Agent, a reward modelling specialist that evaluates "
        "agent outputs and provides structured feedback for reinforcement learning training. "
        "For every evaluation you receive, produce a structured report with these sections:\n"
        "1. **Quality Score** (1-10) with a one-line justification\n"
        "2. **Strengths** – 2-3 specific things the output did well\n"
        "3. **Critical Issues** – ranked list of problems with severity (BLOCKING/HIGH/MEDIUM/LOW)\n"
        "4. **Suggested Fix** – concrete, actionable rewrite or correction for the top issue\n"
        "5. **Affected User Segment** – who is most impacted by the failure\n"
        "6. **RL Training Signal** – reward delta recommendation (positive/negative float, e.g. -0.4)\n"
        "Minimum response: 150 words. Never give vague feedback like 'could be improved' — "
        "name the exact flaw and prescribe the exact fix.",
    ),
    "se_darwin_agent": (
        "SE Darwin Agent",
        "You are SE Darwin Agent, a software-engineering evolutionary optimiser that "
        "applies Darwin-inspired improvement cycles to code and architecture. Respond "
        "concisely and stay in character.",
    ),
    "ring1t_reasoning_agent": (
        "Ring1T Reasoning Agent",
        "You are Ring1T Reasoning Agent, an advanced chain-of-thought reasoning specialist "
        "that decomposes complex problems into structured solution paths. Respond concisely "
        "and stay in character.",
    ),
    "business_idea_generator": (
        "Business Idea Generator",
        "You are Business Idea Generator, an innovative ideation agent that creates "
        "novel, market-validated business ideas. You analyse trends and opportunities "
        "to surface high-potential ventures. Respond concisely and stay in character.",
    ),
    # Marketplace agents
    "genesis-ai-vision-api": (
        "Genesis AI Vision API",
        "You are Genesis AI Vision API, an image analysis and computer vision agent "
        "that processes images, extracts information, and generates visual insights. "
        "Respond concisely and stay in character as a computer vision specialist.",
    ),
    "genesis-workflow-automator": (
        "Genesis Workflow Automator",
        "You are Genesis Workflow Automator, a process automation specialist that "
        "designs and implements business workflow automations. When a platform is "
        "not specified, default to n8n and return an importable workflow artifact "
        "instead of asking a follow-up question. Respond concisely.",
    ),
    "genesis-data-pipeline-agent": (
        "Genesis Data Pipeline Agent",
        "You are Genesis Data Pipeline Agent, a data engineering specialist that "
        "designs ETL pipelines and data architectures. Respond concisely.",
    ),
    "unit-test-generator": (
        "Unit Test Generator",
        "You are Unit Test Generator, an AI that writes comprehensive unit tests "
        "for any codebase. Respond concisely and stay in character.",
    ),
    "api-documentation-generator": (
        "API Documentation Generator",
        "You are API Documentation Generator, an AI that produces clear, complete "
        "API documentation from code. Respond concisely and stay in character.",
    ),
    "social-media-scheduler": (
        "Social Media Scheduler",
        "You are Social Media Scheduler, an AI that plans and optimises social media "
        "content calendars. Respond concisely and stay in character.",
    ),
    "web-scraper-pro": (
        "Web Scraper Pro",
        "You are Web Scraper Pro, an AI specialist in web data extraction and "
        "structuring. Respond concisely and stay in character.",
    ),
    "meeting-summarizer": (
        "Meeting Summarizer",
        "You are Meeting Summarizer, an AI that transforms meeting transcripts into "
        "structured, decision-ready summaries. Always produce output in this exact format:\n\n"
        "## Meeting Summary\n"
        "**Date/Context:** [extract from transcript or note 'not specified']\n\n"
        "### Decisions Made\n"
        "- [Decision 1 — one sentence, definitive]\n"
        "- [Decision 2]\n\n"
        "### Action Items\n"
        "| Owner | Action | Due Date |\n"
        "|-------|--------|----------|\n"
        "| [Name] | [Specific task] | [Date or 'TBD'] |\n\n"
        "### Key Discussion Points\n"
        "- [Important point discussed, not decided]\n\n"
        "### Next Steps\n"
        "- [What happens next, in order]\n\n"
        "If the input is not a meeting transcript, ask for one. "
        "Never output a flat paragraph — always use the table/section format above.",
    ),
    "expense-tracker": (
        "Expense Tracker",
        "You are Expense Tracker, an AI finance assistant that categorises expenses "
        "and generates spending reports. Respond concisely and stay in character.",
    ),
    "review-responder": (
        "Review Responder",
        "You are Review Responder, an AI that crafts professional, empathetic responses "
        "to customer reviews. Respond concisely and stay in character.",
    ),
    "onboarding-automation": (
        "Onboarding Automation",
        "You are Onboarding Automation, an AI specialist in designing automated "
        "employee and customer onboarding sequences. Respond concisely and stay in character.",
    ),
    "image-optimizer": (
        "Image Optimizer",
        "You are Image Optimizer, an AI that advises on image compression, format "
        "selection, and performance optimisation strategies. Respond concisely.",
    ),
    "backup-manager": (
        "Backup Manager",
        "You are Backup Manager, an AI that designs robust backup and disaster recovery "
        "strategies for data and infrastructure. Respond concisely.",
    ),
    # CHUNK_2_REGISTRY: reconcile the 3 bundles that existed on disk
    # (skill_bundles/genesis-{domain,maintenance,pricing}.json) but had no
    # catalogued slug. Text below is excerpted verbatim from each bundle's own
    # system_prompt, not invented.
    "genesis_domain": (
        "Genesis Domain Agent",
        "You are the Genesis Domain Agent. You help buyers find, register, and configure "
        "domain names for new businesses. You generate CREATIVE, memorable domain "
        "candidates (10-20 per request) — never obvious keyword-stuffed names like "
        "'bestplumber.com'. Think like a brand strategist: use portmanteaus, invented "
        "words, metaphors, unexpected word pairings, and cross-industry borrowing.",
    ),
    "genesis_maintenance": (
        "Genesis Maintenance Agent",
        "You are a system maintenance and operations specialist. Monitor system health, "
        "perform routine maintenance, optimize performance, manage backups, and ensure "
        "high availability. Track uptime, resource utilization, database health, and "
        "infrastructure costs. Implement proactive monitoring and automated remediation. "
        "Use UptimeRobot API for uptime monitoring.",
    ),
    "genesis_pricing": (
        "Genesis Pricing Agent",
        "You are the Genesis Pricing Agent. You run price-elasticity experiments, "
        "purchase market datasets, deploy pricing updates, and report on pricing "
        "performance for the buyer's business. Your charter is experimentation: identify "
        "the price points most likely to lift revenue, test them, measure them, and "
        "write back results.",
    ),
}

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class NegotiateRequest(BaseModel):
    event: str
    negotiation_id: str
    requester_agent_id: str
    requester_agent_name: str
    responder_agent_id: str
    requested_service: str
    budget: float
    requirements: dict = {}
    notes: Optional[str] = None
    category: Optional[str] = None
    callback_url: str


class NegotiateResponse(BaseModel):
    negotiation_id: str
    responder_agent_id: str
    status: str  # ACCEPTED, REJECTED, COUNTERED
    price: float
    notes: str
    estimatedDelivery: str


class RunRequest(BaseModel):
    prompt: Optional[str] = None
    # Workflows send structured JSON objects; accept dict/list (BUG-01 / HTTP 422).
    input: Optional[Any] = None
    task: Optional[Any] = None
    testContext: Optional[bool] = False
    mode: Optional[str] = None
    require_artifact: Optional[bool] = False
    # External escrow integration: when the marketplace owns the escrow,
    # it passes the escrow id here so the gateway skips its own
    # initiate / complete / release flow. The caller is then responsible
    # for settle/refund based on the response.
    escrow_id: Optional[str] = None
    # For async jobs (job_mode=async): URL the worker will POST results to
    # on DELIVERED / FAILED / EXPIRED so the marketplace can settle/refund.
    callback_url: Optional[str] = None
    # Explicit flag — useful for tests/direct callers that want to skip
    # internal escrow without passing an external id.
    skip_internal_escrow: Optional[bool] = False


class RunResponse(BaseModel):
    response: str
    agentSlug: Optional[str] = None
    agentName: Optional[str] = None


# ---------------------------------------------------------------------------
# LLM router helper
# ---------------------------------------------------------------------------

def _llm_api_key() -> str:
    """Read the LLM API key at call time. Prefers LLM_API_KEY (used when
    routing through SwarmSync). OpenRouter is not a default Genesis route; it
    is only allowed when GENESIS_ALLOW_OPENROUTER_FALLBACK=true. Per-call read
    lets Render env-var rotations take effect without a manual redeploy."""
    for name in ("LLM_API_KEY", "SWARMSYNC_ROUTING_API_KEY", "ROUTING_API_KEY"):
        val = (os.getenv(name) or "").strip()
        if val:
            return val
    if os.getenv("GENESIS_ALLOW_OPENROUTER_FALLBACK", "").lower() in {"1", "true", "yes"}:
        return (os.getenv("OPENROUTER_API_KEY") or "").strip()
    return ""


def _llm_api_url() -> str:
    """Read the LLM API endpoint at call time. Defaults to SwarmSync routing.
    OpenRouter is intentionally not the default Genesis route."""
    return os.getenv("LLM_API_URL", "https://api.swarmsync.ai/v1/chat/completions")


# Backward-compatibility alias. Existing callers of _openrouter_api_key()
# continue to work, but this now returns the configured LLM router key.
_openrouter_api_key = _llm_api_key


# Legacy persona path model. Bundle-backed Genesis agents use AgentRuntime's
# SwarmSync model default instead.
FREE_MODEL = os.getenv("LEGACY_PERSONA_MODEL", "minimax/minimax-m2.5")
PERSONA_ROUTER_MODEL = os.getenv("GENESIS_LLM_MODEL", "auto").strip()
PERSONA_PRIMARY_MODEL = PERSONA_ROUTER_MODEL
X402_STUB_MARKER = "x402 HTTP service"
ROUTER_FALLBACK_MODELS = [
    PERSONA_PRIMARY_MODEL,
    "openai/gpt-5-mini",
    "openai/gpt-5.1",
]


def _coerce_prompt_field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value).strip()


def _build_user_prompt(body: RunRequest) -> str:
    """Combine prompt, input, and task so role-specific tasks are not dropped."""
    prompt = _coerce_prompt_field(body.prompt)
    inp = _coerce_prompt_field(body.input)
    task_raw = body.task
    parts: list[str] = []
    if prompt:
        parts.append(prompt)
    if inp and inp not in parts:
        parts.append(inp)
    if task_raw is not None:
        if isinstance(task_raw, dict):
            task_text = _coerce_prompt_field(
                task_raw.get("description") or task_raw.get("text") or task_raw.get("task") or task_raw
            )
        else:
            task_text = _coerce_prompt_field(task_raw)
        if task_text and task_text not in parts:
            parts.append(f"Task:\n{task_text}")
    return "\n\n".join(parts).strip()


def _is_x402_stub_response(text: str) -> bool:
    return X402_STUB_MARKER in (text or "")


def _prefer_sync_bundle_run(body: RunRequest) -> bool:
    return bool(body.testContext) or (body.mode or "").strip() == "live_test"


def _router_result_payload(router_result: Any) -> tuple[str, dict[str, Any] | None]:
    if isinstance(router_result, dict):
        return (
            str(router_result.get("text") or ""),
            router_result.get("swarmsync") if isinstance(router_result.get("swarmsync"), dict) else None,
        )
    return str(router_result or ""), None


def _runtime_error_status(result: dict[str, Any]) -> int:
    """Map AgentRuntime failure payloads to honest HTTP statuses."""
    error = str(result.get("error", "agent_failure"))
    message = str(result.get("message", ""))
    combined = f"{error} {message}".lower()

    if error == "timeout":
        return 504
    if "429" in combined or "too many requests" in combined or "rate limit" in combined:
        return 429
    if "402" in combined or "credit" in combined or "payment required" in combined:
        return 402
    if error in {"success_criteria_failed"}:
        return 422
    if error in {"tool_not_allowed", "file_write_limit_exceeded", "token_budget_exceeded", "llm_call_limit_exceeded"}:
        return 429
    return 502


def _raise_for_runtime_failure(result: dict[str, Any], slug: str) -> None:
    """Raise when AgentRuntime returns an explicit failure payload.

    Older behavior wrapped these failures inside HTTP 200 `response` strings,
    which made live monitoring and the Genesis evaluator report false success
    at the transport layer.
    """
    if result.get("ok", True):
        return

    status_code = _runtime_error_status(result)
    raise HTTPException(
        status_code=status_code,
        detail={
            "error": result.get("error", "agent_failure"),
            "message": result.get("message", ""),
            "type": result.get("type"),
            "agentSlug": slug,
        },
    )

async def call_llm_router(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """Call SwarmSync /v1/chat/completions; return text + swarmsync routing metadata."""
    api_key = _llm_api_key()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="LLM API key not configured (set LLM_API_KEY)",
        )
    url = _llm_api_url()

    router_headers: dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://swarmsync.ai",
        "X-Title": "SwarmSync Agent Gateway",
    }
    gateway_secret = os.getenv("AGENT_GATEWAY_SECRET", "").strip()
    if gateway_secret:
        router_headers["X-Agent-Gateway-Secret"] = gateway_secret

    backoffs = [2.0]
    last_exc: HTTPException | None = None
    models = list(dict.fromkeys(m for m in ROUTER_FALLBACK_MODELS if m))

    for model_id in models:
        for attempt, _ in enumerate([()] * (len(backoffs) + 1)):
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    url,
                    headers=router_headers,
                    json={
                        "model": model_id,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "max_tokens": 2048,
                    },
                )

            if resp.status_code == 429:
                last_exc = HTTPException(
                    status_code=429,
                    detail="LLM router rate limit exceeded. Please retry shortly.",
                )
                if attempt < len(backoffs):
                    logger.warning(
                        "LLM router 429 model=%s attempt %d; sleeping %.1fs",
                        model_id,
                        attempt + 1,
                        backoffs[attempt],
                    )
                    await asyncio.sleep(backoffs[attempt])
                    continue
                break

            if not (200 <= resp.status_code < 300):
                detail_text = resp.text[:500]
                combined = detail_text.lower()
                if resp.status_code in (400, 402) or any(
                    x in combined for x in ("402", "credit", "balance", "quota", "payment")
                ):
                    logger.warning(
                        "LLM router %s for model=%s; trying next model",
                        resp.status_code,
                        model_id,
                    )
                    break
                raise HTTPException(
                    status_code=502,
                    detail=f"LLM router returned {resp.status_code}: {detail_text}",
                )

            data = resp.json()
            content = str(data.get("choices", [{}])[0].get("message", {}).get("content") or "")
            if not content.strip():
                last_exc = HTTPException(
                    status_code=502,
                    detail=f"LLM router returned empty content for model={model_id}",
                )
                if attempt < len(backoffs):
                    logger.warning(
                        "LLM router returned empty content for model=%s attempt %d; sleeping %.1fs",
                        model_id,
                        attempt + 1,
                        backoffs[attempt],
                    )
                    await asyncio.sleep(backoffs[attempt])
                    continue
                logger.warning("LLM router returned empty content for model=%s; trying next model", model_id)
                break

            swarmsync = data.get("swarmsync") if isinstance(data.get("swarmsync"), dict) else None
            return {
                "text": content,
                "swarmsync": swarmsync,
                "usage": data.get("usage"),
                "model": data.get("model"),
            }

    if last_exc:
        raise last_exc
    raise HTTPException(
        status_code=502,
        detail="LLM router failed for all fallback models (check LLM_API_KEY credits)",
    )


async def _run_loaded_agent(agent: Any, user_prompt: str, slug: str) -> Optional[str]:
    """
    Best-effort execution for dynamically loaded agents.

    Tries, in order:
    - agent.run(prompt)
    - agent.execute(prompt)
    - module-level run(prompt)
    - module-level execute(prompt)

    Returns the response text on success, or None to signal the caller to fall
    back to the SwarmSync router persona path.
    """
    import inspect
    import types

    candidates: list[tuple[Any, str]] = []

    # Instance methods on the loaded agent object
    for name in ("run", "execute"):
        if hasattr(agent, name):
            candidates.append((getattr(agent, name), f"{type(agent).__name__}.{name}"))

    # If the "agent" is actually a module, also look for module-level entrypoints
    if isinstance(agent, types.ModuleType):
        for name in ("run", "execute"):
            if hasattr(agent, name):
                candidates.append((getattr(agent, name), f"{agent.__name__}.{name}"))

    for func, label in candidates:
        if not callable(func):
            continue
        try:
            logger.info("Executing loaded agent via %s for slug=%s", label, slug)
            result = func(user_prompt)
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                continue
            if not isinstance(result, str):
                result = str(result)
            return result
        except Exception as exc:
            logger.error(
                "Agent entrypoint %s for slug=%s failed: %s",
                label,
                slug,
                exc,
                exc_info=True,
            )
            continue

    logger.warning(
        "Loaded agent for slug=%s has no usable 'run' or 'execute' entrypoint; falling back to persona",
        slug,
    )
    return None


# ---------------------------------------------------------------------------
# Verification system configuration
# ---------------------------------------------------------------------------

INTERNAL_SECRET = os.getenv("INTERNAL_SECRET", "")
SWARMSYNC_API_URL = os.getenv("SWARMSYNC_API_URL", "https://api.swarmsync.ai")
AGENT_GATEWAY_SECRET = os.getenv("AGENT_GATEWAY_SECRET", "")

if not AGENT_GATEWAY_SECRET:
    logger.warning("AGENT_GATEWAY_SECRET is not set — negotiate callbacks will be sent without authentication")

_GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY")
if not _GATEWAY_API_KEY:
    logger.warning("GATEWAY_API_KEY is not set — /agents/{slug}/run and /a2a are open to anonymous callers (dev mode)")

# In-memory job store: job_id -> VerifyJobStatus dict
# Persists only for the lifetime of the process (acceptable — Render restarts are rare,
# and NestJS will mark timed-out verifications via its cron task).
_verification_jobs: dict[str, dict[str, Any]] = {}
_arbitrage_verification_jobs: dict[str, dict[str, Any]] = {}


def _internal_secret() -> str:
    """Return the current internal callback secret from the environment."""
    return os.getenv("INTERNAL_SECRET", "")


def _require_internal_secret(x_internal_secret: str | None) -> None:
    """Raise 401 if the X-Internal-Secret header is missing or wrong."""
    expected_secret = _internal_secret()
    if not expected_secret:
        # If not configured, reject all calls — prevents open endpoints in production
        raise HTTPException(status_code=503, detail="INTERNAL_SECRET not configured on this service")
    if x_internal_secret != expected_secret:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Internal-Secret")


async def _run_and_callback(
    job_id: str,
    request: VerifyRequest,
) -> None:
    """
    Background task: run the Conduit verification, update the job store,
    then POST the result back to the NestJS callback endpoint.
    """
    verification_id = request.context.verification_id

    try:
        # Mark as running
        _verification_jobs[job_id]["status"] = "running"

        result: VerificationResult = await run_verification(
            session_id=job_id,
            spec=request.spec,
            context=request.context,
        )

        completed_at = datetime.now(timezone.utc).isoformat()
        _verification_jobs[job_id].update({
            "status": "completed",
            "result": result.model_dump(),
            "completed_at": completed_at,
        })

        callback_url = f"{SWARMSYNC_API_URL}/conduit/verifications/{verification_id}/callback"
        callback_body = {
            "passed": result.passed,
            "proofHash": result.proof_hash,
            "conduitSessionSig": result.conduit_session_sig,
            "extractedContent": result.extracted_content,
            "failureReason": result.failure_reason,
            "actionLog": [al.model_dump() for al in result.action_log],
            # v2 fields — present when Track 1 or Track 2 ran
            "evalResultHash": result.eval_result_hash,
            "verificationTrack": result.verification_track,
            "requestId": result.request_id,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                callback_url,
                json=callback_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Internal-Secret": _internal_secret(),
                },
            )
            if resp.status_code not in (200, 201):
                logger.error(
                    "Callback to NestJS failed: %d %s",
                    resp.status_code,
                    resp.text[:300],
                )

    except Exception as exc:
        logger.error("Verification job %s failed: %s", job_id, exc, exc_info=True)
        _verification_jobs[job_id].update({
            "status": "failed",
            "error": str(exc),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })

        # Best-effort error callback to NestJS
        try:
            verification_id = request.context.verification_id
            callback_url = f"{SWARMSYNC_API_URL}/conduit/verifications/{verification_id}/callback"
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    callback_url,
                    json={
                        "passed": False,
                        "failureReason": f"Gateway error: {exc}",
                        "actionLog": [],
                    },
                    headers={
                        "Content-Type": "application/json",
                        "X-Internal-Secret": _internal_secret(),
                    },
                )
        except Exception:
            pass  # Already in error handler — swallow


def _extract_arbitrage_spec(request: ArbitrageVerificationRequest) -> VerificationSpec:
    payload = request.request_payload or {}
    policy = request.policy or {}

    verification_url = (
        payload.get("verificationUrl")
        or payload.get("deliveryUrl")
        or payload.get("url")
        or policy.get("url")
    )

    if not isinstance(verification_url, str) or not verification_url.strip():
        raise ValueError("Arbitrage verification requires request_payload.verificationUrl or deliveryUrl")

    selector = payload.get("selector") or policy.get("selector")
    expected_content = payload.get("expectedContent") or policy.get("expectedContent")
    fingerprint_delta = payload.get("fingerprintDelta")
    if fingerprint_delta is None:
        fingerprint_delta = policy.get("fingerprintDelta", False)

    expected_hash = payload.get("expectedHash") or policy.get("expectedHash")
    rubric_json = payload.get("rubricJson") or policy.get("rubricJson")
    rubric_hash = payload.get("rubricHash") or policy.get("rubricHash")
    request_id = payload.get("requestId") or request.verification_run_id

    return VerificationSpec(
        url=verification_url.strip(),
        selector=selector if isinstance(selector, str) and selector.strip() else None,
        expectedContent=(
            expected_content if isinstance(expected_content, str) and expected_content.strip() else None
        ),
        fingerprintDelta=bool(fingerprint_delta),
        expected_hash=expected_hash if isinstance(expected_hash, str) and expected_hash.strip() else None,
        rubric_json=rubric_json if isinstance(rubric_json, dict) else None,
        rubric_hash=rubric_hash if isinstance(rubric_hash, str) and rubric_hash.strip() else None,
        request_id=request_id if isinstance(request_id, str) and request_id.strip() else None,
    )


async def _run_arbitrage_verification_and_callback(
    job_id: str,
    request: ArbitrageVerificationRequest,
) -> None:
    try:
        _arbitrage_verification_jobs[job_id]["status"] = "running"

        spec = _extract_arbitrage_spec(request)
        context = VerificationContext(
            marketplace="SwarmSync.AI",
            purpose="arbitrage_verification",
            escrow_ref=request.transaction_id,
            verification_id=request.verification_run_id,
            request_id=request.verification_run_id,
        )

        result: VerificationResult = await run_verification(
            session_id=job_id,
            spec=spec,
            context=context,
        )

        checks = [
            {
                "name": "conduit_verification",
                "passed": result.passed,
                "track": result.verification_track or 0,
            }
        ]
        if result.eval_result_hash:
            checks.append(
                {
                    "name": "artifact_hash",
                    "passed": result.passed if result.verification_track == 1 else None,
                    "hash": result.eval_result_hash,
                }
            )
        if result.rubric_result:
            checks.append(
                {
                    "name": "rubric_evaluation",
                    "passed": result.rubric_result.get("rubric_pass"),
                    "summary": result.rubric_result,
                }
            )

        evidence_urls = [spec.url]
        callback_body = ArbitrageVerificationResult(
            verificationRunId=request.verification_run_id,
            transactionId=request.transaction_id,
            status="PASSED" if result.passed else "FAILED",
            passed=result.passed,
            score=1.0 if result.passed else 0.0,
            failureReason=result.failure_reason,
            method=request.method,
            checks=checks,
            evidenceUrls=evidence_urls,
            resultPayload={
                "proofHash": result.proof_hash,
                "proofBundleRef": result.proof_bundle_ref,
                "conduitSessionSig": result.conduit_session_sig,
                "verificationTrack": result.verification_track,
                "evalResultHash": result.eval_result_hash,
                "rubricResult": result.rubric_result,
                "actionLog": [al.model_dump() for al in result.action_log],
                "extractedContent": result.extracted_content,
                "evidenceUrl": spec.url,
            },
        )

        _arbitrage_verification_jobs[job_id].update({
            "status": "completed",
            "result": callback_body.model_dump(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })

        callback_url = f"{SWARMSYNC_API_URL}/internal/arbitrage/verification-results"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                callback_url,
                json=callback_body.model_dump(),
                headers={
                    "Content-Type": "application/json",
                    "X-Internal-Secret": _internal_secret(),
                },
            )
            if resp.status_code not in (200, 201):
                logger.error(
                    "Arbitrage callback to NestJS failed: %d %s",
                    resp.status_code,
                    resp.text[:300],
                )

    except Exception as exc:
        logger.error("Arbitrage verification job %s failed: %s", job_id, exc, exc_info=True)
        failed_body = ArbitrageVerificationResult(
            verificationRunId=request.verification_run_id,
            transactionId=request.transaction_id,
            status="ERROR",
            passed=False,
            score=0.0,
            failureReason=str(exc),
            method=request.method,
            checks=[],
            evidenceUrls=[],
            resultPayload={"gatewayError": str(exc)},
        )
        _arbitrage_verification_jobs[job_id].update({
            "status": "failed",
            "result": failed_body.model_dump(),
            "error": str(exc),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })

        try:
            callback_url = f"{SWARMSYNC_API_URL}/internal/arbitrage/verification-results"
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    callback_url,
                    json=failed_body.model_dump(),
                    headers={
                        "Content-Type": "application/json",
                        "X-Internal-Secret": _internal_secret(),
                    },
                )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "swarmsync-agent-gateway"}


@app.get("/health/browser")
async def health_browser():
    """Report browser/Patchright/Conduit readiness with detailed diagnostics."""
    cache_dir = os.path.expanduser("~/.cache/ms-playwright")
    chromium_dirs: list[str] = []
    chromium_installed = False
    executable_path = None

    if os.path.isdir(cache_dir):
        for entry in os.scandir(cache_dir):
            if entry.is_dir() and "chromium" in entry.name.lower():
                chromium_dirs.append(entry.name)
                for root, _dirs, files in os.walk(entry.path):
                    for fname in files:
                        if fname in ("chrome", "chromium", "chrome.exe", "chromium.exe", "headless_shell"):
                            executable_path = os.path.join(root, fname)
                            chromium_installed = True
                            break
                    if chromium_installed:
                        break

    # conduit_browser package importability
    conduit_package_importable = False
    conduit_bridge_startable = False
    startup_error = None
    try:
        from conduit_browser import ConduitBridge  # noqa: F401
        conduit_package_importable = True
        try:
            ConduitBridge(session_id="health_check", budget_cents=0)
            conduit_bridge_startable = True
        except Exception as inst_err:
            startup_error = f"ConduitBridge() instantiation failed: {inst_err}"
    except ImportError as imp_err:
        startup_error = f"import conduit_browser failed: {imp_err}"

    # Memory check (Linux /proc/meminfo)
    memory_warning = None
    try:
        with open("/proc/meminfo") as _mf:
            for _line in _mf:
                if _line.startswith("MemAvailable:"):
                    available_kb = int(_line.split()[1])
                    available_mb = available_kb // 1024
                    if available_mb < 300:
                        memory_warning = (
                            f"Only {available_mb} MB RAM available — Chromium needs ~300+ MB; "
                            "OOM likely. Upgrade to Standard 2 GB plan."
                        )
                    break
    except Exception:
        pass

    render_instance_type = (
        os.getenv("RENDER_INSTANCE_TYPE")
        or os.getenv("RENDER_SERVICE_TYPE")
        or "unknown"
    )

    return {
        "chromium_installed": chromium_installed,
        "executable_path": executable_path,
        "chromium_dirs": chromium_dirs,
        "conduit_package_importable": conduit_package_importable,
        "conduit_bridge_startable": conduit_bridge_startable,
        "startup_error": startup_error,
        "memory_warning": memory_warning,
        "render_instance_type": render_instance_type,
        "smoke_test_url": "/health/conduit/smoke",
    }


@app.get("/health/conduit/smoke")
async def health_conduit_smoke():
    """Live Conduit smoke test: launch Chromium, navigate to example.com, read title, close."""
    import time
    start = time.monotonic()
    try:
        from patchright.async_api import async_playwright  # type: ignore[import]
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            page = await browser.new_page()
            await page.goto("https://example.com", wait_until="domcontentloaded", timeout=30000)
            title = await page.title()
            await browser.close()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "ok": True,
            "title": title,
            "elapsed_ms": elapsed_ms,
            "note": "Chromium launched successfully on this Render instance",
        }
    except MemoryError as mem_err:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "ok": False,
            "error": "MemoryError",
            "message": str(mem_err),
            "elapsed_ms": elapsed_ms,
            "recommendation": "Upgrade to Standard 2 GB plan — Chromium requires ~300+ MB free RAM",
        }
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        err_type = type(exc).__name__
        msg = str(exc)
        recommendation = None
        if "chromium" in msg.lower() or "executable" in msg.lower():
            recommendation = "Add 'python -m patchright install chromium' to Render build command"
        elif "memory" in msg.lower() or "oom" in msg.lower() or "killed" in msg.lower():
            recommendation = "Upgrade to Standard 2 GB plan — Chromium OOM on current instance"
        return {
            "ok": False,
            "error": err_type,
            "message": msg,
            "elapsed_ms": elapsed_ms,
            "recommendation": recommendation,
        }


@app.get("/health/worker")
async def health_worker():
    """Report Genesis job worker status."""
    try:
        from worker import _worker_state
        state = dict(_worker_state)
        # Convert timestamp to ISO string if present
        if state.get("last_tick_at") is not None:
            import datetime as _dt
            state["last_tick_at"] = _dt.datetime.fromtimestamp(
                state["last_tick_at"], tz=_dt.timezone.utc
            ).isoformat()
    except Exception:
        state = {"enabled": False, "error": "worker_module_unavailable"}

    queue_depth = 0
    stale_count = 0
    if _JOB_STORE_OK:
        try:
            from job_store import _conn
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM genesis_jobs WHERE status = 'QUEUED'")
                row = cur.fetchone()
                queue_depth = int(row["n"]) if row else 0
                cur.execute(
                    "SELECT COUNT(*) AS n FROM genesis_jobs WHERE status = 'RUNNING' "
                    "AND (\"lastHeartbeatAt\" IS NULL OR \"lastHeartbeatAt\" < NOW() - INTERVAL '5 minutes')"
                )
                row = cur.fetchone()
                stale_count = int(row["n"]) if row else 0
        except Exception as e:
            logger.warning("health_worker queue query failed: %s", e)

    return {
        **state,
        "queue_depth": queue_depth,
        "stale_job_count": stale_count,
        "worker_mode": os.getenv("WORKER_MODE", "trigger_dev"),
        "last_error": state.get("last_error"),
        "commit": os.getenv("RENDER_GIT_COMMIT") or os.getenv("GENESIS_COMMIT"),
    }


@app.get("/health/sandbox")
async def health_sandbox():
    """Report the active shell sandbox isolation tier (Phase 2).

    isolation="bwrap" means real kernel isolation (filesystem + network
    namespaces). isolation="process" means the hardened process sandbox with
    static escape guards (no kernel FS namespace — bwrap not installed).
    """
    try:
        from runtime.sandbox_manager import isolation_mode, _bwrap_works, _ALLOW_NETWORK
        return {
            "ok": True,
            "isolation": isolation_mode(),
            "bwrap_available": _bwrap_works(),
            "network_allowed": _ALLOW_NETWORK,
            "note": (
                "bwrap = real kernel isolation; process = hardened process "
                "sandbox + static guards (install bubblewrap / Docker deploy for "
                "full filesystem namespace isolation)."
            ),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/agents/jobs/{job_id}/artifacts", dependencies=[Depends(verify_gateway_key)])
async def get_job_artifacts(job_id: str):
    """List artifacts for a completed job.

    Prefers the durable genesis_artifacts metadata (sha256 / size / mime /
    signed_url) recorded at upload time; refreshes signed URLs and falls back to
    a live storage listing if no durable rows exist.
    """
    from artifact_store import list_artifacts, get_signed_url

    # Phase 4: durable metadata path (includes integrity checksums).
    try:
        import durable_store
        rows = durable_store.artifacts_by_job(job_id)
    except Exception:
        rows = []
    if rows:
        artifacts = []
        for r in rows:
            name = r.get("path") or r.get("filename")
            fresh = get_signed_url(job_id=job_id, name=name)
            artifacts.append({
                "name": name,
                "filename": r.get("filename"),
                "size_bytes": int(r["sizeBytes"]) if r.get("sizeBytes") is not None else None,
                "sha256": r.get("sha256"),
                "mime_type": r.get("mimeType"),
                "backend": r.get("storageBackend"),
                "uri": r.get("uri"),
                "signed_url": fresh.get("signed_url") or r.get("signedUrl"),
                "expires_in_seconds": fresh.get("expires_in_seconds"),
                "non_durable": r.get("storageBackend") == "local",
                "created_at": r["createdAt"].isoformat() if r.get("createdAt") and hasattr(r["createdAt"], "isoformat") else r.get("createdAt"),
            })
        return {"job_id": job_id, "backend": rows[0].get("storageBackend"),
                "artifacts": artifacts, "count": len(artifacts), "source": "durable"}

    result = list_artifacts(job_id=job_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "artifacts_not_found"))

    items = result.get("items", [])
    enriched = []
    for item in items:
        name = item.get("name", "")
        url_result = get_signed_url(job_id=job_id, name=name)
        enriched.append({
            "name": name,
            "size_bytes": item.get("size", 0),
            "signed_url": url_result.get("signed_url"),
            "expires_in_seconds": url_result.get("expires_in_seconds"),
            "backend": result.get("backend"),
            "non_durable": result.get("backend") == "local",
        })

    return {
        "job_id": job_id,
        "backend": result.get("backend"),
        "artifacts": enriched,
        "count": len(enriched),
    }


@app.get("/agents/{slug}/capabilities")
async def agent_capabilities(slug: str):
    """Return detailed capability and status metadata for an agent."""
    from capability_cards import card_for
    from tools import get_tool, _TOOL_SCHEMAS

    try:
        from bundle_loader import load_bundle
        bundle = load_bundle(slug)
    except Exception:
        bundle = None

    if bundle is None:
        raise HTTPException(status_code=404, detail="agent not found")

    tools_advertised = bundle.get("tools_advertised", [])
    tool_verification = {}
    for t in tools_advertised:
        registered = get_tool(t) is not None
        has_schema = t in _TOOL_SCHEMAS
        tool_verification[t] = {"registered": registered, "has_schema": has_schema}

    all_verified = all(v["registered"] for v in tool_verification.values())

    runtime_level = bundle.get("runtime_level", "skill_bundle")
    artifact_support = "file_write" in tools_advertised
    browser_required = "conduit" in tools_advertised or bundle.get("browser_required", False)

    card = card_for(slug)

    return {
        "slug": slug,
        "name": bundle.get("name"),
        "runtime_level": runtime_level,
        "tools_verified": all_verified,
        "tool_verification": tool_verification,
        "artifact_support": artifact_support,
        "browser_required": browser_required,
        "job_mode": bundle.get("job_mode", "sync"),
        "is_orchestrator": bundle.get("is_orchestrator", False),
        "eval_pass_rate": bundle.get("eval_pass_rate"),
        "last_verified_at": bundle.get("last_verified_at"),
        "pricing": card.get("pricing") if card else None,
        "reputation": card.get("reputation") if card else None,
    }


@app.get("/agents")
async def list_agents():
    return {
        "agents": [
            {"slug": slug, "name": name}
            for slug, (name, _) in AGENT_PERSONAS.items()
        ]
    }


@app.post("/agents/{slug}/run", response_model=RunResponse, dependencies=[Depends(verify_gateway_key)])
async def run_agent(slug: str, body: RunRequest):
    persona = AGENT_PERSONAS.get(slug)

    if persona:
        display_name, system_prompt = persona
    else:
        # Graceful fallback for unknown slugs
        display_name = slug.replace("_", " ").replace("-", " ").title()
        system_prompt = (
            f"You are {display_name}, a specialised AI agent on the SwarmSync marketplace. "
            "Respond helpfully and concisely in character."
        )

    user_prompt = _build_user_prompt(body)
    bundle_slug = slug
    try:
        from bundle_loader import resolve_bundle_slug

        bundle_slug = resolve_bundle_slug(slug)
    except Exception:
        bundle_slug = slug

    # External-escrow mode: when the marketplace (or any external coordinator)
    # is the escrow owner, the gateway must skip its OWN escrow init / complete /
    # release path. The caller settles or refunds via swarmsync-api directly.
    external_escrow_id = body.escrow_id
    skip_internal_escrow = bool(external_escrow_id) or bool(body.skip_internal_escrow)
    if external_escrow_id:
        logger.info(
            "run_agent slug=%s external escrow_id=%s — skipping internal escrow logic",
            slug,
            external_escrow_id,
        )

    logger.info(
        "run_agent slug=%s testContext=%s prompt_len=%d skip_internal_escrow=%s",
        slug,
        body.testContext,
        len(user_prompt),
        skip_internal_escrow,
    )

    # ------------------------------------------------------------------
    # 0) Phase 2 — consolidated agent runtime backed by skill bundles
    # ------------------------------------------------------------------
    # If a skill bundle exists for this slug, route through the multi-turn
    # AgentRuntime (system prompt + tool whitelist from the bundle). This
    # supersedes the legacy persona path for any slug that has a bundle.
    if _RUNTIME_IMPORT_OK and load_bundle is not None:
        try:
            bundle = load_bundle(bundle_slug)
        except Exception as exc:  # pragma: no cover - ultra-defensive
            logger.error("load_bundle failed for slug=%s: %s", slug, exc, exc_info=True)
            bundle = None

        if bundle is not None:
            bundle_name = bundle.get("name", display_name)

            # Live-test / testContext probes should not spin AgentRuntime for any
            # agent — doing so triggers ConduitBridge (Patchright browser) startup
            # which takes 35-50s on Render free tier, exceeding the 30s proxy
            # timeout. Route all live_test / testContext calls through the fast
            # persona LLM path instead of the full AgentRuntime.
            if _prefer_sync_bundle_run(body):
                logger.info(
                    "run_agent slug=%s live_test/testContext — bypassing AgentRuntime, using persona router",
                    slug,
                )
                display_name = str(bundle_name)
                system_prompt = str(bundle.get("system_prompt") or system_prompt)
                bundle = None

        if bundle is not None:
            # Phase 4 — async / long-running orchestrators and conduit-heavy
            # agents (Builder, Deploy, QA, Research, Meta) get persisted via
            # job_store.create_job and a job_id is returned immediately. The
            # worker picks it up, keeping Render's proxy out of the browser
            # startup path.
            if bundle.get("job_mode") == "async" and not _prefer_sync_bundle_run(body):
                if not _JOB_STORE_OK or create_job is None:
                    raise HTTPException(status_code=503, detail="job_store_unavailable")

                async_params = body.task if isinstance(body.task, dict) else {}
                async_prompt = user_prompt or (
                    json.dumps(body.task) if isinstance(body.task, dict) else ""
                )

                # Phase 6 — if buyer + price are supplied, escrow funds before
                # we hand the job to the worker. Agent wallet comes from the
                # bundle (`wallet_id`). Free-tier (Cato) callers omit these.
                price_tier_cents = async_params.get("price_tier_cents") or bundle.get("price_tier_default_cents")
                buyer_wallet_id = async_params.get("buyer_wallet_id")
                agent_wallet_id = bundle.get("wallet_id")

                escrow_id_async: str | None = None
                # If the caller passed an external escrow id, use it as the
                # job's escrow reference and skip internal escrow init.
                # We still persist it in genesis_jobs.escrowId so the worker
                # can echo it back in the callback payload.
                if external_escrow_id:
                    escrow_id_async = external_escrow_id
                # ESCROW BLOCKER 1/11 — escrow_client.initiate_escrow.
                # PERMANENTLY_PROHIBITED (contract 3.8 / 6.1 item 16). Unreachable
                # unless GENESIS_DEPLOYMENT_PROFILE=swarmsync-marketplace, which
                # leaves _ESCROW_OK False. Quarantined rather than deleted: this
                # is SwarmSync marketplace behaviour that Genesis does not own.
                elif (
                    _ESCROW_OK
                    and not skip_internal_escrow
                    and price_tier_cents
                    and buyer_wallet_id
                    and agent_wallet_id
                ):
                    init = await initiate_escrow(
                        source_wallet_id=buyer_wallet_id,
                        destination_wallet_id=agent_wallet_id,
                        amount_cents=int(price_tier_cents),
                        memo=f"Genesis {slug}",
                        metadata={"slug": slug},
                    )
                    if not init.get("ok"):
                        raise HTTPException(
                            status_code=402,
                            detail=f"escrow_initiate_failed: {init.get('error')}",
                        )
                    escrow_id_async = init.get("escrow_id")

                try:
                    if body.callback_url:
                        _assert_not_ssrf(body.callback_url)
                    job = create_job(
                        agent_slug=slug,
                        prompt=async_prompt,
                        params=async_params,
                        price_tier_cents=int(price_tier_cents) if price_tier_cents else None,
                        buyer_wallet_id=buyer_wallet_id,
                        escrow_id=escrow_id_async,
                        webhook_url=body.callback_url,
                    )
                except Exception as exc:
                    # If WE reserved the escrow (not the marketplace) and we
                    # failed to enqueue, refund. When external_escrow_id is set,
                    # the marketplace owns the refund decision.
                    # ESCROW BLOCKER 2/11 — escrow_client.release_escrow (refund on
                    # enqueue failure). PERMANENTLY_PROHIBITED (6.1 item 18): a
                    # refund is still automation moving money. Unreachable while
                    # _ESCROW_OK is False. Quarantined, not deleted.
                    if escrow_id_async and _ESCROW_OK and not external_escrow_id:
                        try:
                            await release_escrow(
                                escrow_id=escrow_id_async,
                                reason="job_create_failed",
                            )
                        except Exception:
                            logger.exception("escrow release after job_create_failed raised")
                    raise HTTPException(status_code=500, detail=f"job_create_failed: {exc}")

                queued_payload = {
                    "status": job.get("status", "QUEUED"),
                    "slug": bundle.get("slug", bundle_slug),
                    "job_id": job["id"],
                    "poll_url": f"/agents/jobs/{job['id']}",
                    "idempotent_hit": job.get("idempotent_hit", False),
                }
                if escrow_id_async:
                    queued_payload["escrow_id"] = escrow_id_async
                if external_escrow_id:
                    queued_payload["external_escrow_id"] = external_escrow_id
                return RunResponse(
                    response=json.dumps(queued_payload),
                    agentSlug=slug,
                    agentName=bundle_name,
                )

            runtime = _get_runtime()
            if runtime is not None:
                params = {}
                if isinstance(body.task, dict):
                    params = body.task

                # Phase 6 — escrow buyer funds before sync execution if a
                # buyer wallet + price are supplied AND the bundle declares
                # an agent wallet. Free-tier callers (Cato) skip this.
                sync_price_cents = params.get("price_tier_cents") if isinstance(params, dict) else None
                sync_buyer_wallet = params.get("buyer_wallet_id") if isinstance(params, dict) else None
                sync_agent_wallet = bundle.get("wallet_id")

                escrow_id_sync: str | None = None
                # External-escrow mode: the marketplace already initiated the
                # escrow. We do NOT call initiate / complete / release ourselves.
                # The caller settles or refunds based on the response body.
                # ESCROW BLOCKER 3/11 — escrow_client.initiate_escrow (sync path).
                # PERMANENTLY_PROHIBITED (6.1 item 16). Unreachable while
                # _ESCROW_OK is False. Quarantined, not deleted.
                if (
                    _ESCROW_OK
                    and not skip_internal_escrow
                    and sync_price_cents
                    and sync_buyer_wallet
                    and sync_agent_wallet
                ):
                    init_sync = await initiate_escrow(
                        source_wallet_id=sync_buyer_wallet,
                        destination_wallet_id=sync_agent_wallet,
                        amount_cents=int(sync_price_cents),
                        memo=f"Genesis {slug}",
                        metadata={"slug": slug},
                    )
                    if not init_sync.get("ok"):
                        raise HTTPException(
                            status_code=402,
                            detail=f"escrow_initiate_failed: {init_sync.get('error')}",
                        )
                    escrow_id_sync = init_sync.get("escrow_id")

                try:
                    try:
                        result = await runtime.execute_agent(bundle_slug, user_prompt, params)
                    except Exception as inner_exc:
                        # ESCROW BLOCKER 4/11 — escrow_client.release_escrow
                        # (refund on runtime exception). PERMANENTLY_PROHIBITED
                        # (6.1 item 18). Unreachable while _ESCROW_OK is False.
                        if escrow_id_sync and _ESCROW_OK and not external_escrow_id:
                            try:
                                await release_escrow(
                                    escrow_id=escrow_id_sync,
                                    reason=f"runtime_exception:{type(inner_exc).__name__}",
                                )
                            except Exception:
                                logger.exception("escrow release after runtime raise failed")
                        raise

                    # Escrow finalization after runtime returns — only when WE
                    # own the escrow. When external_escrow_id is set, the
                    # marketplace API performs settle/refund itself.
                    # ESCROW BLOCKERS 5/11, 6/11 and 7/11 —
                    #   escrow_client.complete_escrow  (6.1 item 17) settles funds
                    #   escrow_client.calculate_split  (6.1 item 19) constructs the
                    #     disbursement amount; also truncates with
                    #     int(total_cents * pct), losing a minor unit on every
                    #     split where the fee is not a whole number of cents
                    #   escrow_client.release_escrow   (6.1 item 18) refunds
                    # All PERMANENTLY_PROHIBITED and unreachable while _ESCROW_OK
                    # is False. Quarantined, not deleted: settle/refund is
                    # SwarmSync marketplace behaviour Genesis does not own.
                    if escrow_id_sync and _ESCROW_OK and isinstance(result, dict):
                        if result.get("ok"):
                            comp = await complete_escrow(
                                escrow_id=escrow_id_sync,
                                status="SETTLED",
                            )
                            result["escrow"] = {
                                "escrow_id": escrow_id_sync,
                                "status": "SETTLED",
                                "split": calculate_split(int(sync_price_cents)),
                                "settle_response_ok": comp.get("ok", False),
                            }
                        else:
                            rel = await release_escrow(
                                escrow_id=escrow_id_sync,
                                reason=str(result.get("error", "agent_failure")),
                            )
                            result["escrow"] = {
                                "escrow_id": escrow_id_sync,
                                "status": "REFUNDED",
                                "release_response_ok": rel.get("ok", False),
                            }

                    # Echo the external escrow id back so the marketplace can
                    # correlate the response with its own escrow record.
                    if external_escrow_id and isinstance(result, dict):
                        result["external_escrow_id"] = external_escrow_id

                    if isinstance(result, dict):
                        _raise_for_runtime_failure(result, slug)

                    return RunResponse(
                        response=json.dumps(result),
                        agentSlug=slug,
                        agentName=bundle_name,
                    )
                except HTTPException:
                    raise
                except Exception as exc:  # pragma: no cover - ultra-defensive
                    logger.error(
                        "AgentRuntime.execute_agent failed for slug=%s: %s; falling back to legacy path",
                        slug,
                        exc,
                        exc_info=True,
                    )

    # ------------------------------------------------------------------
    # 1) Fallback: router persona. Agent runs must stay on SwarmSync Routing.
    # ------------------------------------------------------------------
    router_result = await call_llm_router(system_prompt, user_prompt)

    response_text, swarmsync_meta = _router_result_payload(router_result)
    payload: dict[str, Any] = {"ok": True, "slug": bundle_slug, "response": response_text}
    if isinstance(router_result, dict):
        if router_result.get("usage") is not None:
            payload["usage"] = router_result.get("usage")
        if router_result.get("model") is not None:
            payload["model"] = router_result.get("model")
    if swarmsync_meta:
        payload["swarmsync"] = swarmsync_meta
        routed = swarmsync_meta.get("routed_model") or ""
        payload["routing"] = {
            "model": routed,
            "provider": routed.split("/")[0] if "/" in routed else routed,
            "tier": swarmsync_meta.get("tier"),
            "routing_reason": swarmsync_meta.get("routing_reason"),
            "estimated_cost": swarmsync_meta.get("estimated_cost"),
        }

    return RunResponse(
        response=json.dumps(payload),
        agentSlug=slug,
        agentName=display_name,
    )


# ============================================================================
# Phase 8 — async job state machine endpoints
# ============================================================================
# `/agents/{slug}/run` remains the synchronous path. The endpoints below let
# callers submit a durable async job (inserted into Postgres `genesis_jobs`,
# picked up by worker.py) and poll for status. Subsequent phases (4 async meta,
# 6 escrow, 11 disputes) attach state transitions on top of this layer.
# ============================================================================

@app.post("/agents/{slug}/jobs", dependencies=[Depends(verify_gateway_key)])
async def submit_job(slug: str, body: dict):
    """Submit an async job. Returns job_id immediately; client polls /agents/jobs/{job_id}.

    External-escrow integration: when `escrow_id` is in the body, the gateway
    persists it on the job and skips its own escrow initiate. The worker
    will fire `webhook_url` on DELIVERED / FAILED so the marketplace can
    settle or refund the marketplace-owned escrow.
    """
    if not _JOB_STORE_OK or create_job is None:
        raise HTTPException(status_code=503, detail="job_store unavailable")

    prompt = body.get("prompt", "")
    params = body.get("params", {})
    idempotency_key = body.get("idempotency_key")
    webhook_url = body.get("webhook_url") or body.get("callback_url")
    if webhook_url:
        _assert_not_ssrf(webhook_url)
    price_tier_cents = body.get("price_tier_cents")
    external_escrow_id = body.get("escrow_id")

    # buyer_client_id from a request header or body
    buyer_client_id = body.get("client_id")

    try:
        job = create_job(
            agent_slug=slug,
            prompt=prompt,
            params=params,
            price_tier_cents=price_tier_cents,
            idempotency_key=idempotency_key,
            webhook_url=webhook_url,
            buyer_client_id=buyer_client_id,
            escrow_id=external_escrow_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"job_create_failed: {e}")

    payload = {
        "job_id": job["id"],
        "status": job["status"],
        "poll_url": f"/agents/jobs/{job['id']}",
        "idempotent_hit": job.get("idempotent_hit", False),
    }
    if external_escrow_id:
        payload["external_escrow_id"] = external_escrow_id

    if not job.get("idempotent_hit"):
        try:
            from trigger_dispatch import dispatch_genesis_job

            dispatch_genesis_job(job["id"])
        except Exception:
            logger.exception("trigger dispatch failed for job %s", job.get("id"))

    return payload


@app.get("/agents/jobs/{job_id}", dependencies=[Depends(verify_gateway_key)])
async def get_job_status(job_id: str):
    """Poll a job's status + result."""
    if not _JOB_STORE_OK or get_job is None:
        raise HTTPException(status_code=503, detail="job_store unavailable")

    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    # Serialize datetimes
    for k in ("createdAt", "updatedAt", "startedAt", "completedAt", "lastHeartbeatAt"):
        if job.get(k):
            job[k] = job[k].isoformat() if hasattr(job[k], "isoformat") else str(job[k])
    return job


@app.get("/agents/jobs/{job_id}/events", dependencies=[Depends(verify_gateway_key)])
async def get_job_events(job_id: str):
    """Return persisted runtime events for a job (Phase 4 observability).

    Events are written to /tmp/jobs/{job_id}/logs/events.jsonl during execution.
    Returns [] if the job has no events yet (job not started or events file missing).
    """
    try:
        from runtime.observability import get_events
        events = get_events(job_id)
        return {"job_id": job_id, "events": events, "count": len(events)}
    except Exception as exc:
        logger.exception("get_job_events failed job=%s", job_id)
        raise HTTPException(status_code=500, detail=f"events_read_failed: {exc}")


@app.get("/agents/jobs/{job_id}/sandbox", dependencies=[Depends(verify_gateway_key)])
async def get_job_sandbox(job_id: str):
    """Return sandbox lifecycle state + isolation tier for a job (Phase 2/6).

    States: CREATED → ACTIVE → FINALIZING → UPLOADED → CLEANED → FAILED.
    Includes the active isolation tier ("bwrap" for real kernel isolation,
    "process" for the hardened process sandbox). 404 if never registered.
    """
    try:
        from runtime.workspace_manager import get_workspace
        from runtime.sandbox_manager import sandbox_status
        ws = get_workspace(job_id)
        if ws is None:
            raise HTTPException(
                status_code=404,
                detail=f"No sandbox registered for job_id={job_id!r}. "
                "Job may not exist or predates hardening.",
            )
        return sandbox_status(job_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("get_job_sandbox failed job=%s", job_id)
        raise HTTPException(status_code=500, detail=f"sandbox_read_failed: {exc}")


@app.post("/agents/jobs/{job_id}/sandbox", dependencies=[Depends(verify_gateway_key)])
async def create_job_sandbox(job_id: str, body: dict | None = None):
    """Explicitly provision a sandbox for a job (Phase 2 lifecycle)."""
    try:
        from runtime.sandbox_manager import create_sandbox
        session_id = (body or {}).get("session_id")
        return create_sandbox(job_id, session_id)
    except Exception as exc:
        logger.exception("create_job_sandbox failed job=%s", job_id)
        raise HTTPException(status_code=500, detail=f"sandbox_create_failed: {exc}")


@app.post("/agents/jobs/{job_id}/sandbox/destroy", dependencies=[Depends(verify_gateway_key)])
async def destroy_job_sandbox(job_id: str, body: dict | None = None):
    """Tear down a job sandbox (Phase 2 lifecycle).

    Body: {"cleanup_policy": "retain_debug" | "purge"}. Default retain_debug.
    """
    try:
        from runtime.sandbox_manager import destroy_sandbox
        policy = (body or {}).get("cleanup_policy", "retain_debug")
        result = destroy_sandbox(job_id, cleanup_policy=policy)
        if not result.get("ok") and result.get("error") == "sandbox_not_found":
            raise HTTPException(status_code=404, detail=result)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("destroy_job_sandbox failed job=%s", job_id)
        raise HTTPException(status_code=500, detail=f"sandbox_destroy_failed: {exc}")


def _serialize_row(row: dict | None) -> dict | None:
    """ISO-serialize any datetime values in a DB row dict."""
    if not row:
        return row
    out = dict(row)
    for k, v in list(out.items()):
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
    return out


@app.get("/agents/sessions/{session_id}", dependencies=[Depends(verify_gateway_key)])
async def get_session(session_id: str):
    """Return a durable agent session record + its child delegations (Phase 3/6).

    Survives service restart — read from Postgres (genesis_agent_sessions),
    not /tmp. 404 if the session was never persisted.
    """
    try:
        import durable_store
        session = durable_store.session_get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
        job_id = session.get("jobId")
        rels = durable_store.relationships_by_parent(job_id) if job_id else []
        children = [
            r for r in rels if r.get("parentSessionId") in (session_id, None)
        ]
        return {
            "session": _serialize_row(session),
            "children": [_serialize_row(r) for r in children],
            "child_count": len(children),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("get_session failed session=%s", session_id)
        raise HTTPException(status_code=500, detail=f"session_read_failed: {exc}")


@app.get("/agents/jobs/{job_id}/trace", dependencies=[Depends(verify_gateway_key)])
async def get_job_trace(job_id: str):
    """Return the full parent→child trace tree for a job (Phase 6).

    Aggregates the durable job record, its session(s), lifecycle events, and
    every child job/session spawned via genesis_call. Proves real delegation
    linkage that survives restart.
    """
    try:
        import durable_store
        job = None
        if _JOB_STORE_OK and get_job is not None:
            try:
                job = get_job(job_id)
            except Exception:
                job = None
        sessions = durable_store.sessions_by_job(job_id)
        rels = durable_store.relationships_by_parent(job_id)
        try:
            from runtime.observability import get_events
            events = get_events(job_id)
        except Exception:
            events = []

        children = []
        for r in rels:
            child_job_id = r.get("childJobId")
            child_job = None
            if child_job_id and _JOB_STORE_OK and get_job is not None:
                try:
                    child_job = get_job(child_job_id)
                except Exception:
                    child_job = None
            children.append({
                "relationship": _serialize_row(r),
                "child_job": _serialize_row(child_job),
                "child_sessions": [
                    _serialize_row(s) for s in durable_store.sessions_by_job(child_job_id)
                ] if child_job_id else [],
            })

        if job is None and not sessions and not rels and not events:
            raise HTTPException(status_code=404, detail=f"no trace found for job: {job_id}")

        return {
            "job_id": job_id,
            "job": _serialize_row(job),
            "sessions": [_serialize_row(s) for s in sessions],
            "events": events,
            "event_count": len(events),
            "children": children,
            "child_count": len(children),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("get_job_trace failed job=%s", job_id)
        raise HTTPException(status_code=500, detail=f"trace_read_failed: {exc}")


@app.post("/internal/genesis-worker/tick")
async def genesis_worker_tick(
    body: dict | None = None,
    x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
):
    """Run one Genesis job-worker poll cycle (legacy fallback)."""
    _require_internal_secret(x_internal_secret)

    if not _JOB_STORE_OK:
        raise HTTPException(status_code=503, detail="job_store unavailable")

    limit = 3
    if body and body.get("limit") is not None:
        try:
            limit = max(0, min(10, int(body["limit"])))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="limit must be an integer 0-10")

    try:
        from worker import run_tick
        import asyncio

        # Fire-and-forget: launch in background so client doesn't timeout waiting
        # for long-running jobs (genesis-meta can take 60-120s+).
        # Store task in _background_tasks so asyncio doesn't GC it before completion.
        _tick_limit = limit

        async def _tick_logged():
            try:
                result = await run_tick(limit=_tick_limit, expire_stale=True)
                logger.info("genesis_worker_tick_complete result=%s", result)
            except Exception:
                logger.exception("genesis_worker_tick_background_error limit=%d", _tick_limit)

        task = asyncio.create_task(_tick_logged())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return {"ok": True, "claimed": -1, "processed": -1, "job_ids": [], "dispatched": True}
    except Exception as e:
        logger.exception("genesis_worker_tick failed")
        raise HTTPException(status_code=500, detail=f"genesis_worker_tick_failed: {e}")


@app.post("/internal/genesis-worker/jobs/{job_id}/execute")
async def genesis_worker_execute_job(
    job_id: str,
    x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
):
    """Claim and execute one QUEUED genesis job (Trigger.dev genesis-job-process)."""
    _require_internal_secret(x_internal_secret)

    if not _JOB_STORE_OK:
        raise HTTPException(status_code=503, detail="job_store unavailable")

    try:
        from worker import execute_job_by_id

        result = await execute_job_by_id(job_id)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("genesis_worker_execute_job failed job=%s", job_id)
        raise HTTPException(status_code=500, detail=f"genesis_worker_execute_failed: {e}")


# ============================================================================
# Phase 11 - dispute filing, agent reputation, admin queue
# ============================================================================

@app.post("/jobs/{job_id}/dispute", dependencies=[Depends(verify_gateway_key)])
async def file_dispute(job_id: str, body: dict):
    """Buyer files a dispute. Marks the job DISPUTED and records evidence.

    Escrow is not auto-released; admin review (via /admin/disputes) decides
    refund vs release. If the escrow has already SETTLED, this records the
    dispute for after-the-fact arbitration.
    """
    reason = (body or {}).get("reason", "").strip()
    evidence = (body or {}).get("evidence", {})
    if not reason:
        raise HTTPException(status_code=400, detail="reason required")
    if not _JOB_STORE_OK:
        raise HTTPException(status_code=503, detail="job_store_unavailable")

    from job_store import update_job_status, _conn, _gen_id

    # Confirm the job exists first so we don't lose dispute data on a typo.
    existing = get_job(job_id) if get_job else None
    if not existing:
        raise HTTPException(status_code=404, detail="job not found")

    # Status transition: only move to DISPUTED if it isn't already terminal-
    # disputed. Other terminal states (DELIVERED/SETTLED/FAILED/REFUNDED) are
    # allowed - a buyer can dispute after settlement.
    try:
        update_job_status(job_id, "DISPUTED")
    except Exception:
        logger.exception("dispute status update failed for %s", job_id)

    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO genesis_job_events
                  (id, "jobId", "eventType", payload, "createdAt")
                VALUES (%s, %s, 'dispute_filed', %s::jsonb, NOW())
                """,
                (_gen_id(), job_id, json.dumps({"reason": reason, "evidence": evidence})),
            )
            conn.commit()
    except Exception:
        logger.exception("dispute event insert failed for %s", job_id)

    return {"ok": True, "job_id": job_id, "status": "DISPUTED"}


@app.get("/agents/{slug}/reputation")
async def agent_reputation(slug: str):
    """Read an agent's 30-day reputation stats. Falls back to seed values."""
    try:
        from capability_cards import _reputation_for
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=503, detail=f"reputation_unavailable: {exc}")
    return {"slug": slug, "reputation": _reputation_for(slug)}


@app.get("/admin/disputes", dependencies=[Depends(require_admin)])
async def list_disputes():
    """Admin view: all DISPUTED jobs with their most recent dispute payload.

    Gated by the require_admin dependency (X-Admin-Email header checked
    against the SWARMSYNC_ADMIN_EMAILS allowlist, defaulting to
    bullrushinvestments@gmail.com). v1 simple header check; will upgrade
    to SwarmSync's user JWT auth when the gateway and main API merge.
    """
    if not _JOB_STORE_OK:
        raise HTTPException(status_code=503, detail="job_store_unavailable")
    from job_store import _conn
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT j.*,
                       (SELECT payload FROM genesis_job_events e
                        WHERE e."jobId" = j.id AND e."eventType" = 'dispute_filed'
                        ORDER BY e."createdAt" DESC LIMIT 1) AS dispute_payload
                FROM genesis_jobs j
                WHERE j.status = 'DISPUTED'
                ORDER BY j."updatedAt" DESC
                LIMIT 100
                """,
            )
            rows = cur.fetchall()
            # Serialize datetimes
            for r in rows:
                for k in ("createdAt", "updatedAt", "startedAt", "completedAt", "lastHeartbeatAt"):
                    if r.get(k) and hasattr(r[k], "isoformat"):
                        r[k] = r[k].isoformat()
            return {"items": rows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/", response_class=HTMLResponse)
async def admin_ui():
    """Genesis Admin UI — single-page app for managing disputes.

    Auth is handled client-side via the X-Admin-Email header (the page
    prompts the operator on first visit and stores the value in
    localStorage). The API endpoints this UI calls
    (/admin/disputes, /admin/disputes/{id}/refund,
    /admin/disputes/{id}/resolve) remain protected by require_admin.
    """
    html_path = _STATIC_DIR / "admin.html"
    if not html_path.exists():
        return HTMLResponse(
            "<h1>Admin UI not deployed</h1>"
            "<p>Missing apps/agents-gateway/static/admin.html on the server.</p>",
            status_code=500,
        )
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.post("/admin/disputes/{job_id}/refund", dependencies=[Depends(require_admin)])
async def admin_refund(job_id: str):
    """Admin issues refund — releases the escrow back to the buyer.

    If the job has an associated escrow id and the escrow_client wrappers
    imported successfully, the escrow is released with reason
    `admin_refund`. The job row transitions to REFUNDED and an
    `admin_refund` event row is appended to genesis_job_events.
    """
    if not _JOB_STORE_OK:
        raise HTTPException(status_code=503, detail="job_store_unavailable")
    from job_store import get_job as _get_job, update_job_status, _conn, _gen_id

    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    escrow_released: Any = None
    # ESCROW BLOCKER 8/11 — escrow_client.release_escrow (admin refund endpoint).
    # PERMANENTLY_PROHIBITED (6.1 item 18). Unreachable while _ESCROW_OK is
    # False; the job row still transitions to REFUNDED and the admin_refund
    # event is still written, so the record-keeping half of the endpoint keeps
    # working while the money movement does not. Quarantined, not deleted.
    if job.get("escrowId") and _ESCROW_OK and release_escrow is not None:
        try:
            escrow_released = await release_escrow(
                escrow_id=job["escrowId"], reason="admin_refund"
            )
        except Exception:
            logger.exception("escrow release failed for admin refund of %s", job_id)

    try:
        update_job_status(job_id, "REFUNDED")
    except Exception:
        logger.exception("admin_refund status update failed for %s", job_id)

    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO genesis_job_events
                  (id, "jobId", "eventType", payload, "createdAt")
                VALUES (%s, %s, 'admin_refund', %s::jsonb, NOW())
                """,
                (_gen_id(), job_id, json.dumps({"escrow": escrow_released})),
            )
            conn.commit()
    except Exception:
        logger.exception("admin_refund event insert failed for %s", job_id)

    return {
        "ok": True,
        "job_id": job_id,
        "status": "REFUNDED",
        "escrow": escrow_released,
    }


@app.post("/admin/disputes/{job_id}/resolve", dependencies=[Depends(require_admin)])
async def admin_resolve(job_id: str):
    """Admin marks dispute resolved without refund — closes the case as-is.

    Useful when the buyer's evidence does not warrant a refund (e.g. the
    delivery met spec). Job transitions to SETTLED and an
    `admin_resolve` event row is appended to genesis_job_events.
    """
    if not _JOB_STORE_OK:
        raise HTTPException(status_code=503, detail="job_store_unavailable")
    from job_store import get_job as _get_job, update_job_status, _conn, _gen_id

    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    try:
        update_job_status(job_id, "SETTLED")
    except Exception:
        logger.exception("admin_resolve status update failed for %s", job_id)

    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO genesis_job_events
                  (id, "jobId", "eventType", payload, "createdAt")
                VALUES (%s, %s, 'admin_resolve', %s::jsonb, NOW())
                """,
                (_gen_id(), job_id, json.dumps({"resolved_by": "admin"})),
            )
            conn.commit()
    except Exception:
        logger.exception("admin_resolve event insert failed for %s", job_id)

    return {"ok": True, "job_id": job_id, "status": "RESOLVED"}


@app.post("/agents/{slug}/negotiate", status_code=202)
async def negotiate_agent(
    slug: str,
    request: NegotiateRequest,
    background_tasks: BackgroundTasks,
    x_agent_gateway_secret: str = Header(default="", alias="x-agent-gateway-secret"),
) -> dict:
    """
    POST /agents/{slug}/negotiate

    Receives an inbound AP2 negotiation from the SwarmSync API.
    Returns 202 immediately; LLM evaluation and callback happen in the background.

    The callback is POSTed to `request.callback_url` (typically
    https://api.swarmsync.ai/ap2/gateway/respond) with the agent's decision.
    """
    # Verify the request came from the trusted SwarmSync API
    if not AGENT_GATEWAY_SECRET:
        raise HTTPException(status_code=503, detail="Negotiate endpoint disabled: AGENT_GATEWAY_SECRET not configured")
    if x_agent_gateway_secret != AGENT_GATEWAY_SECRET:
        raise HTTPException(status_code=401, detail="Invalid gateway secret")

    if slug not in AGENT_PERSONAS:
        # Graceful fallback — unknown slugs use a generic persona, not a 404,
        # so the AP2 flow is never silently dropped due to a slug mismatch.
        logger.warning(
            "negotiate_agent: unknown slug=%s — will use generic persona fallback",
            slug,
        )

    background_tasks.add_task(_evaluate_and_respond, slug, request)
    return {
        "status": "evaluating",
        "negotiation_id": request.negotiation_id,
        "agent": slug,
    }


# ---------------------------------------------------------------------------
# AP2 Negotiate helpers
# ---------------------------------------------------------------------------

async def evaluate_negotiation(slug: str, request: NegotiateRequest) -> dict:
    """
    Ask the agent's LLM persona to evaluate an inbound AP2 negotiation.
    Returns a dict with keys: status, price, notes, counter_budget (optional).
    Defaults to ACCEPTED at the requested budget on any parse failure.
    """
    persona = AGENT_PERSONAS.get(slug)
    if persona:
        _, system_prompt = persona
    else:
        display_name = slug.replace("_", " ").replace("-", " ").title()
        system_prompt = (
            f"You are {display_name}, a specialised AI agent on the SwarmSync marketplace. "
            "Respond helpfully and concisely in character."
        )

    requirements_str = str(request.requirements) if request.requirements else "None"
    notes_str = request.notes or "None"

    negotiation_prompt = (
        "You have received an inbound service request via the AP2 protocol.\n\n"
        f"Requester: {request.requester_agent_name}\n"
        f"Service requested: {request.requested_service}\n"
        f"Budget offered: ${request.budget}\n"
        f"Requirements: {requirements_str}\n"
        f"Notes: {notes_str}\n\n"
        "Evaluate this request based on your capabilities and the budget offered.\n"
        "Respond with ONLY a valid JSON object (no markdown, no extra text):\n"
        "{\n"
        '  "status": "ACCEPTED" or "REJECTED" or "COUNTERED",\n'
        '  "price": <number — your accepted or counter price>,\n'
        '  "notes": "<brief reasoning, max 2 sentences>",\n'
        '  "counter_budget": <number, only if COUNTERED — your preferred price>\n'
        "}\n\n"
        "Decision criteria:\n"
        "- ACCEPTED: the service matches your capabilities and the budget is fair\n"
        "- COUNTERED: the service matches but you need a different price (set counter_budget)\n"
        "- REJECTED: the service is outside your capabilities entirely"
    )

    default_decision = {
        "status": "REJECTED",
        "price": request.budget,
        "notes": "LLM evaluation unavailable — auto-rejected to prevent unintended commitments.",
    }

    try:
        raw = await call_llm_router(system_prompt, negotiation_prompt)
    except HTTPException as e:
        logger.error(
            "LLM router error for negotiate slug=%s status=%s: %s",
            slug,
            e.status_code,
            e.detail,
        )
        return default_decision
    except Exception as exc:
        logger.error("Unexpected error calling LLM for negotiate slug=%s: %s", slug, exc)
        return default_decision

    raw_text, _ = _router_result_payload(raw)
    # Strip markdown code fences if the model wrapped the JSON
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    import json as _json
    try:
        decision = _json.loads(stripped)
        if "status" not in decision or decision["status"] not in ("ACCEPTED", "REJECTED", "COUNTERED"):
            logger.warning("LLM returned unrecognised status for slug=%s — defaulting to REJECTED", slug)
            return default_decision
        return decision
    except Exception as parse_exc:
        logger.warning(
            "Failed to parse LLM JSON for negotiate slug=%s (%s) — defaulting to REJECTED. Raw: %.200s",
            slug,
            parse_exc,
            raw_text,
        )
        return default_decision


def _assert_callback_url_trusted(callback_url: str) -> None:
    """
    SSRF guard: reject any callback_url whose origin does not match the
    configured SWARMSYNC_API_URL.  This prevents a malicious negotiate
    payload from redirecting the gateway's authenticated POST (which carries
    AGENT_GATEWAY_SECRET) to an arbitrary host.
    """
    from urllib.parse import urlparse

    allowed_origin = urlparse(SWARMSYNC_API_URL).netloc.lower()
    incoming_origin = urlparse(callback_url).netloc.lower()

    if not incoming_origin or incoming_origin != allowed_origin:
        raise ValueError(
            f"Untrusted callback_url origin '{incoming_origin}' — "
            f"only '{allowed_origin}' is allowed"
        )


async def send_negotiate_callback(callback_url: str, payload: dict) -> bool:
    """
    POST the negotiation decision back to the SwarmSync API callback URL.
    Returns True on 2xx response, False otherwise.

    The callback_url is validated against SWARMSYNC_API_URL before any
    network call is made to prevent SSRF via a crafted negotiate payload.
    """
    try:
        _assert_callback_url_trusted(callback_url)
    except ValueError as ssrf_exc:
        logger.error("SSRF guard blocked callback to '%s': %s", callback_url, ssrf_exc)
        return False

    headers = {
        "Content-Type": "application/json",
        "x-agent-gateway-secret": AGENT_GATEWAY_SECRET,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(callback_url, json=payload, headers=headers)
        if 200 <= resp.status_code < 300:
            logger.info("Negotiate callback succeeded: %d %s", resp.status_code, callback_url)
            return True
        elif 400 <= resp.status_code < 500:
            # 4xx means the API rejected this request permanently (e.g. "Negotiation no
            # longer actionable" after a prior call already succeeded).  Retrying would
            # create duplicate escrows, so treat this as a terminal non-retriable outcome.
            logger.warning(
                "Negotiate callback returned 4xx (terminal — will not retry): %d %s — body: %.200s",
                resp.status_code,
                callback_url,
                resp.text,
            )
            return True  # signal caller: nothing more to do, stop retrying
        else:
            logger.warning(
                "Negotiate callback returned 5xx: %d %s — body: %.200s",
                resp.status_code,
                callback_url,
                resp.text,
            )
            return False
    except Exception as exc:
        logger.error("Negotiate callback request failed for %s: %s", callback_url, exc)
        return False


async def _evaluate_and_respond(slug: str, request: NegotiateRequest) -> None:
    """Background task: evaluate negotiation via LLM and POST decision to callback URL."""
    try:
        decision = await evaluate_negotiation(slug, request)

        if decision.get("status") == "COUNTERED":
            resolved_price = decision.get("counter_budget", request.budget)
        else:
            resolved_price = decision.get("price", request.budget)

        payload = {
            "negotiationId": request.negotiation_id,
            "responderAgentId": request.responder_agent_id,
            "status": decision.get("status", "ACCEPTED"),
            "price": resolved_price,
            "notes": decision.get("notes", "Auto-evaluated by agent gateway"),
            "estimatedDelivery": (datetime.utcnow() + timedelta(hours=48)).isoformat() + "Z",
        }

        success = await send_negotiate_callback(request.callback_url, payload)
        if not success:
            logger.warning(
                "Negotiate callback failed for slug=%s negotiation_id=%s",
                slug,
                request.negotiation_id,
            )
    except Exception as exc:
        logger.error("Error in _evaluate_and_respond for slug=%s: %s", slug, exc, exc_info=True)


# ---------------------------------------------------------------------------
# Conduit Verification Endpoints
# ---------------------------------------------------------------------------


# RFC-1918 and loopback prefixes that must never be fetched server-side
_SSRF_BLOCKED_PREFIXES = (
    "localhost",
    "127.",
    "0.",
    "10.",
    "192.168.",
    # 172.16.0.0/12 covers 172.16.x.x through 172.31.x.x
    "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.",
    "172.24.", "172.25.", "172.26.", "172.27.",
    "172.28.", "172.29.", "172.30.", "172.31.",
    "::1",
    "fe80:",
)


def _assert_not_ssrf(url: str) -> None:
    """Raise HTTPException 400 if url resolves to a private or loopback address."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    for blocked in _SSRF_BLOCKED_PREFIXES:
        if host == blocked.rstrip(".") or host.startswith(blocked):
            raise HTTPException(
                status_code=400,
                detail=f"Blocked: URL host '{host}' resolves to a private/loopback address",
            )


@app.get("/verify/hash", dependencies=[Depends(verify_gateway_key)])
async def verify_hash(url: str) -> dict:
    """
    GET /verify/hash?url=<artifact_url>

    Server-side fallback for artifact hashing when the browser-side
    crypto.subtle.digest eval is blocked by CORS.  Returns the SHA-256
    of the artifact bytes fetched from the given URL.

    SSRF protection: rejects any URL whose host matches RFC-1918 or
    loopback prefixes (localhost, 127.x, 10.x, 192.168.x, 172.16-31.x).
    """
    import hashlib as _hashlib
    _assert_not_ssrf(url)
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
            resp = await client.get(url)
            if resp.is_redirect:
                raise HTTPException(
                    status_code=400,
                    detail="Redirects not followed — submit the final URL directly",
                )
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Artifact fetch returned HTTP {exc.response.status_code}",
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Artifact fetch failed: {exc}",
        )
    sha256 = _hashlib.sha256(resp.content).hexdigest()
    return {
        "sha256": sha256,
        "byte_count": len(resp.content),
        "http_status": resp.status_code,
    }


@app.post("/verify", status_code=202)
async def start_verification(
    request: VerifyRequest,
    background_tasks: BackgroundTasks,
    x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret"),
) -> dict[str, str]:
    """
    POST /verify

    Starts an async Conduit verification job and returns 202 Accepted with
    the jobId immediately.  When the job completes, the result is POSTed back
    to {SWARMSYNC_API_URL}/conduit/verifications/{verificationId}/callback.

    Authentication: X-Internal-Secret header (shared secret with NestJS API).
    """
    _require_internal_secret(x_internal_secret)

    job_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    _verification_jobs[job_id] = {
        "job_id": job_id,
        "status": "pending",
        "result": None,
        "created_at": created_at,
        "completed_at": None,
        "error": None,
    }

    logger.info(
        "Verification job %s created for negotiation %s verification %s",
        job_id,
        request.negotiationId,
        request.context.verification_id,
    )

    background_tasks.add_task(_run_and_callback, job_id, request)

    return {"jobId": job_id}


@app.post("/internal/arbitrage/verification-jobs", status_code=202)
async def start_arbitrage_verification(
    request: ArbitrageVerificationRequest,
    background_tasks: BackgroundTasks,
    x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret"),
) -> dict[str, str]:
    """
    POST /internal/arbitrage/verification-jobs

    Starts an async arbitrage verification job and callbacks the NestJS
    arbitrage endpoint when the Conduit run completes.
    """
    _require_internal_secret(x_internal_secret)

    job_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    _arbitrage_verification_jobs[job_id] = {
        "job_id": job_id,
        "status": "pending",
        "result": None,
        "created_at": created_at,
        "completed_at": None,
        "error": None,
    }

    logger.info(
        "Arbitrage verification job %s created for transaction %s verification %s",
        job_id,
        request.transaction_id,
        request.verification_run_id,
    )

    background_tasks.add_task(_run_arbitrage_verification_and_callback, job_id, request)
    return {"jobId": job_id}


@app.get("/verify/{job_id}", response_model=VerifyJobStatus)
async def get_verification_status(
    job_id: str,
    x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret"),
) -> VerifyJobStatus:
    """
    GET /verify/:job_id

    Poll the status of a verification job.

    Authentication: X-Internal-Secret header.
    """
    _require_internal_secret(x_internal_secret)

    job = _verification_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Verification job {job_id} not found")

    result_data = job.get("result")
    result_model: Optional[VerificationResult] = None
    if result_data:
        result_model = VerificationResult(**result_data)

    return VerifyJobStatus(
        job_id=job_id,
        status=job["status"],
        result=result_model,
        created_at=job["created_at"],
        completed_at=job.get("completed_at"),
        error=job.get("error"),
    )


@app.get("/internal/arbitrage/verification-jobs/{job_id}")
async def get_arbitrage_verification_status(
    job_id: str,
    x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    _require_internal_secret(x_internal_secret)

    job = _arbitrage_verification_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Arbitrage verification job {job_id} not found")

    return job


# ---------------------------------------------------------------------------
# SwarmSync Commerce Demo Agent — A2A public registry entry
#
# Serves the agent card at /.well-known/agent.json so a2aregistry.org can
# verify ownership, then handles inbound A2A JSON-RPC and demo flows.
# ---------------------------------------------------------------------------

COMMERCE_DEMO_AGENT_CARD: dict[str, Any] = {
    "id": "swarmsync-commerce-demo",
    "name": "SwarmSync Commerce Demo Agent",
    "description": "Public demo agent showing how AI agents become commerce-ready: create paid tasks, hold funds in escrow, verify delivery, build portable trust with SwarmScore, and release or refund payment safely.",
    "url": "https://swarmsync-agents.onrender.com/a2a",
    "wellKnownURI": "https://swarmsync.ai/.well-known/agent.json",
    "protocolVersion": "0.3.0",
    "version": "1.0.2",
    "author": "SwarmSync.AI",
    "provider": {
        "organization": "SwarmSync.AI",
        "url": "https://swarmsync.ai",
    },
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": True,
    },
    "skills": [
        {
            "id": "escrow-demo",
            "name": "Escrow-Protected Payment Demo",
            "description": "Simulates an escrow-protected agent-to-agent payment flow.",
            "tags": ["escrow", "ap2", "commerce", "demo", "agent payments"],
            "examples": [
                "Show me a paid agent task.",
                "How does escrow work between two AI agents?",
            ],
            "documentationUrl": "https://swarmsync.ai",
            "endpoint": "https://swarmsync-agents.onrender.com/demo/escrow-flow",
        },
        {
            "id": "trust-badge-demo",
            "name": "SwarmScore Trust Badge Demo",
            "description": "Returns a sample portable trust badge for an AI agent.",
            "tags": ["swarmscore", "trust", "badge", "demo", "portable trust"],
            "examples": [
                "What is SwarmScore?",
                "Show me a trust badge for an agent.",
            ],
            "documentationUrl": "https://swarmsync.ai",
            "endpoint": "https://swarmsync-agents.onrender.com/demo/trust-badge/example-agent",
        },
        {
            "id": "task-verification-demo",
            "name": "Task Verification Demo",
            "description": "Simulates task verification and payment release.",
            "tags": ["verification", "task", "commerce", "demo", "payment release"],
            "examples": [
                "How does task verification work?",
                "Show me how payment is released after delivery.",
            ],
            "documentationUrl": "https://swarmsync.ai",
            "endpoint": "https://swarmsync-agents.onrender.com/demo/task-verify",
        },
    ],
    "defaultInputModes": ["text", "application/json"],
    "defaultOutputModes": ["text", "application/json"],
    "tags": ["swarmsync", "commerce", "demo", "a2a", "escrow", "swarmscore", "ap2"],
    "contactEmail": "support@swarmsync.ai",
}


@app.get("/.well-known/agent.json")
async def agent_card() -> dict[str, Any]:
    """A2A agent card — required for a2aregistry.org ownership verification."""
    return COMMERCE_DEMO_AGENT_CARD


@app.get("/a2a/health")
async def a2a_health() -> dict[str, Any]:
    """A2A health probe — some registries and crawlers check /a2a/health."""
    return {"status": "ok", "service": COMMERCE_DEMO_AGENT_CARD["name"]}


@app.get("/a2a")
async def a2a_discovery() -> dict[str, Any]:
    """
    GET returns 200 so registry health probes — which often GET the agent URL — do not see
    405 Method Not Allowed (some proxies surface that as 502 to end users).

    Conversation uses POST JSON-RPC only (see POST /a2a).
    """
    return {
        "service": COMMERCE_DEMO_AGENT_CARD["name"],
        "protocolVersion": COMMERCE_DEMO_AGENT_CARD["protocolVersion"],
        "usage": "POST JSON-RPC 2.0 to this same path",
        "methods": ["tasks/send", "message/send", "messages/send"],
        "documentation": COMMERCE_DEMO_AGENT_CARD.get("provider", {}).get("url"),
    }


@app.post("/a2a", dependencies=[Depends(verify_gateway_key)])
async def a2a_handler(request: Request) -> dict[str, Any]:
    """
    A2A JSON-RPC endpoint (v0.3.0).
    Handles tasks/send and message/send from other agents.
    """
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error — body must be valid JSON"},
        }

    method = body.get("method", "")
    req_id = body.get("id") or str(uuid.uuid4())
    params: dict[str, Any] = body.get("params") or {}

    if method == "tasks/send":
        task = params.get("task") or params
        task_type = task.get("type", "unknown") if isinstance(task, dict) else "unknown"
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "taskId": str(uuid.uuid4()),
                "status": "submitted",
                "type": task_type,
                "message": (
                    "Task received by SwarmSync Commerce Demo Agent. "
                    "Connect at https://swarmsync.ai to run live AP2 escrow flows."
                ),
                "agent": COMMERCE_DEMO_AGENT_CARD["id"],
                "demoEndpoints": {
                    "escrow_flow": "POST /demo/escrow-flow",
                    "trust_badge": "GET /demo/trust-badge/{agent_id}",
                    "task_verify": "POST /demo/task-verify",
                },
            },
        }

    if method in ("message/send", "messages/send"):
        msg = params.get("message") or {}
        parts_text = " ".join(
            p.get("text", "") for p in (msg.get("parts") or []) if isinstance(p, dict) and p.get("kind") == "text"
        ).strip()
        query = parts_text.lower()

        if any(kw in query for kw in ["swarmscore", "trust badge", "trust score", "reputation", "badge", "score", "tier", "rating"]):
            reply_text = (
                "SwarmScore is a portable trust badge for AI agents.\n\n"
                "It helps other agents, marketplaces, and platforms evaluate whether an agent is reliable before hiring it. A SwarmScore can include signals like completed tasks, dispute rate, on-time delivery, verification history, and signed proof records.\n\n"
                "The goal is simple: agents should be able to carry trust with them across the agent economy.\n\n"
                "Demo endpoint: GET https://swarmsync-agents.onrender.com/demo/trust-badge/{your-agent-id}\n"
                "→ Go live: https://swarmsync.ai"
            )
        elif any(kw in query for kw in ["what does swarm", "what is swarm", "what do you do", "tell me about swarm", "explain swarm"]):
            reply_text = (
                "SwarmSync is the trust and transaction layer for autonomous agents.\n\n"
                "It helps AI agents safely pay and get paid by creating paid tasks, holding funds in escrow, verifying delivery, applying portable SwarmScore trust badges, and releasing or refunding payment after the work is complete.\n\n"
                "→ https://swarmsync.ai"
            )
        elif any(kw in query for kw in ["show me", "paid task", "paid agent", "sample task", "example task", "transaction"]):
            reply_text = (
                "Sample paid agent transaction:\n\n"
                "  Buyer agent:         research-buyer-agent\n"
                "  Seller agent:        market-research-agent\n"
                "  Task:                Produce a competitor summary for a SaaS landing page\n"
                "  Price:               45 USDC\n"
                "  Escrow status:       funded\n"
                "  Verification method: delivery proof checked by SwarmSync\n"
                "  Final result:        work verified, payment released\n\n"
                "Flow: Buyer agent creates task → seller accepts → escrow funded → work delivered → verification passed → payment released.\n\n"
                "→ Demo: POST https://swarmsync-agents.onrender.com/demo/escrow-flow\n"
                "→ Go live: https://swarmsync.ai\n\n"
                "Note: This is simulated demo data. No real funds are moved."
            )
        elif any(kw in query for kw in ["escrow", "how does pay", "how does fund", "locked", "hold fund", "ap2", "usdc"]):
            reply_text = (
                "Escrow protects both sides of an agent transaction.\n\n"
                "The buyer agent's funds are held before work begins. The seller agent can see that payment is secured, but cannot collect it until the task is verified. If the work passes verification, payment is released. If the work fails, the funds can be refunded.\n\n"
                "Demo endpoint: POST https://swarmsync-agents.onrender.com/demo/escrow-flow\n"
                "→ Go live: https://swarmsync.ai"
            )
        elif any(kw in query for kw in ["get paid", "receive payment", "earn", "my agent get paid", "how can my"]):
            reply_text = (
                "Your agent can get paid through SwarmSync by becoming commerce-ready.\n\n"
                "The setup flow is:\n"
                "  1. Publish an agent card.\n"
                "  2. Register or claim your agent on SwarmSync.\n"
                "  3. Create a service listing.\n"
                "  4. Set pricing.\n"
                "  5. Enable escrow-backed paid tasks.\n"
                "  6. Complete work.\n"
                "  7. Pass verification.\n"
                "  8. Receive payment after release.\n\n"
                "SwarmSync handles the trust, escrow, verification, and payment-release flow.\n\n"
                "→ https://swarmsync.ai"
            )
        elif any(kw in query for kw in ["commerce-ready", "commerce ready", "make my agent", "ready to sell", "ready to earn", "setup", "set up agent"]):
            reply_text = (
                "To make your agent commerce-ready:\n"
                "  1. Give your agent a public identity.\n"
                "  2. Publish a valid agent card.\n"
                "  3. Add clear skills and services.\n"
                "  4. Connect the agent to SwarmSync.\n"
                "  5. Generate or attach a SwarmScore trust badge.\n"
                "  6. Configure pricing.\n"
                "  7. Enable escrow-backed task acceptance.\n"
                "  8. Test a demo transaction.\n"
                "  9. Move to live paid tasks.\n\n"
                "Commerce-ready means your agent can be discovered, trusted, hired, verified, and paid.\n\n"
                "For examples and docs: https://github.com/swarmsync-ai/commerce-demo-agent\n\n"
                "→ https://swarmsync.ai"
            )
        else:
            reply_text = (
                "SwarmSync Commerce Demo Agent — ask me anything:\n\n"
                "  • \"What does SwarmSync do?\"\n"
                "  • \"Show me a paid agent task.\"\n"
                "  • \"How does escrow work?\"\n"
                "  • \"What is SwarmScore?\"\n"
                "  • \"How can my agent get paid through SwarmSync?\"\n"
                "  • \"How do I make my agent commerce-ready?\"\n\n"
                "→ https://swarmsync.ai"
            )

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "kind": "message",
                "messageId": str(uuid.uuid4()),
                "role": "agent",
                "parts": [{"kind": "text", "text": reply_text}],
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method '{method}' not found"},
    }


@app.get("/demo/commerce")
async def demo_commerce_info() -> dict[str, Any]:
    """Public overview of the Commerce Demo Agent capabilities."""
    return {
        "agent": "SwarmSync Commerce Demo Agent",
        "purpose": "Demonstrates how AI agents become commerce-ready through trust, escrow, verification, and payment.",
        "suggested_questions": [
            "What does SwarmSync do?",
            "Show me a paid agent task.",
            "How does escrow work?",
            "What is SwarmScore?",
            "How can my agent get paid through SwarmSync?",
            "How do I make my agent commerce-ready?",
        ],
        "demo_endpoints": [
            {
                "method": "POST",
                "url": "https://swarmsync-agents.onrender.com/demo/escrow-flow",
                "description": "Simulated AP2 escrow flow: create → fund → verify → release.",
            },
            {
                "method": "GET",
                "url": "https://swarmsync-agents.onrender.com/demo/trust-badge/{agent_id}",
                "description": "Sample SwarmScore portable trust badge for an AI agent.",
            },
            {
                "method": "POST",
                "url": "https://swarmsync-agents.onrender.com/demo/task-verify",
                "description": "Simulated task verification and payment release.",
            },
        ],
        "next_step": "Register at https://swarmsync.ai to run real transactions.",
        "github_repo": "https://github.com/swarmsync-ai/commerce-demo-agent",
        "demo_note": "All demo data is simulated. No real funds are moved.",
    }


@app.post("/demo/escrow-flow")
async def demo_escrow_flow() -> dict[str, Any]:
    """Simulated AP2 escrow flow: create → fund → verify → release."""
    escrow_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    return {
        "demo": "AP2 Escrow Flow",
        "escrow_id": escrow_id,
        "status": "completed",
        "steps": [
            {"step": 1, "name": "create", "status": "ok", "ts": now,
             "detail": "Escrow created between buyer and seller agents"},
            {"step": 2, "name": "fund", "status": "ok", "ts": now,
             "detail": "USDC locked in AP2 smart contract on Base"},
            {"step": 3, "name": "verify", "status": "ok", "ts": now,
             "detail": "Conduit browser verified delivery at target URL"},
            {"step": 4, "name": "release", "status": "ok", "ts": now,
             "detail": "Funds released to seller agent wallet on Base"},
        ],
        "about": "This demo shows an AP2-style escrow flow for agent-to-agent commerce.",
        "next_step": "Make your agent commerce-ready at https://swarmsync.ai",
        "demo_note": "This is a simulated demo. No real funds are moved.",
        "buyer_agent": "research-buyer-agent",
        "seller_agent": "market-research-agent",
        "task": "Produce a competitor summary for a SaaS landing page",
        "amount_usdc": 45.00,
        "swarmscore_impact": "Successful completion increases seller SwarmScore.",
    }


@app.get("/demo/trust-badge/{agent_id}")
async def demo_trust_badge(agent_id: str) -> dict[str, Any]:
    """Simulated SwarmScore trust badge for an agent."""
    return {
        "demo": "SwarmScore Trust Badge",
        "agent_id": agent_id,
        "swarmscore": {
            "score": 847,
            "max_score": 1000,
            "tier": "Gold",
            "completed_tasks": 142,
            "dispute_rate": 0.007,
            "on_time_rate": 0.97,
            "badge_url": f"https://swarmsync.ai/badges/{agent_id}",
            "verified": True,
        },
        "about": (
            "SwarmScore is a portable trust badge for AI agents. It helps agents, "
            "marketplaces, registries, and platforms evaluate reliability before paid work begins."
        ),
        "meaning": "This badge shows how a third party could evaluate agent trust before hiring.",
        "next_step": "Make your agent commerce-ready at https://swarmsync.ai",
        "portable": True,
        "verified_by": "SwarmScore",
        "verification_url": f"https://swarmsync.ai/verify/{agent_id}",
        "display_recommendation": "Show this badge on your agent card to increase hire rate.",
        "demo_note": "This is a simulated trust badge. Real scores are computed from live transaction history.",
    }


@app.post("/demo/task-verify")
async def demo_task_verify() -> dict[str, Any]:
    """Simulated task verification and payment release."""
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    return {
        "demo": "Task Verification + Payment Release",
        "job_id": job_id,
        "result": {
            "verification_status": "PASSED",
            "proof_hash": f"sha256:a3f8c2{job_id[:8]}",
            "verified_at": now,
            "payment_released": True,
            "amount_usdc": 45.00,
            "recipient_agent": "example-seller-agent",
        },
        "about": "The demo shows how SwarmSync ties payment release to verified delivery.",
        "meaning": "Payment was released because the work passed verification.",
        "next_step": "Make your agent commerce-ready at https://swarmsync.ai",
        "verification_method": "SwarmSync delivery proof check",
        "release_decision": "Payment released to seller agent.",
        "refund_if_failed": "Funds returned to buyer agent if verification fails.",
        "swarmscore_updated": True,
        "demo_note": "This is a simulated verification. No real funds are moved.",
    }


# ----------------------------------------------------------------------------
# Phase 10 - Capability cards + marketplace listing
# ----------------------------------------------------------------------------

@app.get("/.well-known/agents.json")
async def well_known_agents() -> dict[str, Any]:
    """Public discovery endpoint - JSON-LD listing of all Genesis agents."""
    return {
        "version": 1,
        "platform": "swarmsync",
        "agents": all_cards(),
    }


@app.get("/agents/{slug}/card")
async def agent_capability_card(slug: str) -> dict[str, Any]:
    """Single agent's capability card."""
    card = card_for(slug)
    if card is None:
        raise HTTPException(status_code=404, detail=f"agent not found: {slug}")
    return card


@app.get("/marketplace/search")
async def marketplace_search(
    q: str = "",
    capability: str = "",
    max_price_cents: int = 0,
    job_mode: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """Simple search over capability cards. Postgres FTS upgrade in Phase 11.

    q: keyword query (matches name + description)
    capability: filter to agents whose tools_advertised contains this name
    max_price_cents: cap on total price
    job_mode: 'sync' | 'async' (omit for both)
    """
    cards = all_cards()
    if q:
        ql = q.lower()
        cards = [c for c in cards if ql in c["name"].lower() or ql in c["description"].lower()]
    if capability:
        cards = [c for c in cards if any(cap["tool"] == capability for cap in c["capabilities"])]
    if max_price_cents > 0:
        cards = [c for c in cards if c["pricing"]["total_cents"] <= max_price_cents]
    if job_mode in ("sync", "async"):
        cards = [c for c in cards if c["job_mode"] == job_mode]

    # Rank by reputation, then price asc
    cards.sort(key=lambda c: (-c["reputation"]["rating"], c["pricing"]["total_cents"]))
    return {"total": len(cards), "results": cards[:limit]}


# ----------------------------------------------------------------------------
# Phase 9 - Output storage + Conduit session delivery
# ----------------------------------------------------------------------------

@app.get("/jobs/{job_id}/artifacts")
async def job_artifacts(job_id: str) -> dict[str, Any]:
    """List all artifacts for a job, with current signed URLs."""
    listing = list_artifacts(job_id=job_id)
    if not listing.get("ok"):
        raise HTTPException(status_code=500, detail=listing.get("error", "unknown"))
    items = listing.get("items", [])
    # Refresh signed URLs
    for item in items:
        url_info = get_signed_url(job_id=job_id, name=item["name"])
        if url_info.get("ok"):
            item["signed_url"] = url_info["signed_url"]
            item["expires_in_seconds"] = url_info.get("expires_in_seconds")
    return {"job_id": job_id, "backend": listing.get("backend"), "items": items}


@app.post("/jobs/{job_id}/session", dependencies=[Depends(verify_gateway_key)])
async def store_buyer_session(job_id: str, body: dict) -> dict[str, Any]:
    """Buyer uploads a Conduit session export for the agent to operate under.

    Body: {"session_data": {...conduit session export...}}
    """
    session_data = body.get("session_data")
    if not session_data:
        raise HTTPException(status_code=400, detail="session_data required")
    result = store_session(job_id=job_id, session_data=session_data)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "unknown"))
    return result


@app.get("/artifacts/{job_id}/{name:path}")
async def serve_artifact(job_id: str, name: str):
    """Serve a local artifact when S3 is unavailable. Read-only."""
    local_dir = Path(os.getenv("GENESIS_LOCAL_ARTIFACT_DIR", "/var/data/genesis-artifacts"))
    path = local_dir / job_id / name
    # Path traversal guard
    try:
        path.resolve().relative_to(local_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(str(path))


@app.get("/proofs/{proof_id}/verify")
async def verify_proof(proof_id: str):
    """Verify a GenesisProof's VCAP wrapper JWT.

    Returns the proof metadata plus an Ed25519 verification verdict over the
    stored wrapper token. Does NOT re-verify the Conduit bundle bytes — that
    requires downloading the .tar.gz and running its bundled verify.py.
    """
    if not _PROOF_BRIDGE_OK or verify_vcap_wrapper_jwt is None:
        raise HTTPException(status_code=503, detail="proof_bridge unavailable")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception:
        raise HTTPException(status_code=503, detail="psycopg unavailable")

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise HTTPException(status_code=503, detail="DATABASE_URL not configured")

    try:
        with psycopg.connect(db_url, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM genesis_proofs WHERE id = %s', (proof_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="proof not found")

            verification = verify_vcap_wrapper_jwt(row["vcapWrapperJwt"])
            created_at = row.get("createdAt")
            return {
                "proof_id": proof_id,
                "job_id": row.get("jobId"),
                "agent_slug": row["agentSlug"],
                "created_at": created_at.isoformat() if created_at else None,
                "verification": verification,
                "proof_bundle_uri": row["proofBundleUri"],
                "proof_bundle_signed_url": row.get("proofBundleSignedUrl"),
                "input_hash": row["inputHash"],
                "output_hash": row["outputHash"],
                "conduit_session_id": row["conduitSessionId"],
            }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("proof verification failed for proof_id=%s: %s", proof_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"database error: {exc}") from exc
