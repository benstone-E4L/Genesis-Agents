"""Xero scope map — Genesis-side mirror of Cato xero_scope (same YAML)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

SCOPE_MAP_PATH = Path(__file__).resolve().parent / "XERO_SCOPE_TO_AGENT_MAP.yaml"

OPERATION_SCOPE_FAMILY: dict[str, str] = {
    "create_draft_bill": "accounting.contacts",
    "create_draft_invoice": "accounting.invoices",
    "create_draft_manual_journal": "accounting.manualjournals",
    "create_bank_transaction": "accounting.banktransactions",
    "attach_file_to_bill": "accounting.attachments",
    "attach_file_to_invoice": "accounting.attachments",
    "get_trial_balance": "accounting.reports.trialbalance.read",
    "get_balance_sheet": "accounting.reports.balancesheet.read",
    "get_profit_and_loss": "accounting.reports.profitandloss.read",
    "list_open_payables": "accounting.contacts.read",
    "list_open_receivables": "accounting.contacts.read",
    "get_chart_of_accounts": "accounting.settings.read",
    "get_bank_summary": "accounting.reports.banksummary.read",
}

OPERATION_PRIMARY_AGENTS: dict[str, frozenset[str]] = {
    "create_draft_bill": frozenset({"genesis-e4l-ap"}),
    "create_draft_invoice": frozenset({"genesis-e4l-ar", "genesis-e4l-revenue", "genesis-e4l-shopify"}),
    "create_draft_manual_journal": frozenset({"genesis-e4l-journals", "genesis-e4l-intercompany", "genesis-e4l-close"}),
    "create_bank_transaction": frozenset({"genesis-e4l-cash", "genesis-e4l-treasury", "genesis-e4l-stripe"}),
    "attach_file_to_bill": frozenset({"genesis-e4l-ap"}),
    "attach_file_to_invoice": frozenset({"genesis-e4l-ar"}),
}

E4L_PREFIX = "genesis-e4l-"


@lru_cache(maxsize=1)
def load_scope_map() -> dict[str, Any]:
    if not SCOPE_MAP_PATH.is_file():
        return {"_meta": {}, "scopes": {}, "specialist_overrides": {}}
    with SCOPE_MAP_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    validate_scope_map_structure(data)
    return data


def validate_scope_map_structure(data: dict[str, Any]) -> None:
    """Raise ValueError when YAML scopes are flat (broken indentation)."""
    scopes = data.get("scopes") or {}
    if not isinstance(scopes, dict):
        raise ValueError("scope map scopes must be a mapping")
    for key, entry in scopes.items():
        if key in ("primary_write", "read", "cato_remediation", "policy_note"):
            raise ValueError(
                f"scope map YAML malformed: '{key}' is a top-level scopes key — "
                "indent primary_write/read under each scope family"
            )
        if entry is None:
            raise ValueError(f"scope map entry for {key!r} is null — check YAML indentation")
        if not isinstance(entry, dict):
            raise ValueError(f"scope map entry for {key!r} must be a mapping, got {type(entry).__name__}")


def _expand_read(entry: dict[str, Any]) -> list[str]:
    raw = entry.get("read") or []
    if raw == "all_specialists":
        return list((load_scope_map().get("_meta") or {}).get("specialists") or [])
    return list(raw)


def _primary_write(scope_key: str) -> list[str]:
    entry = (load_scope_map().get("scopes") or {}).get(scope_key) or {}
    return list(entry.get("primary_write") or [])


def writes_forbidden(agent_slug: str) -> bool:
    ov = (load_scope_map().get("specialist_overrides") or {}).get(agent_slug) or {}
    return bool(ov.get("writes_forbidden"))


def operation_allowed(agent_slug: str, operation: str) -> tuple[bool, str]:
    if not agent_slug.startswith(E4L_PREFIX):
        return False, "not_e4l_specialist"
    if writes_forbidden(agent_slug):
        fam = OPERATION_SCOPE_FAMILY.get(operation, "")
        if fam.endswith(".read") or operation.startswith(("get_", "list_")):
            return True, "read_only"
        return False, "write_forbidden"
    primary = OPERATION_PRIMARY_AGENTS.get(operation)
    if primary is not None:
        if agent_slug in primary:
            return True, "primary_write"
        return False, f"operation_denied:{operation}"
    fam = OPERATION_SCOPE_FAMILY.get(operation)
    if not fam:
        return False, "unknown_operation"
    if fam.endswith(".read"):
        entry = (load_scope_map().get("scopes") or {}).get(fam) or {}
        return agent_slug in _expand_read(entry), "read_scope"
    return agent_slug in _primary_write(fam), "primary_write"


def augment_tools_advertised(agent_slug: str, tools: list[str]) -> list[str]:
    """Add xero_scoped_invoke for E4L specialists unless writes forbidden without reads."""
    out = list(tools)
    if not agent_slug.startswith(E4L_PREFIX):
        return out
    if "xero_scoped_invoke" not in out:
        out.append("xero_scoped_invoke")
    for name in (
        "financeos_invoice_proof",
        "financeos_verify_api",
        "financeos_get_invoice_proof",
        "financeos_get_verify_api",
    ):
        if name not in out:
            out.append(name)
    return out
