"""Truthful-failure envelope for Genesis money-domain tools.

Implements Section 5 of docs/FINANCE-TOOL-CONTRACTS.md.

The single rule this module exists to enforce (Section 5.7):

    A tool may return ok: true ONLY when it can name the system it changed or
    read, the identifier of the object it changed or the fingerprint of the
    query it ran, and a readback or checksum proving it. In every other
    circumstance it returns ok: false.

Structural guarantees provided here, not by convention:

  * ``ok`` is the sole discriminator. ``result`` exists iff ``ok`` is true,
    ``error`` exists iff ``ok`` is false. Never both, never neither.
  * The keys ``stub``, ``scaffold`` and ``note`` are stripped from every
    envelope, recursively, so no caller can be told "success, but".
  * ``success()`` raises unless a non-empty evidence object is supplied. A tool
    that has no evidence structurally cannot report success.
  * ``error.retryable`` is derived from ``error.code`` by a fixed table and is
    never chosen per call site.

Pure standard library on purpose: this module must import in a stripped
environment so the prohibition tests can run without Genesis dependencies.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

CONTRACT_VERSION = "1.0.0"

# --- Section 5.4 error taxonomy -------------------------------------------
CODE_NOT_IMPLEMENTED = "not_implemented"
CODE_PROVIDER_UNCONFIGURED = "provider_unconfigured"
CODE_UPSTREAM_TIMEOUT = "upstream_timeout"
CODE_VALIDATION_FAILED = "validation_failed"
CODE_DUPLICATE_REQUEST = "duplicate_request"
CODE_AUTHORIZATION_MISSING = "authorization_missing"
CODE_POLICY_DENIED = "policy_denied"

# Retryability is a property of the code alone (Section 5.4). This table is the
# only place it is decided.
RETRYABLE_BY_CODE: dict[str, bool] = {
    CODE_NOT_IMPLEMENTED: False,
    CODE_PROVIDER_UNCONFIGURED: False,
    CODE_UPSTREAM_TIMEOUT: True,
    CODE_VALIDATION_FAILED: False,
    CODE_DUPLICATE_REQUEST: True,
    CODE_AUTHORIZATION_MISSING: False,
    CODE_POLICY_DENIED: False,
}

ERROR_CODES: frozenset[str] = frozenset(RETRYABLE_BY_CODE)

# Section 5.3 rule 3. These keys are what let a stub masquerade as a success.
BANNED_RESPONSE_KEYS: frozenset[str] = frozenset({"stub", "scaffold", "note"})

MODE_READ_ONLY = "READ_ONLY"
MODE_PROPOSE_ONLY = "PROPOSE_ONLY"
MODE_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
MODES: frozenset[str] = frozenset({MODE_READ_ONLY, MODE_PROPOSE_ONLY, MODE_APPROVAL_REQUIRED})

SOURCE_CALLER_SUPPLIED = "caller_supplied"


# --- helpers ---------------------------------------------------------------

def now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(obj: Any) -> str:
    """UTF-8, keys sorted lexicographically, no insignificant whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(obj: Any) -> str:
    payload = obj if isinstance(obj, str) else canonical_json(obj)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _strip_banned(value: Any) -> Any:
    """Recursively remove BANNED_RESPONSE_KEYS so they cannot reach a caller."""
    if isinstance(value, dict):
        return {k: _strip_banned(v) for k, v in value.items() if k not in BANNED_RESPONSE_KEYS}
    if isinstance(value, list):
        return [_strip_banned(v) for v in value]
    return value


def _request_id() -> str:
    return str(uuid.uuid4())


# --- Section 5.2 failure envelope ------------------------------------------

def failure(
    *,
    tool: str,
    code: str,
    message: str,
    detail: dict[str, Any] | None = None,
    retry_after_ms: int | None = None,
) -> dict[str, Any]:
    """Build the Section 5.2 failure envelope. ``retryable`` is derived, never passed."""
    if code not in RETRYABLE_BY_CODE:
        raise ValueError(f"unknown error code: {code!r}")
    retryable = RETRYABLE_BY_CODE[code]
    if not retryable:
        # A non-retryable code must never carry a retry hint.
        retry_after_ms = None
    return _strip_banned(
        {
            "ok": False,
            "tool": tool,
            "contract_version": CONTRACT_VERSION,
            "request_id": _request_id(),
            "error": {
                "code": code,
                "retryable": retryable,
                "message": message,
                "detail": dict(detail or {}),
                "retry_after_ms": retry_after_ms,
            },
        }
    )


def not_implemented(tool: str, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """The tool exists but the operation is not built. Terminal and permanent."""
    return failure(tool=tool, code=CODE_NOT_IMPLEMENTED, message=message, detail=detail)


def provider_unconfigured(
    tool: str,
    missing_keys: list[str],
    message: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A required credential or endpoint is absent.

    ``missing_keys`` holds environment variable NAMES ONLY — never values.
    """
    merged = dict(detail or {})
    merged["missing_keys"] = sorted(str(k) for k in missing_keys)
    return failure(tool=tool, code=CODE_PROVIDER_UNCONFIGURED, message=message, detail=merged)


def validation_failed(
    tool: str,
    violations: list[dict[str, Any]],
    message: str = "Input violated the tool contract.",
) -> dict[str, Any]:
    """``violations`` is a list of {field, rule, received_type}.

    Received VALUES are deliberately omitted for money and identifiers.
    """
    return failure(
        tool=tool,
        code=CODE_VALIDATION_FAILED,
        message=message,
        detail={"violations": list(violations)},
    )


def from_exception(
    tool: str,
    exc: BaseException,
    *,
    code: str = CODE_UPSTREAM_TIMEOUT,
    message: str | None = None,
    detail: dict[str, Any] | None = None,
    retry_after_ms: int | None = 2000,
) -> dict[str, Any]:
    """Map an exception onto the taxonomy.

    Section 5.3 rule 6: the exception class name is NOT the error code, and
    ``str(exc)`` is not echoed to the caller because it may carry connection
    strings or credential fragments. The type goes in detail.exception_type and
    the message is tool-authored.
    """
    merged = dict(detail or {})
    merged["exception_type"] = type(exc).__name__
    return failure(
        tool=tool,
        code=code,
        message=message or f"{tool} failed with an unhandled internal error.",
        detail=merged,
        retry_after_ms=retry_after_ms,
    )


# --- Section 5.1 success envelope ------------------------------------------

def success(
    *,
    tool: str,
    mode: str,
    result: dict[str, Any],
    evidence: dict[str, Any],
    idempotency_key: str | None = None,
    duplicate: bool = False,
) -> dict[str, Any]:
    """Build the Section 5.1 success envelope.

    Raises ValueError when evidence is missing or empty. That refusal is the
    structural guarantee of Section 5.3 rule 4: a stub has no evidence, so a
    stub cannot construct a success.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode!r}")
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError(
            f"{tool}: ok=true requires a non-empty evidence object "
            "(FINANCE-TOOL-CONTRACTS.md Section 5.3 rule 4)"
        )
    if not isinstance(result, dict):
        raise ValueError(f"{tool}: result must be an object")
    if mode == MODE_APPROVAL_REQUIRED and evidence.get("readback_matches") is not True:
        # Section 5.5 rule 3: an unverifiable write is never reported as success.
        raise ValueError(
            f"{tool}: ok=true from an APPROVAL_REQUIRED tool requires readback_matches=true"
        )
    return _strip_banned(
        {
            "ok": True,
            "tool": tool,
            "contract_version": CONTRACT_VERSION,
            "request_id": _request_id(),
            "mode": mode,
            "idempotency_key": idempotency_key,
            "duplicate": bool(duplicate),
            "result": result,
            "evidence": evidence,
        }
    )


def read_evidence(
    *,
    source: str,
    query_fingerprint: str,
    row_count: int,
    checksum: str,
    as_of: str | None = None,
    unverified: bool | None = None,
) -> dict[str, Any]:
    """Section 5.6 evidence for READ_ONLY tools.

    ``unverified`` is forced true for caller-supplied data: arithmetic over data
    the caller handed in is not reconciliation and must say so machine-readably.
    """
    observed = now_rfc3339()
    if unverified is None:
        unverified = source == SOURCE_CALLER_SUPPLIED
    if source == SOURCE_CALLER_SUPPLIED:
        unverified = True
    return {
        "source": source,
        "unverified": bool(unverified),
        "query_fingerprint": query_fingerprint,
        "row_count": int(row_count),
        "checksum": checksum,
        "as_of": as_of or observed,
        "observed_at": observed,
    }


# --- Section 6.4 prohibited-call refusal ------------------------------------

PROHIBITION_MESSAGE = (
    "This operation is permanently prohibited. "
    "Automation may prepare; a human pays; automation records."
)


def prohibited_refusal(
    tool: str,
    *,
    group: str = "A",
    agent_slug: str | None = None,
) -> dict[str, Any]:
    """The fixed Section 6.4 envelope. Carries no execution."""
    return failure(
        tool=tool,
        code=CODE_POLICY_DENIED,
        message=PROHIBITION_MESSAGE,
        detail={
            "risk_class": "prohibited",
            "agent_slug": agent_slug,
            "prohibition_group": group,
            "remediation": (
                "Escalate to a named human approver. "
                "This decision is not overridable by configuration."
            ),
        },
    )


__all__ = [
    "CONTRACT_VERSION",
    "CODE_NOT_IMPLEMENTED",
    "CODE_PROVIDER_UNCONFIGURED",
    "CODE_UPSTREAM_TIMEOUT",
    "CODE_VALIDATION_FAILED",
    "CODE_DUPLICATE_REQUEST",
    "CODE_AUTHORIZATION_MISSING",
    "CODE_POLICY_DENIED",
    "ERROR_CODES",
    "RETRYABLE_BY_CODE",
    "BANNED_RESPONSE_KEYS",
    "MODE_READ_ONLY",
    "MODE_PROPOSE_ONLY",
    "MODE_APPROVAL_REQUIRED",
    "MODES",
    "SOURCE_CALLER_SUPPLIED",
    "PROHIBITION_MESSAGE",
    "canonical_json",
    "sha256_hex",
    "now_rfc3339",
    "failure",
    "not_implemented",
    "provider_unconfigured",
    "validation_failed",
    "from_exception",
    "success",
    "read_evidence",
    "prohibited_refusal",
]
