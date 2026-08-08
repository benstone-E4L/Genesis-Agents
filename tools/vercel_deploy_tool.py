"""vercel_deploy — Deploy projects to Vercel.

Requires VERCEL_TOKEN env var. Returns a clear 'not_configured' error if
the token is absent so agents do not silently claim a deploy succeeded.
"""
from __future__ import annotations
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from . import register_tool

log = logging.getLogger(__name__)

_VERCEL_API = "https://api.vercel.com"


def _token() -> str:
    return (os.getenv("VERCEL_TOKEN") or "").strip()


def _not_configured() -> dict[str, Any]:
    return {
        "ok": False,
        "error": "vercel_deploy_not_configured",
        "message": "VERCEL_TOKEN is not set. Set the VERCEL_TOKEN env var to enable Vercel deployments. Agent cannot claim deployment succeeded without this credential.",
    }


async def vercel_deploy(
    *,
    project_name: str,
    files: dict[str, str] | None = None,
    env_vars: dict[str, str] | None = None,
    framework: str = "nextjs",
    team_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Deploy a project to Vercel. Requires VERCEL_TOKEN.
    files: dict mapping relative path → file content.
    """
    if not _token():
        return _not_configured()

    if not project_name:
        return {"ok": False, "error": "missing_param", "message": "project_name required"}

    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }
    params: dict[str, str] = {}
    if team_id:
        params["teamId"] = team_id

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Build deployment files list
            deployment_files = []
            if files:
                for path, content in files.items():
                    deployment_files.append({"file": path, "data": content})

            payload: dict[str, Any] = {
                "name": project_name,
                "files": deployment_files,
                "projectSettings": {"framework": framework},
            }
            if env_vars:
                payload["env"] = {k: {"value": v, "type": "plain"} for k, v in env_vars.items()}

            resp = await client.post(
                f"{_VERCEL_API}/v13/deployments",
                headers=headers,
                json=payload,
                params=params,
            )

            if resp.status_code in (200, 201):
                data = resp.json()
                return {
                    "ok": True,
                    "deployment_id": data.get("id"),
                    "deployment_url": f"https://{data.get('url', '')}",
                    "state": data.get("readyState", "BUILDING"),
                    "project": project_name,
                }
            return {
                "ok": False,
                "error": "vercel_api_error",
                "status": resp.status_code,
                "body": resp.text[:300],
            }
    except Exception as e:
        log.exception("vercel_deploy project=%s failed", project_name)
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


VERCEL_DEPLOY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "vercel_deploy",
        "description": (
            "Deploy a project to Vercel. Requires VERCEL_TOKEN env var. "
            "Returns deployment_url on success. Will report not_configured if token is missing — "
            "agent must not claim success when this error is returned."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "description": "Vercel project name."},
                "files": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Map of file path → content to deploy.",
                },
                "env_vars": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Environment variables to set on deployment.",
                },
                "framework": {
                    "type": "string",
                    "description": "Framework preset (default: nextjs).",
                    "default": "nextjs",
                },
                "team_id": {"type": "string", "description": "Vercel team ID (optional)."},
            },
            "required": ["project_name"],
        },
    },
}


def register() -> None:
    register_tool("vercel_deploy", vercel_deploy, VERCEL_DEPLOY_SCHEMA)
