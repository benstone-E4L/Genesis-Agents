"""Genesis Finance tools.

Implements Sections 4, 5 and 6 of docs/FINANCE-TOOL-CONTRACTS.md.

Governing rule (Section 0): automation may PREPARE, a human PAYS, automation
RECORDS. No function in this module constructs or transmits a payment.

What changed and why:

  * ``finance_run_payroll_batch``, ``finance_process_vendor_invoice`` and
    ``finance_run_finance_close`` are PERMANENTLY_PROHIBITED (Section 6.1
    Group A, items 1-3). Bodies, schemas and register_tool lines are DELETED.
    Absence beats denial: deleted code cannot be re-enabled by a config change.
  * Every remaining action returns the Section 5 truthful-failure envelope.
    ok: true is unreachable without evidence.
  * ``finance_generate_finance_report`` is the one tool the contract says
    IMPLEMENT. It now uses integer minor units throughout, an explicit
    ``category`` field instead of the first word of the description, and a
    mandatory explicit currency with mixed-currency rejection.
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

_TX_TYPES = frozenset({"income", "expense", "transfer"})
_REPORT_SOURCES = frozenset({"caller_supplied", "xero_export"})


# ---------------------------------------------------------------------------
# Validation helpers (Section 4 money representation rules)
# ---------------------------------------------------------------------------

def _is_int(value: Any) -> bool:
    """True for a real integer. bool is a subclass of int and is not money."""
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_currency(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 3 and value.isalpha() and value.isupper()


def _valid_period(value: Any) -> bool:
    """YYYY-MM or YYYY-MM-DD/YYYY-MM-DD."""
    if not isinstance(value, str):
        return False
    if len(value) == 7 and value[4] == "-" and value[:4].isdigit() and value[5:].isdigit():
        return True
    if len(value) == 21 and value[10] == "/":
        return _valid_date(value[:10]) and _valid_date(value[11:])
    return False


def _valid_date(value: Any) -> bool:
    """RFC3339 date prefix YYYY-MM-DD."""
    if not isinstance(value, str) or len(value) < 10:
        return False
    head = value[:10]
    return (
        head[4] == "-"
        and head[7] == "-"
        and head[:4].isdigit()
        and head[5:7].isdigit()
        and head[8:10].isdigit()
    )


def _basis_points(numerator: int, denominator: int) -> int:
    """Integer basis points, ROUND_HALF_EVEN. Never a rounded float percentage."""
    if denominator == 0:
        return 0
    value = (Decimal(numerator) * Decimal(10000)) / Decimal(denominator)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def finance_sync_bank_fees(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE (contract 3.1). APPROVAL_REQUIRED — writes a journal entry to
    the system of record and must reconcile to a named bank statement line.

    Not implementable today: no Xero provider integration, no readback evidence
    and no idempotency store. The previous body returned status "reconciled"
    under ok: true having reconciled nothing.
    """
    try:
        return not_implemented(
            "finance_sync_bank_fees",
            "Recording bank fees to the general ledger is not implemented. It "
            "requires a credentialed Xero Accounting API integration "
            "(ManualJournals + BankTransactions), a bank_statement_line_id to "
            "reconcile against, post-write readback evidence and a live "
            "idempotency store. No ledger entry was created.",
            detail={
                "mode": "APPROVAL_REQUIRED",
                "verdict": "QUARANTINE",
                "contract_ref": "FINANCE-TOOL-CONTRACTS.md 3.1 finance_sync_bank_fees",
            },
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("finance_sync_bank_fees", e)


async def finance_generate_finance_report(
    *,
    transactions: list[dict[str, Any]] | None = None,
    currency: str | None = None,
    period: str | None = None,
    source: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """READ_ONLY P&L computed from caller-supplied transactions.

    Integer minor units throughout (Section 4). No float arithmetic touches a
    monetary value. Mixed currencies are rejected, never silently summed.
    Expenses group by an explicit ``category`` field — the previous
    ``description.split()[0]`` heuristic was not an accounting category.

    Evidence declares ``unverified: true`` for caller-supplied data: this is
    arithmetic, not reconciliation, and says so machine-readably.
    """
    tool = "finance_generate_finance_report"
    try:
        violations: list[dict[str, Any]] = []

        if not isinstance(transactions, list):
            violations.append(
                {"field": "transactions", "rule": "required_list", "received_type": type(transactions).__name__}
            )
        if not _valid_currency(currency):
            violations.append(
                {"field": "currency", "rule": "required_iso4217_uppercase_3", "received_type": type(currency).__name__}
            )
        if not _valid_period(period):
            violations.append(
                {"field": "period", "rule": "required_YYYY-MM_or_YYYY-MM-DD/YYYY-MM-DD", "received_type": type(period).__name__}
            )
        if source not in _REPORT_SOURCES:
            violations.append(
                {"field": "source", "rule": f"required_enum_{sorted(_REPORT_SOURCES)}", "received_type": type(source).__name__}
            )
        if violations:
            return validation_failed(tool, violations)

        txs: list[dict[str, Any]] = transactions or []
        for idx, tx in enumerate(txs):
            if not isinstance(tx, dict):
                violations.append({"field": f"transactions[{idx}]", "rule": "must_be_object", "received_type": type(tx).__name__})
                continue
            if not _valid_date(tx.get("date")):
                violations.append({"field": f"transactions[{idx}].date", "rule": "rfc3339_date", "received_type": type(tx.get("date")).__name__})
            desc = tx.get("description")
            if not isinstance(desc, str) or not (1 <= len(desc) <= 255):
                violations.append({"field": f"transactions[{idx}].description", "rule": "str_1_255", "received_type": type(desc).__name__})
            amt = tx.get("amount_minor")
            if not _is_int(amt) or amt < 0:
                # Received value deliberately omitted: it is money.
                violations.append({"field": f"transactions[{idx}].amount_minor", "rule": "int_minor_units_gte_0", "received_type": type(amt).__name__})
            tx_type = tx.get("type")
            if tx_type not in _TX_TYPES:
                violations.append({"field": f"transactions[{idx}].type", "rule": f"enum_{sorted(_TX_TYPES)}", "received_type": type(tx_type).__name__})
            tx_ccy = tx.get("currency")
            if not _valid_currency(tx_ccy):
                violations.append({"field": f"transactions[{idx}].currency", "rule": "required_iso4217_uppercase_3", "received_type": type(tx_ccy).__name__})
            elif tx_ccy != currency:
                # Section 4 rule 4: no cross-currency arithmetic without an
                # explicit FX rate, date and source. Refuse rather than sum.
                violations.append({"field": f"transactions[{idx}].currency", "rule": "must_equal_report_currency", "received_type": "str"})
            if tx_type == "expense":
                cat = tx.get("category")
                if not isinstance(cat, str) or not (1 <= len(cat) <= 64):
                    violations.append({"field": f"transactions[{idx}].category", "rule": "required_str_1_64_for_expense", "received_type": type(cat).__name__})

        # Partial acceptance is forbidden: any bad element fails the whole call.
        if violations:
            return validation_failed(tool, violations)

        total_income_minor = 0
        total_expenses_minor = 0
        expense_by_category: dict[str, int] = defaultdict(int)
        monthly_income: dict[str, int] = defaultdict(int)
        monthly_expenses: dict[str, int] = defaultdict(int)
        expense_items: list[dict[str, Any]] = []

        for tx in txs:
            amount_minor = int(tx["amount_minor"])
            month_key = str(tx["date"])[:7]
            if tx["type"] == "income":
                total_income_minor += amount_minor
                monthly_income[month_key] += amount_minor
            elif tx["type"] == "expense":
                total_expenses_minor += amount_minor
                monthly_expenses[month_key] += amount_minor
                expense_by_category[tx["category"]] += amount_minor
                expense_items.append(
                    {
                        "description": tx["description"],
                        "category": tx["category"],
                        "amount_minor": amount_minor,
                        "date": tx["date"],
                    }
                )
            # transfers move money between own accounts: excluded from P&L.

        net_profit_minor = total_income_minor - total_expenses_minor
        all_months = sorted(set(monthly_income) | set(monthly_expenses))

        report = {
            "period": period,
            "currency": currency,
            "source": source,
            "total_income_minor": total_income_minor,
            "total_expenses_minor": total_expenses_minor,
            # Sign is meaningful on this field alone: a loss is negative.
            "net_profit_minor": net_profit_minor,
            "profit_margin_bp": _basis_points(net_profit_minor, total_income_minor),
            "expense_breakdown_minor": dict(sorted(expense_by_category.items())),
            "monthly_breakdown": [
                {
                    "month": m,
                    "income_minor": monthly_income.get(m, 0),
                    "expenses_minor": monthly_expenses.get(m, 0),
                    "net_minor": monthly_income.get(m, 0) - monthly_expenses.get(m, 0),
                }
                for m in all_months
            ],
            "top_expenses": sorted(expense_items, key=lambda x: -x["amount_minor"])[:5],
            "transaction_count": len(txs),
            "generated_at": now_rfc3339(),
        }

        # Checksum excludes generated_at so a caller can detect real drift
        # rather than clock movement.
        checksum_body = {k: v for k, v in report.items() if k != "generated_at"}
        report["result_checksum"] = sha256_hex(checksum_body)

        return success(
            tool=tool,
            mode=MODE_READ_ONLY,
            result={"report": report},
            evidence=read_evidence(
                source=source or "caller_supplied",
                query_fingerprint=sha256_hex(
                    canonical_json({"transactions": txs, "currency": currency, "period": period})
                ),
                row_count=len(txs),
                checksum=report["result_checksum"],
            ),
        )
    except Exception as e:
        return from_exception(tool, e)


async def finance_import_x402_transactions(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE (contract 3.1). APPROVAL_REQUIRED — changes a financial record.

    The previous body returned ``"imported": len(txs)`` having inserted nothing.
    Section 5.5 rule 4 forbids deriving rows_affected from len(input): the count
    must come from the system that performed the write.
    """
    try:
        return not_implemented(
            "finance_import_x402_transactions",
            "Importing x402 ledger entries is not implemented. It requires a real "
            "ledger table with a unique index on (entity_id, external_txn_id), a "
            "caller-supplied batch checksum the tool recomputes, and post-insert "
            "readback evidence. No rows were inserted and no row count is reported.",
            detail={
                "mode": "APPROVAL_REQUIRED",
                "verdict": "QUARANTINE",
                "rows_inserted": 0,
                "contract_ref": "FINANCE-TOOL-CONTRACTS.md 3.1 finance_import_x402_transactions",
            },
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("finance_import_x402_transactions", e)


async def finance_get_budget_metrics(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE. Previously returned an invented monthly_limit of 15000.00
    under ok: true. An accounting runtime reading that concludes headroom
    exists. No budget store is wired."""
    try:
        return provider_unconfigured(
            "finance_get_budget_metrics",
            _BUDGET_STORE_KEYS,
            "No budget store is wired for the finance agent. Budget figures are "
            "unavailable; none are invented. Absence of a limit is not headroom.",
            detail={"mode": MODE_READ_ONLY, "verdict": "QUARANTINE"},
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("finance_get_budget_metrics", e)


async def finance_get_audit_log(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE. An empty entries[] under ok: true reads as a clean audit log."""
    try:
        return provider_unconfigured(
            "finance_get_audit_log",
            _BUDGET_STORE_KEYS,
            "No audit store is wired for the finance agent. No entries can be "
            "returned; an empty log must not be inferred.",
            detail={"mode": MODE_READ_ONLY, "verdict": "QUARANTINE"},
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("finance_get_audit_log", e)


async def finance_get_alerts(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE. An empty alerts[] under ok: true reads as 'nothing flagged'."""
    try:
        return provider_unconfigured(
            "finance_get_alerts",
            _BUDGET_STORE_KEYS,
            "No alert engine is wired for the finance agent. No alerts can be "
            "returned; 'no alerts raised' must not be inferred.",
            detail={"mode": MODE_READ_ONLY, "verdict": "QUARANTINE"},
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("finance_get_alerts", e)


# ---------------------------------------------------------------------------
# Schemas
#
# DELETED (PERMANENTLY_PROHIBITED, Section 6.1 Group A):
#   finance_run_payroll_batch        (item 1)  — payroll disbursement
#   finance_process_vendor_invoice   (item 2)  — scheduling a disbursement
#   finance_run_finance_close        (item 3)  — composite whose first stage is payroll
# Do not reintroduce. runtime/tool_policy.assert_prohibitions_intact() makes
# the process refuse to start if any of these names is registered again.
# ---------------------------------------------------------------------------

_SCHEMAS: dict[str, dict[str, Any]] = {
    "finance_sync_bank_fees": {
        "type": "function",
        "function": {
            "name": "finance_sync_bank_fees",
            "description": (
                "NOT IMPLEMENTED. Recording bank fees to the general ledger is "
                "quarantined pending a credentialed Xero integration, statement-line "
                "reconciliation and readback evidence. Always returns ok=false with "
                "error.code=not_implemented. No ledger entry is created."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
    "finance_generate_finance_report": {
        "type": "function",
        "function": {
            "name": "finance_generate_finance_report",
            "description": (
                "Compute a P&L from a caller-supplied transaction list. All money is "
                "integer minor units (amount_minor); floats are rejected. currency, "
                "period and source are mandatory and have no defaults. Mixed "
                "currencies are rejected, never summed. Expenses group by an explicit "
                "category field. Returns ok=true with evidence marked unverified for "
                "caller-supplied data — this is arithmetic, not reconciliation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "transactions": {
                        "type": "array",
                        "description": "Transaction objects. An empty list is permitted but must be explicit.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "date": {"type": "string", "description": "RFC3339 date, YYYY-MM-DD prefix."},
                                "description": {"type": "string", "description": "1-255 characters."},
                                "amount_minor": {"type": "integer", "description": "Unsigned integer in the currency's minor unit."},
                                "type": {"type": "string", "enum": ["income", "expense", "transfer"]},
                                "currency": {"type": "string", "description": "ISO-4217 uppercase; must equal the report currency."},
                                "category": {"type": "string", "description": "Accounting category. Required when type is 'expense'."},
                            },
                            "required": ["date", "description", "amount_minor", "type", "currency"],
                        },
                    },
                    "currency": {"type": "string", "description": "ISO-4217 uppercase 3 letters. Mandatory, no default."},
                    "period": {"type": "string", "description": "YYYY-MM or YYYY-MM-DD/YYYY-MM-DD."},
                    "source": {"type": "string", "enum": ["caller_supplied", "xero_export"]},
                },
                "required": ["transactions", "currency", "period", "source"],
                "additionalProperties": True,
            },
        },
    },
    "finance_import_x402_transactions": {
        "type": "function",
        "function": {
            "name": "finance_import_x402_transactions",
            "description": (
                "NOT IMPLEMENTED. Bulk-importing x402 ledger entries is quarantined "
                "pending a real ledger table with a uniqueness index and post-insert "
                "readback. Always returns ok=false with error.code=not_implemented "
                "and rows_inserted 0. It never reports len(input) as a row count."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
    "finance_get_budget_metrics": {
        "type": "function",
        "function": {
            "name": "finance_get_budget_metrics",
            "description": (
                "Return the finance agent's budget window from the budget store. No "
                "store is wired, so this always returns ok=false with "
                "error.code=provider_unconfigured. It never invents a limit."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
    "finance_get_audit_log": {
        "type": "function",
        "function": {
            "name": "finance_get_audit_log",
            "description": (
                "Return finance audit entries from the audit store. No store is wired, "
                "so this always returns ok=false with error.code=provider_unconfigured. "
                "It never returns an empty log that could be read as 'clean'."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
    "finance_get_alerts": {
        "type": "function",
        "function": {
            "name": "finance_get_alerts",
            "description": (
                "Return finance alerts from the alert engine. None is wired, so this "
                "always returns ok=false with error.code=provider_unconfigured. It "
                "never returns an empty list that could be read as 'nothing flagged'."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
}


def register() -> None:
    # PERMANENTLY_PROHIBITED names are absent by construction. Adding a
    # register_tool line for finance_run_payroll_batch,
    # finance_process_vendor_invoice or finance_run_finance_close will make the
    # gateway refuse to boot.
    register_tool("finance_sync_bank_fees", finance_sync_bank_fees, _SCHEMAS["finance_sync_bank_fees"])
    register_tool("finance_generate_finance_report", finance_generate_finance_report, _SCHEMAS["finance_generate_finance_report"])
    register_tool("finance_import_x402_transactions", finance_import_x402_transactions, _SCHEMAS["finance_import_x402_transactions"])
    register_tool("finance_get_budget_metrics", finance_get_budget_metrics, _SCHEMAS["finance_get_budget_metrics"])
    register_tool("finance_get_audit_log", finance_get_audit_log, _SCHEMAS["finance_get_audit_log"])
    register_tool("finance_get_alerts", finance_get_alerts, _SCHEMAS["finance_get_alerts"])
