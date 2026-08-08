"""Workflow automation agent tools - Zapier/n8n/Make exports and webhook trigger.

Phase 5: zapier and n8n export heuristically generate importable JSON; make_export is a scaffold;
webhook_trigger is functional but capped at 10s and only fires against https:// URLs.
"""
from __future__ import annotations
import ipaddress
import logging
import socket as _socket
from typing import Any

from . import register_tool

log = logging.getLogger(__name__)


def _is_safe_url(url: str) -> bool:
    """Return False if the URL resolves to a private/loopback/link-local address."""
    try:
        from urllib.parse import urlparse
        hostname = urlparse(url).hostname
        if not hostname:
            return False
        for blocked in ("localhost", "metadata.google.internal", "169.254.169.254"):
            if hostname.lower() == blocked:
                return False
        addrs = _socket.getaddrinfo(hostname, None)
        for *_, sockaddr in addrs:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return True
    except Exception:
        return False  # On resolution failure, block by default


_PHASE9_NOTE = "Phase 9 integration pending"


def _guess_action_app(description: str) -> str:
    desc = (description or "").lower()
    if any(k in desc for k in ("slack", "channel", "dm")):
        return "Slack"
    if any(k in desc for k in ("email", "gmail", "outlook")):
        return "Gmail"
    if any(k in desc for k in ("notion", "page")):
        return "Notion"
    if any(k in desc for k in ("sheets", "spreadsheet", "google sheet")):
        return "Google Sheets"
    if any(k in desc for k in ("airtable", "base")):
        return "Airtable"
    if any(k in desc for k in ("hubspot", "crm", "salesforce")):
        return "HubSpot"
    return "Slack"


def _guess_action_verb(description: str) -> str:
    desc = (description or "").lower()
    if any(k in desc for k in ("notify", "send", "alert", "message", "post")):
        return "send_message"
    if any(k in desc for k in ("create", "add", "insert", "new")):
        return "create_record"
    if any(k in desc for k in ("update", "modify", "change")):
        return "update_record"
    if any(k in desc for k in ("delete", "remove")):
        return "delete_record"
    return "send_message"


async def workflow_zapier_export(*, workflow_description: str, **kwargs: Any) -> dict[str, Any]:
    try:
        action_app = _guess_action_app(workflow_description)
        action_verb = _guess_action_verb(workflow_description)
        name = f"Auto-generated: {workflow_description[:60]}"
        return {
            "ok": True,
            "platform": "zapier",
            "exported_workflow": {
                "name": name,
                "trigger": {
                    "type": "webhook",
                    "config": {"url_placeholder": "https://hooks.zapier.com/hooks/catch/<id>/<token>/"},
                },
                "actions": [
                    {
                        "type": "filter",
                        "config": {"condition_placeholder": "only_continue_if: <field> <op> <value>"},
                    },
                    {
                        "type": "action",
                        "config": {
                            "app_placeholder": action_app,
                            "action_placeholder": action_verb,
                            "fields_placeholder": {"<field_a>": "<value_a>", "<field_b>": "<value_b>"},
                        },
                    },
                ],
                "notes": "Heuristically generated. Import into Zapier and customize the trigger/action details.",
            },
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


async def workflow_n8n_export(*, workflow_description: str, **kwargs: Any) -> dict[str, Any]:
    try:
        action_app = _guess_action_app(workflow_description)
        action_verb = _guess_action_verb(workflow_description)
        name = f"Auto-generated: {workflow_description[:60]}"
        # n8n's exchange format is a `nodes` array + `connections` map.
        nodes = [
            {
                "parameters": {"path": "auto-generated", "httpMethod": "POST"},
                "name": "Webhook",
                "type": "n8n-nodes-base.webhook",
                "typeVersion": 1,
                "position": [240, 300],
            },
            {
                "parameters": {
                    "conditions": {
                        "string": [{"value1": "={{$json.field}}", "operation": "isNotEmpty"}]
                    }
                },
                "name": "IF",
                "type": "n8n-nodes-base.if",
                "typeVersion": 1,
                "position": [460, 300],
            },
            {
                "parameters": {
                    "app_placeholder": action_app,
                    "operation_placeholder": action_verb,
                    "fields_placeholder": {"<field_a>": "<value_a>"},
                },
                "name": action_app,
                "type": f"n8n-nodes-base.{action_app.lower().replace(' ', '')}",
                "typeVersion": 1,
                "position": [680, 300],
            },
        ]
        connections = {
            "Webhook": {"main": [[{"node": "IF", "type": "main", "index": 0}]]},
            "IF": {"main": [[{"node": action_app, "type": "main", "index": 0}]]},
        }
        return {
            "ok": True,
            "platform": "n8n",
            "exported_workflow": {
                "name": name,
                "nodes": nodes,
                "connections": connections,
                "active": False,
                "settings": {},
                "notes": "Heuristically generated. Import into n8n and customize node credentials/parameters.",
            },
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


async def workflow_make_export(*, workflow_description: str, **kwargs: Any) -> dict[str, Any]:
    try:
        return {
            "ok": False,
            "scaffold": True,
            "tool": "workflow_make_export",
            "workflow_description": workflow_description,
            "message": (
                "Make (Integromat) scenario blueprint format is more complex (module IDs, mapper trees, "
                f"flow metadata). Deferred to Phase 9. {_PHASE9_NOTE}."
            ),
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


async def workflow_webhook_trigger(
    *, webhook_url: str, payload: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    try:
        if not isinstance(webhook_url, str) or not webhook_url.startswith("https://"):
            return {
                "ok": False,
                "error": "invalid_url",
                "message": "webhook_url must start with https:// (refusing to fire).",
                "webhook_url": webhook_url,
            }
        if not _is_safe_url(webhook_url):
            return {"ok": False, "error": "ssrf_blocked", "hint": "Webhook URL resolves to a private or reserved address"}
        body = payload if isinstance(payload, dict) else {}

        try:
            import httpx  # type: ignore
        except ImportError:
            httpx = None  # type: ignore

        if httpx is not None:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json=body)
                text = resp.text[:2000] if resp.text else ""
                return {
                    "ok": resp.is_success,
                    "status_code": resp.status_code,
                    "response_text": text,
                    "webhook_url": webhook_url,
                }

        # Fallback: stdlib urllib in a thread.
        import asyncio
        import json as _json
        import urllib.request

        def _post() -> tuple[int, str]:
            data = _json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10.0) as r:  # noqa: S310
                return r.status, r.read(2000).decode("utf-8", errors="replace")

        status, text = await asyncio.to_thread(_post)
        return {
            "ok": 200 <= status < 300,
            "status_code": status,
            "response_text": text,
            "webhook_url": webhook_url,
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


WORKFLOW_ZAPIER_EXPORT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workflow_zapier_export",
        "description": "Generate an importable Zapier-style workflow template from a description.",
        "parameters": {
            "type": "object",
            "properties": {
                "workflow_description": {
                    "type": "string",
                    "description": "Plain-English description of the desired automation.",
                },
            },
            "required": ["workflow_description"],
        },
    },
}

WORKFLOW_N8N_EXPORT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workflow_n8n_export",
        "description": "Generate an importable n8n workflow JSON from a description.",
        "parameters": {
            "type": "object",
            "properties": {
                "workflow_description": {
                    "type": "string",
                    "description": "Plain-English description of the desired automation.",
                },
            },
            "required": ["workflow_description"],
        },
    },
}

WORKFLOW_MAKE_EXPORT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workflow_make_export",
        "description": "Generate a Make (Integromat) scenario blueprint. Scaffold - deferred to Phase 9.",
        "parameters": {
            "type": "object",
            "properties": {
                "workflow_description": {
                    "type": "string",
                    "description": "Plain-English description of the desired automation.",
                },
            },
            "required": ["workflow_description"],
        },
    },
}

WORKFLOW_WEBHOOK_TRIGGER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workflow_webhook_trigger",
        "description": (
            "POST a JSON payload to an https:// webhook URL. 10s timeout. "
            "Refuses non-https URLs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "webhook_url": {
                    "type": "string",
                    "description": "Target webhook URL (must start with https://).",
                },
                "payload": {
                    "type": "object",
                    "description": "JSON body to POST.",
                    "additionalProperties": True,
                },
            },
            "required": ["webhook_url"],
        },
    },
}


def register() -> None:
    register_tool("workflow_zapier_export", workflow_zapier_export, WORKFLOW_ZAPIER_EXPORT_SCHEMA)
    register_tool("workflow_n8n_export", workflow_n8n_export, WORKFLOW_N8N_EXPORT_SCHEMA)
    register_tool("workflow_make_export", workflow_make_export, WORKFLOW_MAKE_EXPORT_SCHEMA)
    register_tool("workflow_webhook_trigger", workflow_webhook_trigger, WORKFLOW_WEBHOOK_TRIGGER_SCHEMA)
