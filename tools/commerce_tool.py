"""Genesis Commerce tools.

Implements Sections 5 and 6 of docs/FINANCE-TOOL-CONTRACTS.md.

What changed and why:

  * Four functions are PERMANENTLY_PROHIBITED (Section 6.1 Group A, items 6-9)
    and are DELETED — bodies, schemas and register_tool lines:
      commerce_register_domain          (spends money at a registrar)
      commerce_activate_payment_gateway (stands up a money-movement rail)
      commerce_ship_fulfillment_batch   (incurs per-order carrier charges)
      commerce_launch_commerce_stack    (composite of all three)
    Absence beats denial: deleted code cannot be re-enabled by a config change.
  * The remaining actions return the Section 5 truthful-failure envelope.
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
    provider_unconfigured,
)

log = logging.getLogger(__name__)

# Environment variable NAMES only — never values (Section 5.4).
_BUDGET_STORE_KEYS = ["GENESIS_JOB_DATABASE_URL", "DATABASE_URL"]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def commerce_configure_tax_engine(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE (contract 3.3). APPROVAL_REQUIRED.

    It moves no money but determines the tax on every future invoice, which is
    a regulatory position (UK VAT, US state sales tax) with penalty exposure.
    The previous body returned status "configured" under ok: true having
    configured nothing — the worst possible failure mode for a tax position.

    A qualified accountant must sign off the rule set before any implementation.
    """
    try:
        return not_implemented(
            "commerce_configure_tax_engine",
            "Configuring a tax engine is not implemented. It determines the tax on "
            "every future invoice and requires a named provider, an "
            "accountant-approved rule set and readback evidence. No tax "
            "configuration was created or changed.",
            detail={
                "mode": "APPROVAL_REQUIRED",
                "verdict": "QUARANTINE",
                "requires_human_signoff": "qualified accountant must approve the rule set",
                "contract_ref": "FINANCE-TOOL-CONTRACTS.md 3.3 commerce_configure_tax_engine",
            },
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("commerce_configure_tax_engine", e)


async def commerce_get_budget_metrics(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE. Previously returned an invented monthly_limit of 1500.00
    under ok: true."""
    try:
        return provider_unconfigured(
            "commerce_get_budget_metrics",
            _BUDGET_STORE_KEYS,
            "No budget store is wired for the commerce agent. Budget figures are "
            "unavailable; none are invented. Absence of a limit is not headroom.",
            detail={"mode": MODE_READ_ONLY, "verdict": "QUARANTINE"},
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("commerce_get_budget_metrics", e)


async def commerce_get_audit_log(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE. An empty entries[] under ok: true reads as a clean audit log."""
    try:
        return provider_unconfigured(
            "commerce_get_audit_log",
            _BUDGET_STORE_KEYS,
            "No audit store is wired for the commerce agent. No entries can be "
            "returned; an empty log must not be inferred.",
            detail={"mode": MODE_READ_ONLY, "verdict": "QUARANTINE"},
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("commerce_get_audit_log", e)


async def commerce_get_alerts(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE. An empty alerts[] under ok: true reads as 'nothing flagged'."""
    try:
        return provider_unconfigured(
            "commerce_get_alerts",
            _BUDGET_STORE_KEYS,
            "No alert engine is wired for the commerce agent. No alerts can be "
            "returned; 'no alerts raised' must not be inferred.",
            detail={"mode": MODE_READ_ONLY, "verdict": "QUARANTINE"},
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("commerce_get_alerts", e)


# ---------------------------------------------------------------------------
# Schemas
#
# DELETED (PERMANENTLY_PROHIBITED, Section 6.1 Group A):
#   commerce_register_domain          (item 6)
#   commerce_activate_payment_gateway (item 7)
#   commerce_ship_fulfillment_batch   (item 8)
#   commerce_launch_commerce_stack    (item 9)
# Do not reintroduce. runtime/tool_policy.assert_prohibitions_intact() makes
# the process refuse to start if any of these names is registered again.
# ---------------------------------------------------------------------------

_SCHEMAS: dict[str, dict[str, Any]] = {
    "commerce_configure_tax_engine": {
        "type": "function",
        "function": {
            "name": "commerce_configure_tax_engine",
            "description": (
                "NOT IMPLEMENTED. Tax-engine configuration determines the tax on every "
                "future invoice and is quarantined pending a named provider and an "
                "accountant-approved rule set. Always returns ok=false with "
                "error.code=not_implemented. No tax configuration is created."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
    "commerce_get_budget_metrics": {
        "type": "function",
        "function": {
            "name": "commerce_get_budget_metrics",
            "description": (
                "Return the commerce agent's budget window from the budget store. No "
                "store is wired, so this always returns ok=false with "
                "error.code=provider_unconfigured. It never invents a limit."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
    "commerce_get_audit_log": {
        "type": "function",
        "function": {
            "name": "commerce_get_audit_log",
            "description": (
                "Return commerce audit entries from the audit store. No store is wired, "
                "so this always returns ok=false with error.code=provider_unconfigured. "
                "It never returns an empty log that could be read as 'clean'."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
    "commerce_get_alerts": {
        "type": "function",
        "function": {
            "name": "commerce_get_alerts",
            "description": (
                "Return commerce alerts from the alert engine. None is wired, so this "
                "always returns ok=false with error.code=provider_unconfigured. It "
                "never returns an empty list that could be read as 'nothing flagged'."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
}


def register() -> None:
    # PERMANENTLY_PROHIBITED names are absent by construction. Adding a
    # register_tool line for commerce_register_domain,
    # commerce_activate_payment_gateway, commerce_ship_fulfillment_batch or
    # commerce_launch_commerce_stack will make the gateway refuse to boot.
    register_tool("commerce_configure_tax_engine", commerce_configure_tax_engine, _SCHEMAS["commerce_configure_tax_engine"])
    register_tool("commerce_get_budget_metrics", commerce_get_budget_metrics, _SCHEMAS["commerce_get_budget_metrics"])
    register_tool("commerce_get_audit_log", commerce_get_audit_log, _SCHEMAS["commerce_get_audit_log"])
    register_tool("commerce_get_alerts", commerce_get_alerts, _SCHEMAS["commerce_get_alerts"])
