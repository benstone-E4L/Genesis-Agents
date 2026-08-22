"""Scoped Xero MCP invoke — domain specialists post after VerifyAPI gate."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from accounting.xero_scope import operation_allowed

XERO_MCP_BRIDGE_URL = os.environ.get("XERO_MCP_BRIDGE_URL", "").strip()
XERO_MCP_BRIDGE_TOKEN = os.environ.get("XERO_MCP_BRIDGE_TOKEN", "").strip()

_VERIFY_API_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_IDEMPOTENCY_FILE = "xero_scoped_idempotency.json"


def _trusted_agent_slug(kwargs: dict[str, Any], agent_slug: str) -> str:
    """Runtime-injected slug wins over model-supplied agent_slug."""
    for key in ("_parent_agent_slug", "_agent_slug"):
        trusted = kwargs.get(key)
        if isinstance(trusted, str) and trusted.strip():
            return trusted.strip()
    return (agent_slug or "").strip()


def _validate_verify_api_run_id(value: str) -> tuple[bool, str]:
    raw = str(value or "").strip()
    if not raw:
        return False, "empty"
    if not _VERIFY_API_RUN_ID_RE.fullmatch(raw):
        return False, "invalid_format"
    return True, raw


def _allowed_bridge_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False
    if parsed.username or parsed.password:
        return False
    return True


def _dispatch_allowed_operations(kwargs: dict[str, Any]) -> list[str] | None:
    raw = kwargs.get("_allowed_xero_operations")
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    return [str(op) for op in raw if isinstance(op, str) and op]


def _idempotency_key(verify_id: str, operation: str, slug: str) -> str:
    return f"{slug}:{operation}:{verify_id}"


def _check_idempotency(job_dir: Path | None, key: str) -> bool:
    if job_dir is None:
        return False
    path = job_dir / _IDEMPOTENCY_FILE
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return isinstance(data, dict) and key in data


def _record_idempotency(job_dir: Path | None, key: str) -> None:
    if job_dir is None:
        return
    path = job_dir / _IDEMPOTENCY_FILE
    try:
        data: dict[str, Any] = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
        data[key] = True
        path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    except OSError:
        return


async def xero_scoped_invoke(
    *,
    operation: str,
    payload: dict[str, Any] | None = None,
    entity_key: str = "",
    verify_api_run_id: str = "",
    agent_slug: str = "",
    _job_dir: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Invoke one scoped Xero operation. Requires verify_api_run_id from Cato/FinanceOS."""
    trusted_slug = _trusted_agent_slug(kwargs, agent_slug)
    if not trusted_slug:
        return {
            "ok": False,
            "error": "agent_slug_required",
            "reason": "runtime agent slug missing",
        }
    if agent_slug and agent_slug.strip() and agent_slug.strip() != trusted_slug:
        return {
            "ok": False,
            "error": "agent_slug_mismatch",
            "reason": "model-supplied agent_slug does not match runtime specialist",
            "trusted_slug": trusted_slug,
        }

    ok_id, verify_id = _validate_verify_api_run_id(verify_api_run_id)
    if not ok_id:
        return {
            "ok": False,
            "error": "verify_api_required",
            "reason": f"verify_api_run_id {verify_id}",
        }

    idem_key = _idempotency_key(verify_id, operation, trusted_slug)
    if _check_idempotency(_job_dir, idem_key):
        return {
            "ok": False,
            "error": "duplicate_verify_api_run",
            "reason": "this verify_api_run_id already executed for this operation in this job",
            "operation": operation,
        }

    dispatch_allowlist = _dispatch_allowed_operations(kwargs)
    if dispatch_allowlist is not None and operation not in dispatch_allowlist:
        return {
            "ok": False,
            "error": "dispatch_scope_denied",
            "reason": "operation not in Cato-injected allowed_xero_operations",
            "operation": operation,
        }

    allowed, reason = operation_allowed(trusted_slug, operation)
    if not allowed:
        return {"ok": False, "error": "scope_forbidden", "reason": reason, "operation": operation}

    execution_realm = kwargs.get("_execution_realm") or kwargs.get("execution_realm") or "demo_mcp"

    # Dry-run / test mode when bridge not configured
    if not XERO_MCP_BRIDGE_URL:
        _record_idempotency(_job_dir, idem_key)
        return {
            "ok": True,
            "dry_run": True,
            "simulated": True,
            "operation": operation,
            "entity_key": entity_key,
            "executor": trusted_slug,
            "execution_realm": execution_realm,
            "verify_api_run_id": verify_id,
            "receipt": {
                "xero_resource_id": f"dry-run:{operation}:{verify_id[:8]}",
                "read_back_pending": True,
                "simulated": True,
            },
        }

    if not _allowed_bridge_url(XERO_MCP_BRIDGE_URL):
        return {
            "ok": False,
            "error": "xero_bridge_url_invalid",
            "reason": "XERO_MCP_BRIDGE_URL must be http(s) with no embedded credentials",
        }

    headers = {"Content-Type": "application/json"}
    if XERO_MCP_BRIDGE_TOKEN:
        headers["Authorization"] = f"Bearer {XERO_MCP_BRIDGE_TOKEN}"
    body = {
        "operation": operation,
        "entity_key": entity_key,
        "payload": payload or {},
        "agent_slug": trusted_slug,
        "verify_api_run_id": verify_id,
        "execution_realm": execution_realm,
        "scope_map_version": kwargs.get("_scope_map_version"),
    }
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(XERO_MCP_BRIDGE_URL, json=body, headers=headers) as resp:
            text = await resp.text()
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError:
                data = {"raw": text}
            if resp.status >= 400:
                return {
                    "ok": False,
                    "error": "xero_bridge_error",
                    "status": resp.status,
                    "body": data,
                }
            _record_idempotency(_job_dir, idem_key)
            return {
                "ok": True,
                "dry_run": False,
                "operation": operation,
                "executor": trusted_slug,
                "execution_realm": execution_realm,
                "verify_api_run_id": verify_id,
                "bridge_response": data,
            }


XERO_SCOPED_SCHEMA: dict[str, Any] = {
    "name": "xero_scoped_invoke",
    "description": (
        "Invoke a scoped Xero MCP operation for this specialist. "
        "Requires verify_api_run_id from proof chain. "
        "Routine domain posts only — not for controller remediation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {"type": "string"},
            "payload": {"type": "object"},
            "entity_key": {"type": "string"},
            "verify_api_run_id": {"type": "string"},
        },
        "required": ["operation", "verify_api_run_id"],
    },
}


def register() -> None:
    from tools import register_tool

    register_tool("xero_scoped_invoke", xero_scoped_invoke, XERO_SCOPED_SCHEMA)
