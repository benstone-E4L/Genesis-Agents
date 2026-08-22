"""FinanceOS proof-chain tools — InvoiceProof and VerifyAPI before Xero writes."""

from __future__ import annotations

from typing import Any

from accounting.financeos_proofs_client import (
    get_invoice_proof_run,
    get_verify_api_run,
    post_invoice_proof,
    post_verify_api,
)


def _trusted_agent_slug(kwargs: dict[str, Any], agent_slug: str) -> str:
    for key in ("_parent_agent_slug", "_agent_slug"):
        trusted = kwargs.get(key)
        if isinstance(trusted, str) and trusted.strip():
            return trusted.strip()
    return (agent_slug or "").strip()


async def financeos_invoice_proof(
    *,
    document_id: str,
    source_channel: str = "",
    intent_id: str = "",
    agent_slug: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Run FinanceOS InvoiceProof for a document. Returns run_id for downstream steps."""
    _ = _trusted_agent_slug(kwargs, agent_slug)
    result = await post_invoice_proof(
        document_id=document_id,
        source_channel=source_channel or None,
        intent_id=intent_id or None,
    )
    if not result.get("ok"):
        return result
    return {
        "ok": result.get("verdict") == "PASS" and not result.get("blocked"),
        "invoice_proof_run_id": result.get("run_id"),
        "verdict": result.get("verdict"),
        "blocked": bool(result.get("blocked")),
        "missing_fields": result.get("missing_fields", []),
        "reason_codes": result.get("reason_codes", []),
    }


async def financeos_verify_api(
    *,
    entity_id: str,
    operation: str,
    agent_slug: str = "",
    intent_id: str = "",
    intent_type: str = "",
    boundary_type: str = "agent_scope_map",
    remediation_reason: str = "",
    phoenix_trace_id: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Run FinanceOS VerifyAPI. Returns verify_api_run_id for xero_scoped_invoke."""
    slug = _trusted_agent_slug(kwargs, agent_slug)
    if not slug:
        return {"ok": False, "error": "agent_slug_required"}
    result = await post_verify_api(
        entity_id=entity_id,
        agent_slug=slug,
        boundary_type=boundary_type,
        operation=operation or None,
        intent_id=intent_id or None,
        intent_type=intent_type or None,
        remediation_reason=remediation_reason or None,
        phoenix_trace_id=phoenix_trace_id or None,
    )
    if not result.get("ok"):
        return result
    return {
        "ok": result.get("verdict") == "PASS" and not result.get("blocked"),
        "verify_api_run_id": result.get("run_id"),
        "verdict": result.get("verdict"),
        "blocked": bool(result.get("blocked")),
        "blocking_reason": result.get("blocking_reason"),
        "allowed_tools": result.get("allowed_tools", []),
    }


async def financeos_get_invoice_proof(*, run_id: str, **kwargs: Any) -> dict[str, Any]:
    _ = kwargs
    return await get_invoice_proof_run(run_id)


async def financeos_get_verify_api(*, run_id: str, **kwargs: Any) -> dict[str, Any]:
    _ = kwargs
    return await get_verify_api_run(run_id)


INVOICE_PROOF_SCHEMA: dict[str, Any] = {
    "name": "financeos_invoice_proof",
    "description": "Run FinanceOS InvoiceProof on a document before AP/AR posting.",
    "parameters": {
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "FinanceOS documents.id UUID"},
            "source_channel": {"type": "string"},
            "intent_id": {"type": "string"},
        },
        "required": ["document_id"],
    },
}

VERIFY_API_SCHEMA: dict[str, Any] = {
    "name": "financeos_verify_api",
    "description": (
        "Run FinanceOS VerifyAPI before xero_scoped_invoke. "
        "Pass verify_api_run_id from the response into xero_scoped_invoke."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string", "description": "FinanceOS entities.id UUID"},
            "operation": {"type": "string", "description": "e.g. create_draft_bill"},
            "intent_id": {"type": "string"},
            "intent_type": {"type": "string"},
            "boundary_type": {"type": "string"},
            "remediation_reason": {"type": "string"},
        },
        "required": ["entity_id", "operation"],
    },
}

GET_INVOICE_SCHEMA: dict[str, Any] = {
    "name": "financeos_get_invoice_proof",
    "description": "Read a FinanceOS InvoiceProof run by id.",
    "parameters": {
        "type": "object",
        "properties": {"run_id": {"type": "string"}},
        "required": ["run_id"],
    },
}

GET_VERIFY_SCHEMA: dict[str, Any] = {
    "name": "financeos_get_verify_api",
    "description": "Read a FinanceOS VerifyAPI run by id.",
    "parameters": {
        "type": "object",
        "properties": {"run_id": {"type": "string"}},
        "required": ["run_id"],
    },
}


def register() -> None:
    from tools import register_tool

    register_tool("financeos_invoice_proof", financeos_invoice_proof, INVOICE_PROOF_SCHEMA)
    register_tool("financeos_verify_api", financeos_verify_api, VERIFY_API_SCHEMA)
    register_tool("financeos_get_invoice_proof", financeos_get_invoice_proof, GET_INVOICE_SCHEMA)
    register_tool("financeos_get_verify_api", financeos_get_verify_api, GET_VERIFY_SCHEMA)
