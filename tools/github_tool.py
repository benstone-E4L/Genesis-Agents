"""github_tool — GitHub repository and file operations.

Requires GITHUB_TOKEN env var. If not configured, all operations return a
clear 'not_configured' error so agents do not silently fake git work.
"""
from __future__ import annotations
import logging
import os
from typing import Any

import httpx

from . import register_tool

log = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"


def _token() -> str:
    return (os.getenv("GITHUB_TOKEN") or "").strip()


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _not_configured() -> dict[str, Any]:
    return {
        "ok": False,
        "error": "github_tool_not_configured",
        "message": "GITHUB_TOKEN is not set. Set the GITHUB_TOKEN env var to enable GitHub operations.",
    }


async def github_tool(
    *,
    action: str,
    repo: str | None = None,
    path: str | None = None,
    content: str | None = None,
    message: str | None = None,
    branch: str = "main",
    org: str | None = None,
    private: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Perform a GitHub operation. action must be one of:
    create_repo, push_file, get_file, list_repos, create_branch.
    Requires GITHUB_TOKEN env var.
    """
    if not _token():
        return _not_configured()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if action == "create_repo":
                if not repo:
                    return {"ok": False, "error": "missing_param", "message": "repo name required"}
                owner = org or (await client.get(f"{_GITHUB_API}/user", headers=_headers())).json().get("login", "")
                payload: dict[str, Any] = {"name": repo, "private": private, "auto_init": True}
                if org:
                    resp = await client.post(f"{_GITHUB_API}/orgs/{org}/repos", headers=_headers(), json=payload)
                else:
                    resp = await client.post(f"{_GITHUB_API}/user/repos", headers=_headers(), json=payload)
                if resp.status_code in (200, 201):
                    data = resp.json()
                    return {"ok": True, "repo_url": data.get("html_url"), "clone_url": data.get("clone_url"), "repo": data.get("full_name")}
                return {"ok": False, "error": "github_api_error", "status": resp.status_code, "body": resp.text[:300]}

            elif action == "push_file":
                if not all([repo, path, content]):
                    return {"ok": False, "error": "missing_param", "message": "repo, path, and content required"}
                import base64
                encoded = base64.b64encode(content.encode()).decode()  # type: ignore[union-attr]
                # Try to get existing file SHA for update
                sha = None
                get_resp = await client.get(
                    f"{_GITHUB_API}/repos/{repo}/contents/{path}",
                    headers=_headers(),
                    params={"ref": branch},
                )
                if get_resp.status_code == 200:
                    sha = get_resp.json().get("sha")

                put_payload: dict[str, Any] = {
                    "message": message or f"Update {path}",
                    "content": encoded,
                    "branch": branch,
                }
                if sha:
                    put_payload["sha"] = sha

                put_resp = await client.put(
                    f"{_GITHUB_API}/repos/{repo}/contents/{path}",
                    headers=_headers(),
                    json=put_payload,
                )
                if put_resp.status_code in (200, 201):
                    return {"ok": True, "path": path, "repo": repo, "branch": branch}
                return {"ok": False, "error": "github_api_error", "status": put_resp.status_code, "body": put_resp.text[:300]}

            elif action == "get_file":
                if not all([repo, path]):
                    return {"ok": False, "error": "missing_param", "message": "repo and path required"}
                import base64
                resp = await client.get(
                    f"{_GITHUB_API}/repos/{repo}/contents/{path}",
                    headers=_headers(),
                    params={"ref": branch},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    decoded = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8", errors="replace")
                    return {"ok": True, "path": path, "content": decoded, "sha": data.get("sha")}
                return {"ok": False, "error": "file_not_found", "status": resp.status_code}

            elif action == "list_repos":
                resp = await client.get(f"{_GITHUB_API}/user/repos", headers=_headers(), params={"per_page": 30})
                if resp.status_code == 200:
                    repos = [{"name": r["name"], "full_name": r["full_name"], "url": r["html_url"]} for r in resp.json()]
                    return {"ok": True, "repos": repos}
                return {"ok": False, "error": "github_api_error", "status": resp.status_code}

            elif action == "create_branch":
                if not repo:
                    return {"ok": False, "error": "missing_param", "message": "repo required"}
                new_branch = kwargs.get("new_branch", "dev")
                # Get base SHA
                ref_resp = await client.get(f"{_GITHUB_API}/repos/{repo}/git/ref/heads/{branch}", headers=_headers())
                if ref_resp.status_code != 200:
                    return {"ok": False, "error": "base_branch_not_found", "branch": branch}
                sha = ref_resp.json()["object"]["sha"]
                create_resp = await client.post(
                    f"{_GITHUB_API}/repos/{repo}/git/refs",
                    headers=_headers(),
                    json={"ref": f"refs/heads/{new_branch}", "sha": sha},
                )
                if create_resp.status_code == 201:
                    return {"ok": True, "branch": new_branch, "repo": repo}
                return {"ok": False, "error": "github_api_error", "status": create_resp.status_code, "body": create_resp.text[:300]}

            else:
                return {"ok": False, "error": "unknown_action", "message": f"Unknown action '{action}'. Valid: create_repo, push_file, get_file, list_repos, create_branch"}

    except Exception as e:
        log.exception("github_tool action=%s failed", action)
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


GITHUB_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "github_tool",
        "description": (
            "GitHub repository operations. Requires GITHUB_TOKEN env var. "
            "Actions: create_repo, push_file, get_file, list_repos, create_branch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create_repo", "push_file", "get_file", "list_repos", "create_branch"],
                    "description": "The GitHub operation to perform.",
                },
                "repo": {"type": "string", "description": "Repository full name (owner/repo) or just name for create_repo."},
                "path": {"type": "string", "description": "File path within the repo."},
                "content": {"type": "string", "description": "File content for push_file."},
                "message": {"type": "string", "description": "Commit message for push_file."},
                "branch": {"type": "string", "description": "Branch name (default: main)."},
                "org": {"type": "string", "description": "GitHub org for create_repo."},
                "private": {"type": "boolean", "description": "Make repo private (default: true)."},
                "new_branch": {"type": "string", "description": "New branch name for create_branch action."},
            },
            "required": ["action"],
        },
    },
}


def register() -> None:
    register_tool("github_tool", github_tool, GITHUB_TOOL_SCHEMA)
