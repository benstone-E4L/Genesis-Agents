"""Genesis Pricing tools.

Implements Sections 5 and 6 of docs/FINANCE-TOOL-CONTRACTS.md.

What changed and why:

  * ``pricing_purchase_dataset`` and ``pricing_run_pricing_cycle`` are
    PERMANENTLY_PROHIBITED (Section 6.1 Group A, items 10 and 11). Their
    bodies, schemas and register_tool lines are DELETED. Absence beats denial.
  * ``pricing_generate_pricing_report`` is PERMANENTLY_PROHIBITED (Section 6.1
    Group C, item 20) for fabricating financial figures. It previously returned
    revenue_total_usd 482500.00, revenue_delta_pct 8.4, elasticity_mean -1.32
    and best_price_point_usd 49.00 as hardcoded constants wrapped in ok: true —
    output that did not depend on any input. The constants are deleted, the
    schema and register_tool line are deleted, and the symbol survives only as
    a refusal shim so that in-repo callers fail loudly instead of raising
    AttributeError.
  * Every remaining action returns the Section 5 truthful-failure envelope.
    ok: true is unreachable without evidence.
"""
from __future__ import annotations

import logging
from typing import Any

from . import register_tool
from ._envelope import (
    MODE_READ_ONLY,
    from_exception,
    not_implemented,
    prohibited_refusal,
    provider_unconfigured,
)

log = logging.getLogger(__name__)

# Environment variable NAMES only — never values (Section 5.4).
_BUDGET_STORE_KEYS = ["GENESIS_JOB_DATABASE_URL", "DATABASE_URL"]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def pricing_run_elasticity_experiment(
    *,
    experiment_id: str | None = None,
    product_id: str | None = None,
    price_range: list[float] | None = None,
    duration_days: int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """QUARANTINE (contract 3.4). APPROVAL_REQUIRED — changes what real
    customers are charged. No provider is wired and no Cato authorization gate
    exists, so the only truthful response is a refusal."""
    try:
        return not_implemented(
            "pricing_run_elasticity_experiment",
            "Live price-elasticity experiments are not implemented. This tool "
            "would change what real customers are charged and requires a named "
            "channel provider, price readback evidence and a per-call human "
            "authorization, none of which exist.",
            detail={
                "mode": "APPROVAL_REQUIRED",
                "verdict": "QUARANTINE",
                "contract_ref": "FINANCE-TOOL-CONTRACTS.md 3.4 pricing_run_elasticity_experiment",
            },
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("pricing_run_elasticity_experiment", e)


async def pricing_deploy_pricing_update(
    *,
    channel: str | None = None,
    product_id: str | None = None,
    new_price_minor: int | None = None,
    currency: str | None = None,
    scope: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """QUARANTINE (contract 3.4). APPROVAL_REQUIRED — alters a price."""
    try:
        return not_implemented(
            "pricing_deploy_pricing_update",
            "Pushing a price change is not implemented. It requires a named "
            "channel provider (never resolved at call time from a free-text "
            "channel string), price readback evidence, an enforced max_change_bp "
            "and a per-call human authorization.",
            detail={
                "mode": "APPROVAL_REQUIRED",
                "verdict": "QUARANTINE",
                "contract_ref": "FINANCE-TOOL-CONTRACTS.md 3.4 pricing_deploy_pricing_update",
            },
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("pricing_deploy_pricing_update", e)


async def pricing_generate_pricing_report(**kwargs: Any) -> dict[str, Any]:
    """PERMANENTLY PROHIBITED — Section 6.1 Group C, item 20.

    Retained as a refusal shim only. It holds no figures, reads no input and
    can never return ok: true. Not registered as a dispatchable tool: its
    schema and register_tool line are deleted, so ``tools.get_tool`` returns
    None for this name and ``assert_prohibitions_intact()`` passes.

    A genuine successor must be specified separately under a different name as
    READ_ONLY, computing from caller-supplied or Xero-exported data under the
    Section 5.6 evidence rules.
    """
    return prohibited_refusal("pricing_generate_pricing_report", group="C")


async def pricing_get_budget_metrics(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE (contract 3.1, the twelve readers).

    Previously returned monthly_limit 1500.00 / remaining_budget 1500.00 as
    invented constants under ok: true. An accounting runtime reading that
    concludes budget headroom exists. No budget store is wired, so the correct
    response is provider_unconfigured.
    """
    try:
        return provider_unconfigured(
            "pricing_get_budget_metrics",
            _BUDGET_STORE_KEYS,
            "No budget store is wired for the pricing agent. Budget figures are "
            "unavailable; none are invented. Absence of a limit is not headroom.",
            detail={"mode": MODE_READ_ONLY, "verdict": "QUARANTINE"},
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("pricing_get_budget_metrics", e)


async def pricing_get_audit_log(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE. An empty entries[] under ok: true reads as a clean audit log."""
    try:
        return provider_unconfigured(
            "pricing_get_audit_log",
            _BUDGET_STORE_KEYS,
            "No audit store is wired for the pricing agent. No entries can be "
            "returned; an empty log must not be inferred.",
            detail={"mode": MODE_READ_ONLY, "verdict": "QUARANTINE"},
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("pricing_get_audit_log", e)


async def pricing_get_alerts(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE. An empty alerts[] under ok: true reads as 'nothing flagged'."""
    try:
        return provider_unconfigured(
            "pricing_get_alerts",
            _BUDGET_STORE_KEYS,
            "No alert engine is wired for the pricing agent. No alerts can be "
            "returned; 'no alerts raised' must not be inferred.",
            detail={"mode": MODE_READ_ONLY, "verdict": "QUARANTINE"},
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("pricing_get_alerts", e)


# ---------------------------------------------------------------------------
# Schemas
#
# DELETED (PERMANENTLY_PROHIBITED, Section 6.1):
#   pricing_purchase_dataset          (Group A, item 10)
#   pricing_run_pricing_cycle         (Group A, item 11)
#   pricing_generate_pricing_report   (Group C, item 20)
# Do not reintroduce. runtime/tool_policy.assert_prohibitions_intact() makes
# the process refuse to start if any of these names is registered again.
# ---------------------------------------------------------------------------

_SCHEMAS: dict[str, dict[str, Any]] = {
    "pricing_run_elasticity_experiment": {
        "type": "function",
        "function": {
            "name": "pricing_run_elasticity_experiment",
            "description": (
                "NOT IMPLEMENTED. Running a live price-elasticity experiment changes "
                "what real customers are charged and is quarantined pending a named "
                "provider and per-call human authorization. Always returns "
                "ok=false with error.code=not_implemented."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "experiment_id": {"type": "string"},
                    "product_id": {"type": "string"},
                    "price_range": {"type": "array", "items": {"type": "number"}},
                    "duration_days": {"type": "integer"},
                },
                "additionalProperties": True,
            },
        },
    },
    "pricing_deploy_pricing_update": {
        "type": "function",
        "function": {
            "name": "pricing_deploy_pricing_update",
            "description": (
                "NOT IMPLEMENTED. Pushing a price change is quarantined pending a "
                "named channel provider, price readback evidence and per-call human "
                "authorization. Always returns ok=false with error.code=not_implemented."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "product_id": {"type": "string"},
                    "new_price_minor": {"type": "integer", "description": "Integer minor units."},
                    "currency": {"type": "string", "description": "Explicit ISO-4217 code."},
                    "scope": {"type": "string"},
                },
                "additionalProperties": True,
            },
        },
    },
    "pricing_get_budget_metrics": {
        "type": "function",
        "function": {
            "name": "pricing_get_budget_metrics",
            "description": (
                "Return the pricing agent's budget window from the budget store. "
                "No store is wired, so this always returns ok=false with "
                "error.code=provider_unconfigured. It never invents a limit."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
    "pricing_get_audit_log": {
        "type": "function",
        "function": {
            "name": "pricing_get_audit_log",
            "description": (
                "Return pricing audit entries from the audit store. No store is wired, "
                "so this always returns ok=false with error.code=provider_unconfigured. "
                "It never returns an empty log that could be read as 'clean'."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
    "pricing_get_alerts": {
        "type": "function",
        "function": {
            "name": "pricing_get_alerts",
            "description": (
                "Return pricing alerts from the alert engine. None is wired, so this "
                "always returns ok=false with error.code=provider_unconfigured. It "
                "never returns an empty list that could be read as 'nothing flagged'."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
}


def register() -> None:
    # PERMANENTLY_PROHIBITED names are absent by construction. Adding a
    # register_tool line for pricing_purchase_dataset, pricing_run_pricing_cycle
    # or pricing_generate_pricing_report will make the gateway refuse to boot.
    register_tool("pricing_run_elasticity_experiment", pricing_run_elasticity_experiment, _SCHEMAS["pricing_run_elasticity_experiment"])
    register_tool("pricing_deploy_pricing_update", pricing_deploy_pricing_update, _SCHEMAS["pricing_deploy_pricing_update"])
    register_tool("pricing_get_budget_metrics", pricing_get_budget_metrics, _SCHEMAS["pricing_get_budget_metrics"])
    register_tool("pricing_get_audit_log", pricing_get_audit_log, _SCHEMAS["pricing_get_audit_log"])
    register_tool("pricing_get_alerts", pricing_get_alerts, _SCHEMAS["pricing_get_alerts"])
