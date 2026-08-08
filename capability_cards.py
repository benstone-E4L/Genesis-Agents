"""Capability cards - JSON-LD representation of each Genesis agent for marketplace discovery."""
from __future__ import annotations
import json, logging, os
from pathlib import Path
from typing import Any
from bundle_loader import list_bundles, load_bundle

log = logging.getLogger(__name__)

PLATFORM_FEE_PCT = float(os.getenv("SWARMSYNC_PLATFORM_FEE_PCT", "0.10"))
TREASURY_WALLET = os.getenv("X402_PLATFORM_WALLET_ADDRESS", "")

# Cold-start reputation seeding (per plan): 5-star + 100% for first 10 jobs.
SEED_RATING = 5.0
SEED_SUCCESS_RATE = 1.0
SEED_JOBS_THRESHOLD = 10


def _tool_descriptions() -> dict[str, str]:
    """Map of tool name -> one-line description for capability cards.

    PERMANENTLY_PROHIBITED names are filtered out before the map is returned
    (docs/FINANCE-TOOL-CONTRACTS.md Section 6.2 Layer 6): a capability card is a
    public advertisement, and the marketplace must not advertise an operation
    that cannot and must not run. The filter is applied programmatically rather
    than by deleting lines, so a name re-added below is suppressed automatically.
    """
    from runtime.tool_policy import PROHIBITED_TOOLS

    descriptions = {
        "conduit": "Audited browser automation, multi-engine web search, structured extraction, marketplace adapters, proof bundle export.",
        "file_write": "Write artifacts to ephemeral job storage.",
        "code_format": "Format code via black or prettier.",
        "genesis_call": "Dispatch a sub-task to another Genesis agent (orchestrator only).",
        # Commerce/finance/billing/pricing/domain (Phase 3)
        "commerce_register_domain": "Register a domain name for the buyer's commerce stack.",
        "commerce_activate_payment_gateway": "Activate a payment processor (Stripe/PayPal/etc.).",
        "commerce_configure_tax_engine": "Configure regional tax rules.",
        "commerce_ship_fulfillment_batch": "Submit shipments to fulfillment provider.",
        "commerce_launch_commerce_stack": "Orchestrate full commerce stack setup.",
        "finance_run_payroll_batch": "Run a payroll batch for employees.",
        "finance_process_vendor_invoice": "Process a vendor invoice.",
        "finance_generate_pnl": "Generate a P&L statement.",
        "finance_reconcile_accounts": "Reconcile account ledgers.",
        "finance_export_books": "Export accounting books in standard format.",
        "pricing_run_elasticity_experiment": "Run a price elasticity experiment.",
        "pricing_purchase_dataset": "Purchase a pricing dataset (uses x402).",
        "pricing_deploy_price_change": "Deploy a new price for a product.",
        "pricing_generate_report": "Generate a pricing/revenue report.",
        "billing_import_ar_ledger": "Import an accounts-receivable ledger.",
        "billing_run_dunning_sequence": "Execute a dunning sequence for overdue accounts.",
        "billing_change_subscription_plan": "Change a customer's subscription plan.",
        "billing_generate_revops_report": "Generate a revenue operations report.",
        "billing_process_refund": "Process a refund.",
        "domain_generate_candidates": "Generate domain name candidates from a theme.",
        "domain_check_availability": "Check domain availability (Name.com).",
        "domain_register": "Register a domain (requires AP2 escrow consent).",
        "domain_configure_dns": "Configure DNS records.",
        # HR/data/workflow/vision (Phase 5)
        "hr_greenhouse_query": "Query Greenhouse ATS (OAuth, Phase 9).",
        "hr_lever_query": "Query Lever ATS (OAuth, Phase 9).",
        "hr_bamboohr_query": "Query BambooHR (OAuth, Phase 9).",
        "hr_template_generate": "Generate HR document from template.",
        "data_s3_signed_url": "Generate signed S3 URL.",
        "data_bigquery_query": "Execute BigQuery query.",
        "data_dbt_compile": "Compile a dbt model.",
        "data_pipeline_design": "Design an ETL pipeline.",
        "data_quality_check": "Run data-quality checks on a table.",
        "workflow_zapier_export": "Export a Zapier zap.",
        "workflow_n8n_export": "Export an n8n workflow.",
        "workflow_make_export": "Export a Make scenario.",
        "workflow_webhook_trigger": "Trigger a webhook with payload.",
        "vision_analyze": "Analyze an image with a vision-capable model.",
        "vision_ocr": "Extract text from an image (OCR).",
        "vision_compare": "Compare two images.",
        "vision_extract_chart_data": "Extract data from chart images.",
    }
    return {k: v for k, v in descriptions.items() if k not in PROHIBITED_TOOLS}


def _reputation_for(slug: str) -> dict[str, Any]:
    """Return reputation stats for an agent. Falls back to seed values until DB has data."""
    # Phase 8 sibling adds the Job table; we lazily try to import job_store.
    # If unavailable or no jobs, return seed values per the plan.
    try:
        from job_store import _conn
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE status IN ('SETTLED', 'DELIVERED')) AS successes,
                  COUNT(*) FILTER (WHERE status IN ('FAILED', 'REFUNDED', 'EXPIRED')) AS failures,
                  COUNT(*) FILTER (WHERE status = 'DISPUTED') AS disputes,
                  COALESCE(EXTRACT(EPOCH FROM AVG("completedAt" - "createdAt")) FILTER (WHERE status = 'SETTLED'), 0) AS avg_settle_s
                FROM genesis_jobs
                WHERE "agentSlug" = %s AND "createdAt" > NOW() - INTERVAL '30 days'
                """,
                (slug,),
            )
            row = cur.fetchone()
            if not row or row["total"] == 0 or row["total"] < SEED_JOBS_THRESHOLD:
                return {
                    "rating": SEED_RATING,
                    "success_rate": SEED_SUCCESS_RATE,
                    "total_jobs_30d": int(row["total"]) if row else 0,
                    "dispute_rate": 0.0,
                    "avg_settlement_seconds": 0,
                    "seeded": True,
                }
            return {
                "rating": round(SEED_RATING * (row["successes"] / row["total"]), 2),  # crude rating
                "success_rate": round(row["successes"] / row["total"], 3),
                "total_jobs_30d": int(row["total"]),
                "dispute_rate": round(row["disputes"] / row["total"], 3) if row["total"] else 0.0,
                "avg_settlement_seconds": int(row["avg_settle_s"] or 0),
                "seeded": False,
            }
    except Exception as e:
        log.debug("reputation lookup failed for %s: %s", slug, e)
        return {
            "rating": SEED_RATING,
            "success_rate": SEED_SUCCESS_RATE,
            "total_jobs_30d": 0,
            "dispute_rate": 0.0,
            "avg_settlement_seconds": 0,
            "seeded": True,
        }


def card_for(slug: str) -> dict[str, Any] | None:
    bundle = load_bundle(slug)
    if bundle is None:
        return None

    tool_descs = _tool_descriptions()
    capabilities = []
    for t in bundle.get("tools_advertised", []):
        capabilities.append({
            "tool": t,
            "description": tool_descs.get(t, "Custom tool - see runtime registry for schema."),
        })

    total_price_cents = bundle.get("price_tier_default_cents", 0)
    platform_fee_cents = int(total_price_cents * PLATFORM_FEE_PCT)
    agent_net_cents = total_price_cents - platform_fee_cents

    reputation = _reputation_for(slug)

    return {
        "@context": "https://schema.org",
        "@type": "Service",
        "slug": bundle["slug"],
        "name": bundle["name"],
        "version": bundle.get("version", "1.0.0"),
        "description": bundle["system_prompt"][:300],
        "is_orchestrator": bool(bundle.get("is_orchestrator", False)),
        "job_mode": bundle.get("job_mode", "sync"),
        "status": bundle.get("status", "deployed"),
        "model_hint": bundle.get("model_hint"),
        "capabilities": capabilities,
        "output_shape_hint": bundle.get("output_shape_hint", []),
        "limits": {
            "token_budget": bundle.get("token_budget", 4000),
            "conduit_budget_cents": bundle.get("conduit_budget_cents", 200),
            "timeout_seconds": bundle.get("timeout_s", 300),
        },
        "pricing": {
            "total_cents": total_price_cents,
            "total_usd": round(total_price_cents / 100, 2),
            "agent_net_cents": agent_net_cents,
            "agent_net_usd": round(agent_net_cents / 100, 2),
            "platform_fee_cents": platform_fee_cents,
            "platform_fee_pct": PLATFORM_FEE_PCT,
            "currency": "USD",
        },
        "reputation": reputation,
        "endpoints": {
            "invoke_sync": f"/agents/{slug}/run",
            "invoke_async": f"/agents/{slug}/jobs",
        },
        "treasury_wallet": TREASURY_WALLET,
    }


def all_cards() -> list[dict[str, Any]]:
    cards = []
    for slug in list_bundles():
        c = card_for(slug)
        if c is not None:
            cards.append(c)
    return cards
