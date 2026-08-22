"""HTTP client for FinanceOS /internal/proofs/* (canonical InvoiceProof + VerifyAPI)."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse

import aiohttp

FINANCEOS_API_BASE_URL = os.environ.get("FINANCEOS_API_BASE_URL", "").strip().rstrip("/")
INTERNAL_PROOFS_SERVICE_TOKEN = os.environ.get("INTERNAL_PROOFS_SERVICE_TOKEN", "").strip()


def _base_url() -> str:
    return os.environ.get("FINANCEOS_API_BASE_URL", "").strip().rstrip("/")


def _service_token() -> str:
    return os.environ.get("INTERNAL_PROOFS_SERVICE_TOKEN", "").strip()


def financeos_proofs_configured() -> bool:
    base = _base_url()
    token = _service_token()
    return len(base) > 0 and len(token) >= 32 and _allowed_base_url(base)


def _allowed_base_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_service_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def post_invoice_proof(
    *,
    document_id: str,
    source_channel: str | None = None,
    intent_id: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    if not financeos_proofs_configured():
        return {
            "ok": False,
            "error": "financeos_proofs_not_configured",
            "reason": "set FINANCEOS_API_BASE_URL and INTERNAL_PROOFS_SERVICE_TOKEN (>=32 chars)",
        }
    body: dict[str, Any] = {"document_id": document_id}
    if source_channel:
        body["source_channel"] = source_channel
    if intent_id:
        body["intent_id"] = intent_id
    return await _post("/internal/proofs/invoice", body, session=session)


async def post_verify_api(
    *,
    entity_id: str,
    agent_slug: str,
    boundary_type: str = "agent_scope_map",
    operation: str | None = None,
    intent_id: str | None = None,
    intent_type: str | None = None,
    remediation_reason: str | None = None,
    phoenix_trace_id: str | None = None,
    handoff_digest: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    if not financeos_proofs_configured():
        return {
            "ok": False,
            "error": "financeos_proofs_not_configured",
            "reason": "set FINANCEOS_API_BASE_URL and INTERNAL_PROOFS_SERVICE_TOKEN (>=32 chars)",
        }
    body: dict[str, Any] = {
        "entity_id": entity_id,
        "agent_slug": agent_slug,
        "boundary_type": boundary_type,
    }
    if operation:
        body["operation"] = operation
    if intent_id:
        body["intent_id"] = intent_id
    if intent_type:
        body["intent_type"] = intent_type
    if remediation_reason:
        body["remediation_reason"] = remediation_reason
    if phoenix_trace_id:
        body["phoenix_trace_id"] = phoenix_trace_id
    if handoff_digest:
        body["handoff_digest"] = handoff_digest
    return await _post("/internal/proofs/verify", body, session=session)


async def get_invoice_proof_run(run_id: str, session: aiohttp.ClientSession | None = None) -> dict[str, Any]:
    if not financeos_proofs_configured():
        return {"ok": False, "error": "financeos_proofs_not_configured"}
    return await _get(f"/internal/proofs/invoice/{run_id}", session=session)


async def get_verify_api_run(run_id: str, session: aiohttp.ClientSession | None = None) -> dict[str, Any]:
    if not financeos_proofs_configured():
        return {"ok": False, "error": "financeos_proofs_not_configured"}
    return await _get(f"/internal/proofs/verify/{run_id}", session=session)


async def _post(path: str, body: dict[str, Any], session: aiohttp.ClientSession | None) -> dict[str, Any]:
    url = f"{_base_url()}{path}"
    owns = session is None
    if owns:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
    try:
        assert session is not None
        async with session.post(url, json=body, headers=_headers()) as resp:
            data = await _read_json(resp)
            if resp.status in (200, 201):
                return {"ok": True, "status": resp.status, **data}
            if resp.status == 422:
                return {"ok": False, "status": resp.status, "blocked": True, **data}
            return {"ok": False, "status": resp.status, **data}
    except aiohttp.ClientError as exc:
        return {"ok": False, "error": "financeos_transport_error", "reason": str(exc)}
    finally:
        if owns:
            await session.close()


async def _get(path: str, session: aiohttp.ClientSession | None) -> dict[str, Any]:
    url = f"{_base_url()}{path}"
    owns = session is None
    if owns:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
    try:
        assert session is not None
        async with session.get(url, headers=_headers()) as resp:
            data = await _read_json(resp)
            if resp.status == 200:
                return {"ok": True, "status": resp.status, **data}
            return {"ok": False, "status": resp.status, **data}
    except aiohttp.ClientError as exc:
        return {"ok": False, "error": "financeos_transport_error", "reason": str(exc)}
    finally:
        if owns:
            await session.close()


async def _read_json(resp: aiohttp.ClientResponse) -> dict[str, Any]:
    text = await resp.text()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"data": parsed}
    except json.JSONDecodeError:
        return {"raw": text}
