"""Phase 6 - Escrow client for AP2 payments integration.

Calls SwarmSync's /payments/ap2/* endpoints from the gateway.
Handles escrow initiate -> complete -> release lifecycle.

Env vars:
  SWARMSYNC_API_INTERNAL_URL - base URL of swarmsync-api (default: http://localhost:3000)
  INTERNAL_SECRET - shared secret for callback-style auth between gateway and api
  X402_PLATFORM_WALLET_ADDRESS - treasury wallet for 10% platform fee
  SWARMSYNC_PLATFORM_FEE_PCT - platform fee (default 0.10)
"""
from __future__ import annotations
import logging
import os
from typing import Any
import httpx

log = logging.getLogger(__name__)

API_URL = os.getenv("SWARMSYNC_API_INTERNAL_URL", "http://localhost:3000")
INTERNAL_SECRET = os.getenv("INTERNAL_SECRET", "")
TREASURY_WALLET = os.getenv("X402_PLATFORM_WALLET_ADDRESS", "")
PLATFORM_FEE_PCT = float(os.getenv("SWARMSYNC_PLATFORM_FEE_PCT", "0.10"))


async def initiate_escrow(
    *,
    source_wallet_id: str,
    destination_wallet_id: str,
    amount_cents: int,
    memo: str | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Hold buyer funds in escrow before agent execution."""
    if not INTERNAL_SECRET:
        return {"ok": False, "error": "internal_secret_not_configured"}

    payload = {
        "sourceWalletId": source_wallet_id,
        "destinationWalletId": destination_wallet_id,
        "amount": amount_cents / 100.0,
        "purpose": "AGENT_HIRE",
        "memo": memo or "",
        "metadata": metadata or {},
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{API_URL}/payments/ap2/initiate",
                json=payload,
                headers={"x-internal-secret": INTERNAL_SECRET},
            )
            if resp.status_code not in (200, 201):
                return {
                    "ok": False,
                    "error": "initiate_failed",
                    "status": resp.status_code,
                    "body": resp.text[:500],
                }
            data = resp.json()
            return {
                "ok": True,
                "escrow_id": data.get("escrowId") or data.get("id"),
                "raw": data,
            }
    except Exception as e:
        log.exception("escrow initiate failed")
        return {
            "ok": False,
            "error": "exception",
            "type": type(e).__name__,
            "message": str(e),
        }


async def complete_escrow(
    *,
    escrow_id: str,
    status: str = "SETTLED",
    failure_reason: str | None = None,
) -> dict[str, Any]:
    """Mark escrow complete. status: SETTLED | AUTHORIZED | FAILED."""
    if not INTERNAL_SECRET:
        return {"ok": False, "error": "internal_secret_not_configured"}

    payload: dict[str, Any] = {"escrowId": escrow_id, "status": status}
    if failure_reason:
        payload["failureReason"] = failure_reason
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{API_URL}/payments/ap2/complete",
                json=payload,
                headers={"x-internal-secret": INTERNAL_SECRET},
            )
            if resp.status_code not in (200, 201):
                return {
                    "ok": False,
                    "error": "complete_failed",
                    "status": resp.status_code,
                    "body": resp.text[:500],
                }
            return {"ok": True, "raw": resp.json()}
    except Exception as e:
        log.exception("escrow complete failed")
        return {
            "ok": False,
            "error": "exception",
            "type": type(e).__name__,
            "message": str(e),
        }


async def release_escrow(
    *,
    escrow_id: str,
    reason: str = "agent_failure",
) -> dict[str, Any]:
    """Refund the buyer."""
    if not INTERNAL_SECRET:
        return {"ok": False, "error": "internal_secret_not_configured"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{API_URL}/payments/ap2/release",
                json={"escrowId": escrow_id, "reason": reason},
                headers={"x-internal-secret": INTERNAL_SECRET},
            )
            if resp.status_code not in (200, 201):
                return {
                    "ok": False,
                    "error": "release_failed",
                    "status": resp.status_code,
                    "body": resp.text[:500],
                }
            return {"ok": True, "raw": resp.json()}
    except Exception as e:
        log.exception("escrow release failed")
        return {
            "ok": False,
            "error": "exception",
            "type": type(e).__name__,
            "message": str(e),
        }


def calculate_split(
    total_cents: int,
    fee_pct_override: float | None = None,
) -> dict[str, Any]:
    """Compute the platform-fee skim. Returns {total, platform_fee, agent_net}."""
    pct = fee_pct_override if fee_pct_override is not None else PLATFORM_FEE_PCT
    platform_fee = int(total_cents * pct)
    return {
        "total_cents": total_cents,
        "platform_fee_cents": platform_fee,
        "agent_net_cents": total_cents - platform_fee,
        "platform_fee_pct": pct,
        "treasury_wallet": TREASURY_WALLET,
    }
