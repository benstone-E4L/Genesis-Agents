"""Genesis Billing tools.

Implements Sections 4, 5 and 6 of docs/FINANCE-TOOL-CONTRACTS.md.

What changed and why:

  * ``billing_run_dunning_batch`` and ``billing_run_billing_cycle`` are
    PERMANENTLY_PROHIBITED (Section 6.1 Group A, items 4-5). Bodies, schemas
    and register_tool lines are DELETED. "Retry" against an overdue invoice
    means re-presenting a stored payment instrument, which is constructing and
    transmitting a card charge; the cycle bundles that behind one approval.
  * Every remaining action returns the Section 5 truthful-failure envelope.
    ok: true is unreachable without evidence.
  * ``billing_generate_revops_report`` is implemented per the contract:
    integer minor units, and annualisation driven by an explicit ``interval``
    field rather than string-matching the plan name.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from . import register_tool
from ._envelope import (
    MODE_READ_ONLY,
    canonical_json,
    from_exception,
    not_implemented,
    now_rfc3339,
    provider_unconfigured,
    read_evidence,
    sha256_hex,
    success,
    validation_failed,
)

log = logging.getLogger(__name__)

# Environment variable NAMES only — never values (Section 5.4).
_BUDGET_STORE_KEYS = ["GENESIS_JOB_DATABASE_URL", "DATABASE_URL"]

_SUB_STATUSES = frozenset({"active", "trial", "cancelled"})
_SUB_INTERVALS = frozenset({"month", "year"})


def _is_int(value: Any) -> bool:
    """True for a real integer. bool is a subclass of int and is not money."""
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_currency(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 3 and value.isalpha() and value.isupper()


def _allocate(total_minor: int, parts: int) -> list[int]:
    """Largest-remainder allocation. The parts sum EXACTLY to total_minor.

    Pure integer arithmetic, so no truncation and no float rounding error.
    The remainder is carried by the earliest buckets, deterministically.
    """
    if parts <= 0:
        raise ValueError("parts must be positive")
    base, remainder = divmod(total_minor, parts)
    out = [base] * parts
    for i in range(remainder):
        out[i] += 1
    return out


def _basis_points(numerator: int, denominator: int) -> int:
    if denominator == 0:
        return 0
    value = (Decimal(numerator) * Decimal(10000)) / Decimal(denominator)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def _divide_half_even(numerator: int, denominator: int) -> int:
    if denominator == 0:
        return 0
    value = Decimal(numerator) / Decimal(denominator)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def billing_import_ar_ledger(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE (contract 3.2). APPROVAL_REQUIRED — writes a financial record set.

    The previous body returned status "imported" with a caller-supplied record
    count under ok: true, having imported nothing.
    """
    try:
        return not_implemented(
            "billing_import_ar_ledger",
            "Importing an accounts-receivable ledger is not implemented. It "
            "requires named provider credentials, a real destination table, a "
            "caller-asserted expected_record_count checked against the actual "
            "fetched count, and post-write readback evidence. No rows were "
            "imported and no row count is reported.",
            detail={
                "mode": "APPROVAL_REQUIRED",
                "verdict": "QUARANTINE",
                "rows_inserted": 0,
                "contract_ref": "FINANCE-TOOL-CONTRACTS.md 3.2 billing_import_ar_ledger",
            },
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("billing_import_ar_ledger", e)


async def billing_deploy_plan_change(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE (contract 3.2). APPROVAL_REQUIRED — alters a price and can
    issue an invoice."""
    try:
        return not_implemented(
            "billing_deploy_plan_change",
            "Changing a customer's billing plan is not implemented. It requires a "
            "named provider chosen before build (not at call time), an explicit "
            "subscription_id, a current_plan_id optimistic-concurrency guard, "
            "proration semantics verified against the provider's documented "
            "behaviour, readback evidence and a per-call human authorization. "
            "No subscription was modified.",
            detail={
                "mode": "APPROVAL_REQUIRED",
                "verdict": "QUARANTINE",
                "contract_ref": "FINANCE-TOOL-CONTRACTS.md 3.2 billing_deploy_plan_change",
            },
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("billing_deploy_plan_change", e)


async def billing_generate_revops_report(
    *,
    subscriptions: list[dict[str, Any]] | None = None,
    currency: str | None = None,
    period: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """READ_ONLY RevOps summary computed from caller-supplied subscriptions.

    Integer minor units throughout. Annualisation comes from an explicit
    ``interval`` field, never from string-matching the plan name: the previous
    heuristic silently divided "Enterprise Yearly Support (billed monthly)" by
    twelve and left "Pro-12" undivided. Annual-to-monthly amortisation uses
    largest-remainder allocation so twelve months sum exactly to the annual
    amount.
    """
    tool = "billing_generate_revops_report"
    try:
        violations: list[dict[str, Any]] = []
        if not isinstance(subscriptions, list):
            violations.append({"field": "subscriptions", "rule": "required_list", "received_type": type(subscriptions).__name__})
        if not _valid_currency(currency):
            violations.append({"field": "currency", "rule": "required_iso4217_uppercase_3", "received_type": type(currency).__name__})
        if not isinstance(period, str) or not period:
            violations.append({"field": "period", "rule": "required_non_empty_str", "received_type": type(period).__name__})
        if violations:
            return validation_failed(tool, violations)

        subs: list[dict[str, Any]] = subscriptions or []
        for idx, sub in enumerate(subs):
            if not isinstance(sub, dict):
                violations.append({"field": f"subscriptions[{idx}]", "rule": "must_be_object", "received_type": type(sub).__name__})
                continue
            plan = sub.get("plan")
            if not isinstance(plan, str) or not (1 <= len(plan) <= 128):
                violations.append({"field": f"subscriptions[{idx}].plan", "rule": "str_1_128", "received_type": type(plan).__name__})
            amt = sub.get("amount_minor")
            if not _is_int(amt) or amt < 0:
                violations.append({"field": f"subscriptions[{idx}].amount_minor", "rule": "int_minor_units_gte_0", "received_type": type(amt).__name__})
            if sub.get("status") not in _SUB_STATUSES:
                violations.append({"field": f"subscriptions[{idx}].status", "rule": f"enum_{sorted(_SUB_STATUSES)}", "received_type": type(sub.get("status")).__name__})
            if sub.get("interval") not in _SUB_INTERVALS:
                violations.append({"field": f"subscriptions[{idx}].interval", "rule": f"enum_{sorted(_SUB_INTERVALS)}", "received_type": type(sub.get("interval")).__name__})
            sub_ccy = sub.get("currency")
            if not _valid_currency(sub_ccy):
                violations.append({"field": f"subscriptions[{idx}].currency", "rule": "required_iso4217_uppercase_3", "received_type": type(sub_ccy).__name__})
            elif sub_ccy != currency:
                violations.append({"field": f"subscriptions[{idx}].currency", "rule": "must_equal_report_currency", "received_type": "str"})

        if violations:
            return validation_failed(tool, violations)

        mrr_minor = 0
        arr_minor = 0
        active_count = 0
        trial_count = 0
        churned_count = 0
        plan_groups: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "mrr_minor": 0})

        for sub in subs:
            plan = sub["plan"]
            amount_minor = int(sub["amount_minor"])
            if sub["interval"] == "year":
                monthly_minor = _allocate(amount_minor, 12)[0]
                annual_minor = amount_minor
            else:
                monthly_minor = amount_minor
                annual_minor = amount_minor * 12

            plan_groups[plan]["count"] += 1
            if sub["status"] == "active":
                active_count += 1
                mrr_minor += monthly_minor
                arr_minor += annual_minor
                plan_groups[plan]["mrr_minor"] += monthly_minor
            elif sub["status"] == "trial":
                trial_count += 1
            elif sub["status"] == "cancelled":
                churned_count += 1

        report = {
            "period": period,
            "currency": currency,
            "source": "caller_supplied",
            "mrr_minor": mrr_minor,
            "arr_minor": arr_minor,
            "active_subscriptions": active_count,
            "trial_subscriptions": trial_count,
            "churned_subscriptions": churned_count,
            "churn_rate_bp": _basis_points(churned_count, active_count + churned_count),
            "plan_breakdown": {
                plan: {"count": v["count"], "mrr_minor": v["mrr_minor"]}
                for plan, v in sorted(plan_groups.items())
            },
            "arpa_minor": _divide_half_even(mrr_minor, active_count),
            "subscription_count": len(subs),
            "generated_at": now_rfc3339(),
        }
        checksum_body = {k: v for k, v in report.items() if k != "generated_at"}
        report["result_checksum"] = sha256_hex(checksum_body)

        return success(
            tool=tool,
            mode=MODE_READ_ONLY,
            result={"report": report},
            evidence=read_evidence(
                source="caller_supplied",
                query_fingerprint=sha256_hex(
                    canonical_json({"subscriptions": subs, "currency": currency, "period": period})
                ),
                row_count=len(subs),
                checksum=report["result_checksum"],
            ),
        )
    except Exception as e:
        return from_exception(tool, e)


async def billing_get_budget_metrics(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE. Previously returned an invented monthly_limit of 1500.00
    under ok: true."""
    try:
        return provider_unconfigured(
            "billing_get_budget_metrics",
            _BUDGET_STORE_KEYS,
            "No budget store is wired for the billing agent. Budget figures are "
            "unavailable; none are invented. Absence of a limit is not headroom.",
            detail={"mode": MODE_READ_ONLY, "verdict": "QUARANTINE"},
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("billing_get_budget_metrics", e)


async def billing_get_audit_log(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE. An empty entries[] under ok: true reads as a clean audit log."""
    try:
        return provider_unconfigured(
            "billing_get_audit_log",
            _BUDGET_STORE_KEYS,
            "No audit store is wired for the billing agent. No entries can be "
            "returned; an empty log must not be inferred.",
            detail={"mode": MODE_READ_ONLY, "verdict": "QUARANTINE"},
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("billing_get_audit_log", e)


async def billing_get_alerts(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE. An empty alerts[] under ok: true reads as 'nothing flagged'."""
    try:
        return provider_unconfigured(
            "billing_get_alerts",
            _BUDGET_STORE_KEYS,
            "No alert engine is wired for the billing agent. No alerts can be "
            "returned; 'no alerts raised' must not be inferred.",
            detail={"mode": MODE_READ_ONLY, "verdict": "QUARANTINE"},
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("billing_get_alerts", e)


# ---------------------------------------------------------------------------
# Schemas
#
# DELETED (PERMANENTLY_PROHIBITED, Section 6.1 Group A):
#   billing_run_dunning_batch  (item 4) — charge retries against overdue invoices
#   billing_run_billing_cycle  (item 5) — bundles dunning + plan change behind one approval
# Do not reintroduce. runtime/tool_policy.assert_prohibitions_intact() makes
# the process refuse to start if any of these names is registered again.
# ---------------------------------------------------------------------------

_SCHEMAS: dict[str, dict[str, Any]] = {
    "billing_import_ar_ledger": {
        "type": "function",
        "function": {
            "name": "billing_import_ar_ledger",
            "description": (
                "NOT IMPLEMENTED. Importing an AR ledger is quarantined pending named "
                "provider credentials, a real destination table and readback evidence. "
                "Always returns ok=false with error.code=not_implemented and "
                "rows_inserted 0."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
    "billing_deploy_plan_change": {
        "type": "function",
        "function": {
            "name": "billing_deploy_plan_change",
            "description": (
                "NOT IMPLEMENTED. Changing a customer's billing plan alters a price and "
                "can issue an invoice; it is quarantined pending a named provider, "
                "verified proration semantics, readback evidence and per-call human "
                "authorization. Always returns ok=false with error.code=not_implemented."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
    "billing_generate_revops_report": {
        "type": "function",
        "function": {
            "name": "billing_generate_revops_report",
            "description": (
                "Compute MRR, ARR, churn and ARPA from a caller-supplied subscription "
                "list. All money is integer minor units (amount_minor); floats are "
                "rejected. currency and period are mandatory. Annualisation uses the "
                "explicit interval field ('month' or 'year'), never the plan name. "
                "Returns ok=true with evidence marked unverified for caller-supplied "
                "data — this is arithmetic, not reconciliation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subscriptions": {
                        "type": "array",
                        "description": "Subscription objects. An empty list is permitted but must be explicit.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "plan": {"type": "string"},
                                "amount_minor": {"type": "integer", "description": "Unsigned integer in the currency's minor unit, for one interval."},
                                "interval": {"type": "string", "enum": ["month", "year"]},
                                "status": {"type": "string", "enum": ["active", "trial", "cancelled"]},
                                "currency": {"type": "string", "description": "ISO-4217 uppercase; must equal the report currency."},
                            },
                            "required": ["plan", "amount_minor", "interval", "status", "currency"],
                        },
                    },
                    "currency": {"type": "string", "description": "ISO-4217 uppercase 3 letters. Mandatory, no default."},
                    "period": {"type": "string"},
                },
                "required": ["subscriptions", "currency", "period"],
                "additionalProperties": True,
            },
        },
    },
    "billing_get_budget_metrics": {
        "type": "function",
        "function": {
            "name": "billing_get_budget_metrics",
            "description": (
                "Return the billing agent's budget window from the budget store. No "
                "store is wired, so this always returns ok=false with "
                "error.code=provider_unconfigured. It never invents a limit."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
    "billing_get_audit_log": {
        "type": "function",
        "function": {
            "name": "billing_get_audit_log",
            "description": (
                "Return billing audit entries from the audit store. No store is wired, "
                "so this always returns ok=false with error.code=provider_unconfigured. "
                "It never returns an empty log that could be read as 'clean'."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
    "billing_get_alerts": {
        "type": "function",
        "function": {
            "name": "billing_get_alerts",
            "description": (
                "Return billing alerts from the alert engine. None is wired, so this "
                "always returns ok=false with error.code=provider_unconfigured. It "
                "never returns an empty list that could be read as 'nothing flagged'."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
}


def register() -> None:
    # PERMANENTLY_PROHIBITED names are absent by construction. Adding a
    # register_tool line for billing_run_dunning_batch or
    # billing_run_billing_cycle will make the gateway refuse to boot.
    register_tool("billing_import_ar_ledger", billing_import_ar_ledger, _SCHEMAS["billing_import_ar_ledger"])
    register_tool("billing_deploy_plan_change", billing_deploy_plan_change, _SCHEMAS["billing_deploy_plan_change"])
    register_tool("billing_generate_revops_report", billing_generate_revops_report, _SCHEMAS["billing_generate_revops_report"])
    register_tool("billing_get_budget_metrics", billing_get_budget_metrics, _SCHEMAS["billing_get_budget_metrics"])
    register_tool("billing_get_audit_log", billing_get_audit_log, _SCHEMAS["billing_get_audit_log"])
    register_tool("billing_get_alerts", billing_get_alerts, _SCHEMAS["billing_get_alerts"])
