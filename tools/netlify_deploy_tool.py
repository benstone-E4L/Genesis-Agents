"""netlify_deploy — Deploy sites to Netlify.

Requires NETLIFY_TOKEN env var. Returns a clear 'not_configured' error if
token is absent so agents do not silently claim a deploy succeeded.
"""
from __future__ import annotations
import logging
import os
import zipfile
import io
from typing import Any

import httpx

from . import register_tool

log = logging.getLogger(__name__)

_NETLIFY_API = "https://api.netlify.com/api/v1"


def _token() -> str:
    return (os.getenv("NETLIFY_TOKEN") or "").strip()


def _not_configured() -> dict[str, Any]:
    return {
        "ok": False,
        "error": "netlify_deploy_not_configured",
        "message": "NETLIFY_TOKEN is not set. Set the NETLIFY_TOKEN env var to enable Netlify deployments. Agent must not claim deployment succeeded without this credential.",
    }


async def netlify_deploy(
    *,
    site_name: str,
    files: dict[str, str] | None = None,
    site_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Deploy a site to Netlify. Requires NETLIFY_TOKEN.
    files: dict mapping relative path → file content.
    Either site_name (creates new) or site_id (deploys to existing) is required.
    """
    if not _token():
        return _not_configured()

    headers = {
        "Authorization": f"Bearer {_token()}",
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Create site if no site_id provided
            if not site_id:
                site_resp = await client.post(
                    f"{_NETLIFY_API}/sites",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"name": site_name},
                )
                if site_resp.status_code not in (200, 201):
                    return {
                        "ok": False,
                        "error": "netlify_site_create_failed",
                        "status": site_resp.status_code,
                        "body": site_resp.text[:300],
                    }
                site_data = site_resp.json()
                site_id = site_data.get("id")

            if not files:
                return {
                    "ok": True,
                    "site_id": site_id,
                    "site_name": site_name,
                    "message": "Site created (no files to deploy)",
                    "deploy_url": f"https://{site_name}.netlify.app",
                }

            # Create zip archive of files in memory
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for path, content in files.items():
                    zf.writestr(path, content)
            zip_bytes = zip_buffer.getvalue()

            deploy_resp = await client.post(
                f"{_NETLIFY_API}/sites/{site_id}/deploys",
                headers={
                    **headers,
                    "Content-Type": "application/zip",
                },
                content=zip_bytes,
            )

            if deploy_resp.status_code in (200, 201):
                data = deploy_resp.json()
                return {
                    "ok": True,
                    "deploy_id": data.get("id"),
                    "site_id": site_id,
                    "deploy_url": data.get("deploy_url") or f"https://{site_name}.netlify.app",
                    "state": data.get("state", "uploading"),
                }
            return {
                "ok": False,
                "error": "netlify_deploy_failed",
                "status": deploy_resp.status_code,
                "body": deploy_resp.text[:300],
            }

    except Exception as e:
        log.exception("netlify_deploy site=%s failed", site_name)
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


NETLIFY_DEPLOY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "netlify_deploy",
        "description": (
            "Deploy a site to Netlify. Requires NETLIFY_TOKEN env var. "
            "Returns deploy_url on success. Will report not_configured if token is missing — "
            "agent must not claim success when this error is returned."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "site_name": {"type": "string", "description": "Netlify site name (used as subdomain)."},
                "files": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Map of file path → content to deploy.",
                },
                "site_id": {
                    "type": "string",
                    "description": "Existing Netlify site ID (omit to create new).",
                },
            },
            "required": ["site_name"],
        },
    },
}


def register() -> None:
    register_tool("netlify_deploy", netlify_deploy, NETLIFY_DEPLOY_SCHEMA)
