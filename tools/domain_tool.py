"""Genesis Domain tools.

Implements Sections 5 and 6 of docs/FINANCE-TOOL-CONTRACTS.md for the
finance-adjacent subset of this module.

What changed and why:

  * Three functions are PERMANENTLY_PROHIBITED (Section 6.1 Group A, items
    12-14) and are DELETED — bodies, schemas and register_tool lines:
      domain_create_intent_mandate  (constructs a spend mandate)
      domain_register               (buys a domain)
      domain_select_and_register    (composite ending in a purchase)
    Absence beats denial.
  * ``domain_get_cost_summary`` previously returned total_monthly_cost 0.00,
    total_domains 0 and threshold_exceeded false under ok: true — a fabricated
    all-clear identical in shape to a genuine one. It now returns
    provider_unconfigured.
  * The candidate generator and the Name.com availability check are real and
    keep working; they now carry Section 5.6 evidence and return
    provider_unconfigured instead of a fake success when credentials are absent.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from . import register_tool
from ._envelope import (
    CODE_UPSTREAM_TIMEOUT,
    MODE_READ_ONLY,
    canonical_json,
    failure,
    from_exception,
    not_implemented,
    provider_unconfigured,
    read_evidence,
    sha256_hex,
    success,
    validation_failed,
)

log = logging.getLogger(__name__)

# Environment variable NAMES only — never values (Section 5.4).
_NAMECOM_KEYS = ["NAMECOM_USERNAME", "NAMECOM_TOKEN"]
_REGISTRY_STORE_KEYS = ["GENESIS_JOB_DATABASE_URL", "DATABASE_URL"]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def domain_generate_candidates(
    *,
    theme: str | None = None,
    business_name: str | None = None,
    business_type: str | None = None,
    count: int = 10,
    **kwargs: Any,
) -> dict[str, Any]:
    """Generate scored domain-name candidates around a theme / business name.

    Heuristic: shorter root scores higher; hyphens penalised; popular TLDs
    (.com, .io) get a bonus. Deduplicated and sorted descending by score.
    """
    try:
        root_theme = (theme or business_name or "").strip().lower()
        if not root_theme:
            return validation_failed(
                "domain_generate_candidates",
                [{"field": "theme", "rule": "theme_or_business_name_required", "received_type": type(theme).__name__}],
                "Provide 'theme' or 'business_name'.",
            )
        # Strip non-alphanumeric chars from the root.
        cleaned = "".join(ch for ch in root_theme if ch.isalnum())
        if not cleaned:
            return validation_failed(
                "domain_generate_candidates",
                [{"field": "theme", "rule": "must_contain_alphanumeric", "received_type": "str"}],
                "Theme/business_name had no alphanumeric characters.",
            )

        prefixes = ["", "get", "my", "the", "use"]
        suffixes = ["", "hq", "io", "app", "ai", "co"]
        tlds = [".com", ".io", ".ai", ".co", ".dev", ".app"]

        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for p in prefixes:
            for s in suffixes:
                for tld in tlds:
                    root = f"{p}{cleaned}{s}"
                    domain = f"{root}{tld}"
                    if domain in seen:
                        continue
                    seen.add(domain)
                    score = 100 - len(root) - (10 if "-" in root else 0)
                    if tld in (".com", ".io"):
                        score += 5
                    candidates.append({
                        "domain": domain,
                        "score": score,
                        "tld": tld,
                        "length": len(root),
                        "memorable": len(root) <= 12,
                        "brandable": p == "" and s == "",
                    })
        candidates.sort(key=lambda c: -c["score"])
        top = candidates[: max(1, int(count))]
        result = {"theme": root_theme, "count": len(top), "candidates": top}
        return success(
            tool="domain_generate_candidates",
            mode=MODE_READ_ONLY,
            result=result,
            evidence=read_evidence(
                # Deterministic local computation over the caller's input.
                source="caller_supplied",
                query_fingerprint=sha256_hex(canonical_json({"theme": root_theme, "count": int(count)})),
                row_count=len(top),
                checksum=sha256_hex(canonical_json(result)),
            ),
        )
    except Exception as e:
        return from_exception("domain_generate_candidates", e)


async def domain_check_availability(
    *,
    domains: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """READ_ONLY batch availability lookup against Name.com v4.

    Returns provider_unconfigured — not a fake success — when NAMECOM_USERNAME
    or NAMECOM_TOKEN are absent. The previous body returned ok: true with a row
    of nulls per domain, which a caller could not distinguish from a real
    "no result" answer.
    """
    tool = "domain_check_availability"
    try:
        domain_list = domains or []
        if not isinstance(domain_list, list) or not domain_list:
            return validation_failed(
                tool,
                [{"field": "domains", "rule": "required_non_empty_list_1_50", "received_type": type(domains).__name__}],
                "Provide 'domains' (non-empty list of strings, 1-50 elements).",
            )
        if len(domain_list) > 50:
            return validation_failed(
                tool,
                [{"field": "domains", "rule": "max_50_elements", "received_type": "list"}],
            )

        username = os.getenv("NAMECOM_USERNAME")
        token = os.getenv("NAMECOM_TOKEN")
        if not (username and token):
            return provider_unconfigured(
                tool,
                _NAMECOM_KEYS,
                "Name.com credentials are not configured, so availability is unknown. "
                "No availability result is invented.",
                detail={"mode": MODE_READ_ONLY},
            )

        try:
            import httpx  # type: ignore
        except Exception as imp_err:
            return provider_unconfigured(
                tool,
                ["httpx"],
                "The httpx HTTP client is not installed in this deployment, so the "
                "Name.com lookup cannot be performed.",
                detail={"mode": MODE_READ_ONLY, "exception_type": type(imp_err).__name__},
            )

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.name.com/v4/domains:checkAvailability",
                    auth=(username, token),
                    json={"domainNames": domain_list},
                )
                # A 2xx is the only success. The previous `status_code < 400`
                # test conflated a 3xx with a completed lookup.
                if resp.status_code not in (200, 201):
                    return failure(
                        tool=tool,
                        code=CODE_UPSTREAM_TIMEOUT,
                        message="Name.com did not return a usable availability response.",
                        detail={"provider": "namecom", "status_code": resp.status_code, "state": "indeterminate"},
                        retry_after_ms=2000,
                    )
                data = resp.json()
                results = data.get("results")
                if not isinstance(results, list):
                    return failure(
                        tool=tool,
                        code=CODE_UPSTREAM_TIMEOUT,
                        message="Name.com returned a malformed availability body.",
                        detail={"provider": "namecom", "state": "indeterminate"},
                        retry_after_ms=2000,
                    )
                return success(
                    tool=tool,
                    mode=MODE_READ_ONLY,
                    result={"results": results, "count": len(results)},
                    evidence=read_evidence(
                        source="namecom",
                        query_fingerprint=sha256_hex(canonical_json({"domainNames": sorted(domain_list)})),
                        row_count=len(results),
                        checksum=sha256_hex(canonical_json(results)),
                        unverified=False,
                    ),
                )
        except Exception as e:
            return from_exception(
                tool,
                e,
                message="The Name.com availability lookup did not complete.",
                detail={"provider": "namecom", "state": "indeterminate"},
            )
    except Exception as e:
        return from_exception(tool, e)


# ---------------------------------------------------------------------------
# DELETED (PERMANENTLY_PROHIBITED, Section 6.1 Group A):
#   domain_create_intent_mandate  (item 12) — constructs a spend mandate
#   domain_register               (item 13) — buys a domain
#   domain_select_and_register    (item 14) — composite ending in a purchase
# Bodies, schemas and register_tool lines are gone. Do not reintroduce:
# runtime/tool_policy.assert_prohibitions_intact() makes the process refuse to
# start if any of these names is registered again.
# ---------------------------------------------------------------------------


async def domain_configure_dns(**kwargs: Any) -> dict[str, Any]:
    """Not implemented. The previous body returned status "configured" under
    ok: true having configured no DNS records at all."""
    try:
        return not_implemented(
            "domain_configure_dns",
            "DNS configuration is not implemented. It requires a credentialed "
            "registrar/DNS provider and post-write readback of the zone. No DNS "
            "record was created or changed.",
            detail={"mode": "APPROVAL_REQUIRED", "records_written": 0},
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("domain_configure_dns", e)


async def domain_get_cost_summary(**kwargs: Any) -> dict[str, Any]:
    """QUARANTINE (contract 3.5).

    Previously returned total_monthly_cost 0.00, total_domains 0,
    threshold_exceeded false and registered_domains [] under ok: true — a
    fabricated all-clear identical in shape to a genuine one. A caller could
    not distinguish "no domains are costing anything" from "nothing was
    checked". No registry store is wired, so the truthful answer is that the
    figures are unavailable.
    """
    try:
        return provider_unconfigured(
            "domain_get_cost_summary",
            _REGISTRY_STORE_KEYS,
            "No domain registry store is wired. Domain cost figures are "
            "unavailable; a zero cost and an empty domain list are not inferred.",
            detail={"mode": MODE_READ_ONLY, "verdict": "QUARANTINE"},
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("domain_get_cost_summary", e)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_SCHEMAS: dict[str, dict[str, Any]] = {
    "domain_generate_candidates": {
        "type": "function",
        "function": {
            "name": "domain_generate_candidates",
            "description": (
                "Generate scored domain-name candidates around a theme or business name. "
                "Returns a ranked list with score/tld/length/memorable/brandable per candidate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "theme": {"type": "string", "description": "Theme or seed root for candidates."},
                    "business_name": {"type": "string", "description": "Business name (alternate to theme)."},
                    "business_type": {"type": "string", "description": "Business type / industry hint."},
                    "count": {"type": "integer", "default": 10},
                },
                "additionalProperties": True,
            },
        },
    },
    "domain_check_availability": {
        "type": "function",
        "function": {
            "name": "domain_check_availability",
            "description": (
                "Batch-check domain availability via Name.com when NAMECOM_USERNAME+NAMECOM_TOKEN "
                "env vars are set; returns a scaffold response otherwise."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of fully-qualified domain names to check.",
                    },
                },
                "required": ["domains"],
                "additionalProperties": True,
            },
        },
    },
    "domain_configure_dns": {
        "type": "function",
        "function": {
            "name": "domain_configure_dns",
            "description": "NOT IMPLEMENTED. Writing DNS records requires a credentialed registrar/DNS provider and zone readback. Always returns ok=false with error.code=not_implemented.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "records": {
                        "type": "array",
                        "items": {"type": "object", "additionalProperties": True},
                    },
                },
                "required": ["domain"],
                "additionalProperties": True,
            },
        },
    },
    "domain_get_cost_summary": {
        "type": "function",
        "function": {
            "name": "domain_get_cost_summary",
            "description": "Return domain cost totals from the registry store. No store is wired, so this always returns ok=false with error.code=provider_unconfigured. It never reports a zero cost or an empty domain list as fact.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    },
}


def register() -> None:
    # PERMANENTLY_PROHIBITED names are absent by construction. Adding a
    # register_tool line for domain_create_intent_mandate, domain_register or
    # domain_select_and_register will make the gateway refuse to boot.
    register_tool("domain_generate_candidates", domain_generate_candidates, _SCHEMAS["domain_generate_candidates"])
    register_tool("domain_check_availability", domain_check_availability, _SCHEMAS["domain_check_availability"])
    register_tool("domain_configure_dns", domain_configure_dns, _SCHEMAS["domain_configure_dns"])
    register_tool("domain_get_cost_summary", domain_get_cost_summary, _SCHEMAS["domain_get_cost_summary"])
