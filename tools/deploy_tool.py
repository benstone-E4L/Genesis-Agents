"""Deploy tools - GitHub, Vercel, and Netlify integrations."""
from __future__ import annotations
import base64
import logging
import os
from typing import Any

import httpx

from . import register_tool

log = logging.getLogger(__name__)

_GH_BASE = "https://api.github.com"
_VERCEL_BASE = "https://api.vercel.com"
_NETLIFY_BASE = "https://api.netlify.com/api/v1"


async def github_tool(*, action: str, repo: str, **kwargs: Any) -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return {
            "ok": False,
            "error": "missing_env: GITHUB_TOKEN",
            "hint": "set GITHUB_TOKEN to enable this tool",
        }

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            if action == "get_repo":
                resp = await client.get(f"{_GH_BASE}/repos/{repo}", headers=headers)
                resp.raise_for_status()
                return {"ok": True, "action": action, "result": resp.json()}

            elif action == "list_files":
                path = kwargs.get("path", "")
                url = f"{_GH_BASE}/repos/{repo}/contents/{path}"
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                return {"ok": True, "action": action, "result": resp.json()}

            elif action == "create_file":
                path = kwargs["path"]
                content_raw = kwargs["content"]
                if isinstance(content_raw, str):
                    content_b64 = base64.b64encode(content_raw.encode("utf-8")).decode("utf-8")
                else:
                    content_b64 = base64.b64encode(content_raw).decode("utf-8")
                body: dict[str, Any] = {
                    "message": kwargs["message"],
                    "content": content_b64,
                    "branch": kwargs.get("branch", "main"),
                }
                resp = await client.put(
                    f"{_GH_BASE}/repos/{repo}/contents/{path}",
                    headers=headers,
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
                commit_sha = data.get("commit", {}).get("sha", "")
                return {"ok": True, "action": action, "result": {"commit_sha": commit_sha, **data}}

            elif action == "get_file":
                path = kwargs["path"]
                resp = await client.get(
                    f"{_GH_BASE}/repos/{repo}/contents/{path}",
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                raw_content = data.get("content", "")
                decoded = base64.b64decode(raw_content.replace("\n", "")).decode("utf-8")
                return {"ok": True, "action": action, "result": {"text": decoded, **{k: v for k, v in data.items() if k != "content"}}}

            elif action == "create_pr":
                pr_body: dict[str, Any] = {
                    "title": kwargs["title"],
                    "head": kwargs["head"],
                    "base": kwargs.get("base", "main"),
                    "body": kwargs.get("body", ""),
                }
                resp = await client.post(
                    f"{_GH_BASE}/repos/{repo}/pulls",
                    headers=headers,
                    json=pr_body,
                )
                resp.raise_for_status()
                data = resp.json()
                return {"ok": True, "action": action, "result": {"pr_url": data.get("html_url", ""), **data}}

            else:
                return {
                    "ok": False,
                    "error": "unsupported_action",
                    "message": f"Unknown action '{action}'. Supported: get_repo, list_files, create_file, get_file, create_pr.",
                }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


async def vercel_deploy(*, project_id: str, target: str = "production", **kwargs: Any) -> dict[str, Any]:
    token = os.environ.get("VERCEL_TOKEN")
    if not token:
        return {
            "ok": False,
            "error": "missing_env: VERCEL_TOKEN",
            "hint": "set VERCEL_TOKEN to enable this tool",
        }

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        repo_id = kwargs.get("repo_id", "")
        if repo_id:
            body: dict[str, Any] = {
                "name": project_id,
                "target": target,
                "gitSource": {
                    "type": "github",
                    "repoId": repo_id,
                    "ref": kwargs.get("ref", "main"),
                },
            }
        else:
            body = {
                "name": project_id,
                "target": target,
                "files": kwargs.get("files", []),
            }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_VERCEL_BASE}/v13/deployments",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        return {
            "ok": True,
            "deployment_id": data.get("id", ""),
            "url": data.get("url", ""),
            "state": data.get("readyState", data.get("state", "")),
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


async def netlify_deploy(*, site_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
    token = os.environ.get("NETLIFY_AUTH_TOKEN")
    if not token:
        return {
            "ok": False,
            "error": "missing_env: NETLIFY_AUTH_TOKEN",
            "hint": "set NETLIFY_AUTH_TOKEN to enable this tool",
        }

    resolved_site_id = site_id or os.environ.get("NETLIFY_SITE_ID")
    if not resolved_site_id:
        return {
            "ok": False,
            "error": "missing_env: NETLIFY_SITE_ID",
            "hint": "set NETLIFY_SITE_ID or pass site_id to enable this tool",
        }

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if kwargs.get("trigger_only"):
                resp = await client.post(
                    f"{_NETLIFY_BASE}/sites/{resolved_site_id}/deploys",
                    headers=headers,
                )
            else:
                resp = await client.post(
                    f"{_NETLIFY_BASE}/sites/{resolved_site_id}/deploys",
                    headers=headers,
                    json={},
                )
            resp.raise_for_status()
            data = resp.json()

        return {
            "ok": True,
            "deploy_id": data.get("id", ""),
            "state": data.get("state", ""),
            "url": data.get("deploy_ssl_url") or data.get("url", ""),
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


GITHUB_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "github_tool",
        "description": (
            "Interact with the GitHub API. Supports: get_repo, list_files, create_file, get_file, create_pr. "
            "Requires GITHUB_TOKEN env var."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get_repo", "list_files", "create_file", "get_file", "create_pr"],
                    "description": "Action to perform.",
                },
                "repo": {
                    "type": "string",
                    "description": "Repository in owner/name format (e.g. 'acme/my-repo').",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory path (required for list_files, create_file, get_file).",
                },
                "content": {
                    "type": "string",
                    "description": "File content as a string (required for create_file).",
                },
                "message": {
                    "type": "string",
                    "description": "Commit message (required for create_file).",
                },
                "branch": {
                    "type": "string",
                    "description": "Branch name (default: main).",
                },
                "title": {
                    "type": "string",
                    "description": "PR title (required for create_pr).",
                },
                "head": {
                    "type": "string",
                    "description": "Head branch for PR (required for create_pr).",
                },
                "base": {
                    "type": "string",
                    "description": "Base branch for PR (default: main).",
                },
                "body": {
                    "type": "string",
                    "description": "PR description body.",
                },
            },
            "required": ["action", "repo"],
        },
    },
}

VERCEL_DEPLOY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "vercel_deploy",
        "description": "Trigger a Vercel deployment. Requires VERCEL_TOKEN env var.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Vercel project name or ID.",
                },
                "target": {
                    "type": "string",
                    "description": "Deployment target: 'production' or 'preview' (default: production).",
                    "default": "production",
                },
                "repo_id": {
                    "type": "string",
                    "description": "GitHub repo ID for git-connected deployments.",
                },
                "ref": {
                    "type": "string",
                    "description": "Git ref/branch to deploy (default: main).",
                },
                "files": {
                    "type": "array",
                    "description": "Array of file objects for direct file deployment (used when repo_id is not provided).",
                    "items": {"type": "object", "additionalProperties": True},
                },
            },
            "required": ["project_id"],
        },
    },
}

NETLIFY_DEPLOY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "netlify_deploy",
        "description": (
            "Trigger a Netlify deployment. Requires NETLIFY_AUTH_TOKEN env var. "
            "site_id defaults to NETLIFY_SITE_ID env var."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "site_id": {
                    "type": "string",
                    "description": "Netlify site ID (falls back to NETLIFY_SITE_ID env var).",
                },
                "trigger_only": {
                    "type": "boolean",
                    "description": "If true, trigger a redeploy without additional config.",
                    "default": False,
                },
            },
            "required": [],
        },
    },
}


def register() -> None:
    register_tool("github_tool", github_tool, GITHUB_TOOL_SCHEMA)
    register_tool("vercel_deploy", vercel_deploy, VERCEL_DEPLOY_SCHEMA)
    register_tool("netlify_deploy", netlify_deploy, NETLIFY_DEPLOY_SCHEMA)
