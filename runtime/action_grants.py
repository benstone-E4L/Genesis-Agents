"""Server-issued, payload-bound, expiring, single-use action grants."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


class GrantError(ValueError):
    pass


def _key(explicit: str | None = None) -> bytes:
    value = explicit if explicit is not None else (os.getenv("GENESIS_ACTION_GRANT_KEY") or "")
    if len(value) < 32:
        raise GrantError("GENESIS_ACTION_GRANT_KEY_not_configured")
    return value.encode("utf-8")


def _digest(tool: str, args: dict[str, Any]) -> str:
    canonical = json.dumps({"tool": tool, "args": args}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def issue_action_grant(
    *, principal_id: str, tenant_id: str, tool: str, args: dict[str, Any],
    authorization_id: str, key: str | None = None, now: int | None = None, ttl_seconds: int = 300,
) -> str:
    current = int(time.time() if now is None else now)
    payload = {
        "v": 1, "aud": "genesis-tool-dispatch", "jti": uuid.uuid4().hex,
        "authorization_id": authorization_id, "sub": principal_id, "tenant": tenant_id,
        "tool": tool, "args_digest": _digest(tool, args), "iat": current,
        "exp": current + min(max(1, int(ttl_seconds)), 300),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    sig = base64.urlsafe_b64encode(hmac.new(_key(key), encoded.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"genesis-action-v1.{encoded}.{sig}"


def consume_action_grant(
    token: str, *, principal_id: str, tenant_id: str, tool: str, args: dict[str, Any],
    db_path: Path | None = None, key: str | None = None, now: int | None = None,
) -> str:
    current = int(time.time() if now is None else now)
    try:
        prefix, encoded, supplied = token.split(".", 2)
        if prefix != "genesis-action-v1":
            raise ValueError
        expected = base64.urlsafe_b64encode(hmac.new(_key(key), encoded.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(expected, supplied):
            raise GrantError("grant_signature_invalid")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except GrantError:
        raise
    except Exception as exc:
        raise GrantError("grant_invalid") from exc
    if payload.get("v") != 1 or payload.get("aud") != "genesis-tool-dispatch":
        raise GrantError("grant_audience_invalid")
    if current > int(payload.get("exp", 0)) or current < int(payload.get("iat", 0)) - 60:
        raise GrantError("grant_expired")
    checks = (
        (payload.get("sub"), principal_id, "grant_principal_mismatch"),
        (payload.get("tenant"), tenant_id, "grant_tenant_mismatch"),
        (payload.get("tool"), tool, "grant_tool_mismatch"),
        (payload.get("args_digest"), _digest(tool, args), "grant_args_mismatch"),
    )
    for actual, required, error in checks:
        if not hmac.compare_digest(str(actual or ""), str(required or "")):
            raise GrantError(error)
    configured = db_path or Path(os.getenv("GENESIS_AUTH_DB_PATH") or "")
    if not str(configured):
        raise GrantError("GENESIS_AUTH_DB_PATH_not_configured")
    configured.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(configured) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS consumed_action_grants "
                "(jti TEXT PRIMARY KEY, consumed_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)"
            )
            conn.execute("DELETE FROM consumed_action_grants WHERE expires_at < ?", (current,))
            conn.execute(
                "INSERT INTO consumed_action_grants(jti, consumed_at, expires_at) VALUES (?, ?, ?)",
                (str(payload["jti"]), current, int(payload["exp"])),
            )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise GrantError("grant_already_consumed") from exc
    return str(payload.get("authorization_id") or "")
