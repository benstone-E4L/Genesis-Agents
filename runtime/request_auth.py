"""Verified AP2 principals and short-lived owner-scoped continuation tokens."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

AP2_VERSION = 1
# Canonical `payload.agent` value for a signed /retrieval/query envelope. Retrieval is not an
# agent invocation, so it borrows the AP2 envelope shape rather than an agent slug: `agent` names
# the route, `task` carries the query text, `params` carries the retrieval scope. This constant is
# the contract both Cato (cato/core/ask_e4l.py) and this gateway sign/verify against.
RETRIEVAL_ENVELOPE_AGENT = "retrieval.query"
MAX_CLOCK_SKEW_SECONDS = 300
PRINCIPAL_TOKEN_TTL_SECONDS = 600
PRINCIPAL_TOKEN_AUDIENCE = "genesis-gateway"
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    principal_id: str
    tenant_id: str
    client_id: str
    scopes: frozenset[str]
    auth_method: str
    expires_at: int

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes or "*" in self.scopes


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _registry_path() -> Path:
    return Path(__file__).resolve().parent.parent / "trusted_ap2_clients.json"


def _load_registry(path: Path | None = None) -> list[dict[str, Any]]:
    data = json.loads((path or _registry_path()).read_text(encoding="utf-8"))
    clients = data.get("clients")
    if data.get("version") != 1 or not isinstance(clients, list):
        raise AuthenticationError("trusted_client_registry_invalid")
    return clients


def _parse_timestamp(value: Any) -> int:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthenticationError("ap2_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise AuthenticationError("ap2_timestamp_invalid")
    return int(parsed.astimezone(timezone.utc).timestamp())


def _auth_db_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    configured = (os.getenv("GENESIS_AUTH_DB_PATH") or "").strip()
    if not configured:
        raise AuthenticationError("GENESIS_AUTH_DB_PATH_not_configured")
    return Path(configured)


def auth_db_is_usable(db_path: Path | None = None) -> tuple[bool, str]:
    """Can the AP2 nonce store actually be opened and written? (usable, reason).

    Called at boot by main.assert_auth_material_configured. Checking only that the env var is
    non-empty proves nothing: GENESIS_AUTH_DB_PATH=/var/data/... on a host with no disk mounted
    at /var/data passes the length check and then fails on the FIRST AP2 request, after the
    caller believed it was authenticated.
    """
    try:
        path = _auth_db_path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ap2_nonces (client_id TEXT NOT NULL, nonce TEXT NOT NULL, "
                "expires_at INTEGER NOT NULL, PRIMARY KEY(client_id, nonce))"
            )
            conn.commit()
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _consume_nonce(client_id: str, nonce: str, expires_at: int, *, db_path: Path | None = None) -> None:
    path = _auth_db_path(db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Fail CLOSED and named. Previously an unwritable path raised OSError straight through
        # verify_agent_principal's `except AuthenticationError`, surfacing as HTTP 500 with a
        # stack trace instead of a diagnosable refusal.
        raise AuthenticationError("ap2_nonce_store_unavailable") from exc
    try:
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ap2_nonces (client_id TEXT NOT NULL, nonce TEXT NOT NULL, "
                "expires_at INTEGER NOT NULL, PRIMARY KEY(client_id, nonce))"
            )
            conn.execute("DELETE FROM ap2_nonces WHERE expires_at < ?", (int(time.time()),))
            conn.execute(
                "INSERT INTO ap2_nonces(client_id, nonce, expires_at) VALUES (?, ?, ?)",
                (client_id, nonce, expires_at),
            )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise AuthenticationError("ap2_replay_detected") from exc
    except (sqlite3.Error, OSError) as exc:
        # Disk full / read-only mount / corrupt DB. Replay protection is unavailable, so the
        # only safe answer is refusal — never "accept and hope".
        raise AuthenticationError("ap2_nonce_store_unavailable") from exc


def verify_ap2_envelope(
    body: dict[str, Any],
    *,
    header_version: str | None,
    header_pubkey: str | None,
    required_scope: str,
    now: int | None = None,
    registry_path: Path | None = None,
    db_path: Path | None = None,
) -> Principal:
    """Verify Cato's exact AP2 v1 canonical bytes and atomically consume nonce."""
    current = int(time.time() if now is None else now)
    if body.get("version") != AP2_VERSION or str(header_version or "") != str(AP2_VERSION):
        raise AuthenticationError("ap2_version_invalid")
    payload = body.get("payload")
    nonce = str(body.get("nonce") or "")
    timestamp = str(body.get("timestamp") or "")
    pubkey_b64 = str(body.get("pubkey") or "")
    signature_b64 = str(body.get("signature") or "")
    if not isinstance(payload, dict) or not _NONCE_RE.fullmatch(nonce):
        raise AuthenticationError("ap2_envelope_invalid")
    if not hmac.compare_digest(pubkey_b64, str(header_pubkey or "")):
        raise AuthenticationError("ap2_pubkey_header_mismatch")
    stamped = _parse_timestamp(timestamp)
    if abs(current - stamped) > MAX_CLOCK_SKEW_SECONDS:
        raise AuthenticationError("ap2_envelope_expired")

    matches = [
        c for c in _load_registry(registry_path)
        if c.get("enabled") is True and hmac.compare_digest(str(c.get("pubkey_b64") or ""), pubkey_b64)
    ]
    if len(matches) != 1:
        raise AuthenticationError("ap2_client_untrusted")
    client = matches[0]
    scopes = frozenset(str(v) for v in client.get("capabilities", []))
    if required_scope not in scopes and "*" not in scopes:
        raise AuthenticationError("ap2_scope_denied")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64, validate=True))
        signature = base64.b64decode(signature_b64, validate=True)
        public_key.verify(signature, _canonical_json({"payload": payload, "nonce": nonce, "timestamp": timestamp}))
    except Exception as exc:
        raise AuthenticationError("ap2_signature_invalid") from exc

    _consume_nonce(str(client["client_id"]), nonce, stamped + MAX_CLOCK_SKEW_SECONDS, db_path=db_path)
    return Principal(
        principal_id=str(client.get("principal_id") or f"client:{client['client_id']}"),
        tenant_id=str(client.get("tenant_id") or ""),
        client_id=str(client["client_id"]),
        scopes=scopes,
        auth_method="ap2",
        expires_at=stamped + MAX_CLOCK_SKEW_SECONDS,
    )


def _eq(left: Any, right: Any) -> bool:
    """Constant-time compare of two arbitrary strings.

    `hmac.compare_digest` raises TypeError on any non-ASCII `str`, so every caller that compares
    attacker-influenced free text (a prompt, a retrieval query) must encode first. Comparing the
    UTF-8 bytes keeps the constant-time property and accepts the full unicode range.
    """
    return hmac.compare_digest(str(left or "").encode("utf-8"), str(right or "").encode("utf-8"))


def assert_envelope_binds(
    body: dict[str, Any], *, agent: str, task: str, params: dict[str, Any]
) -> None:
    """Prove the signed AP2 payload actually covers what the route will execute.

    `verify_ap2_envelope` only proves that `{payload, nonce, timestamp}` was signed by a trusted
    key. It says nothing about the *unsigned* top-level request fields a route reads. A route must
    therefore project its real execution inputs into (agent, task, params) and prove the signed
    payload matches them exactly — otherwise the signature covers nothing that matters and the
    envelope is theatre.

    Raises AuthenticationError; kept free of FastAPI so the binding is directly testable without
    a live signing key or a running app.
    """
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise AuthenticationError("ap2_envelope_invalid")
    if not _eq(payload.get("agent"), agent):
        raise AuthenticationError("ap2_agent_mismatch")
    if not _eq(payload.get("task"), task):
        raise AuthenticationError("ap2_task_mismatch")
    signed_params = payload.get("params")
    if not isinstance(signed_params, dict):
        raise AuthenticationError("ap2_params_invalid")
    if not hmac.compare_digest(_canonical_json(signed_params), _canonical_json(params)):
        raise AuthenticationError("ap2_params_mismatch")


def _token_key(explicit: str | None = None) -> bytes:
    value = explicit if explicit is not None else (os.getenv("GENESIS_PRINCIPAL_TOKEN_KEY") or "")
    if len(value) < 32:
        raise AuthenticationError("GENESIS_PRINCIPAL_TOKEN_KEY_not_configured")
    return value.encode("utf-8")


def issue_principal_token(
    principal: Principal, *, key: str | None = None, now: int | None = None, ttl_seconds: int = PRINCIPAL_TOKEN_TTL_SECONDS
) -> str:
    current = int(time.time() if now is None else now)
    payload = {
        "v": 1, "aud": PRINCIPAL_TOKEN_AUDIENCE,
        "sub": principal.principal_id, "tenant": principal.tenant_id,
        "client": principal.client_id, "scopes": sorted(principal.scopes),
        "iat": current, "exp": current + min(max(1, int(ttl_seconds)), PRINCIPAL_TOKEN_TTL_SECONDS),
    }
    encoded = base64.urlsafe_b64encode(_canonical_json(payload)).rstrip(b"=").decode("ascii")
    signature = hmac.new(_token_key(key), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"genesis-principal-v1.{encoded}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"


def verify_principal_token(token: str, *, key: str | None = None, now: int | None = None) -> Principal:
    current = int(time.time() if now is None else now)
    try:
        prefix, encoded, supplied = token.split(".", 2)
        if prefix != "genesis-principal-v1":
            raise ValueError
        expected = base64.urlsafe_b64encode(
            hmac.new(_token_key(key), encoded.encode("ascii"), hashlib.sha256).digest()
        ).rstrip(b"=").decode("ascii")
        if not hmac.compare_digest(expected, supplied):
            raise AuthenticationError("principal_token_signature_invalid")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except AuthenticationError:
        raise
    except Exception as exc:
        raise AuthenticationError("principal_token_invalid") from exc
    if (
        payload.get("v") != 1 or payload.get("aud") != PRINCIPAL_TOKEN_AUDIENCE
        or current > int(payload.get("exp", 0)) or current < int(payload.get("iat", 0)) - 60
    ):
        raise AuthenticationError("principal_token_expired")
    if not payload.get("sub") or not payload.get("tenant"):
        raise AuthenticationError("principal_token_identity_missing")
    return Principal(
        principal_id=str(payload["sub"]), tenant_id=str(payload["tenant"]), client_id=str(payload.get("client") or ""),
        scopes=frozenset(str(v) for v in payload.get("scopes", [])), auth_method="principal_token",
        expires_at=int(payload["exp"]),
    )


def legacy_gateway_principal() -> Principal:
    """Compatibility identity; cannot read owner-scoped rows or administer.

    `job.read`/`artifact.read` are granted so the compatibility path is actually REACHABLE.
    Without them `_require_owned_job` refused on the scope check before ever consulting
    owns_resource — which made owns_resource's own `tenant is None and owner is None ->
    legacy_gateway` branch dead code, and made `GET /agents/jobs/{id}` return 403 "principal
    scope denied" to a gateway-key caller reading even a pre-AP2, unowned job.

    This grants no access to AP2-owned rows: owns_resource still returns False for any row that
    carries a tenant or an owner, so the tenant scoping is untouched. `admin` is still absent.
    """
    return Principal(
        principal_id="legacy:gateway", tenant_id="legacy", client_id="legacy-gateway",
        scopes=frozenset({
            "agent.invoke", "agent.list", "agent.health", "job.read", "artifact.read",
        }),
        auth_method="legacy_gateway", expires_at=int(time.time()) + 60,
    )


def owns_resource(principal: Principal, row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    tenant = row.get("tenantId", row.get("tenant_id"))
    owner = row.get("ownerPrincipalId", row.get("owner_principal_id"))
    if tenant is None and owner is None:
        return principal.auth_method == "legacy_gateway"
    return hmac.compare_digest(str(tenant or ""), principal.tenant_id) and hmac.compare_digest(
        str(owner or ""), principal.principal_id
    )
