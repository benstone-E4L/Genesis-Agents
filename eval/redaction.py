"""Recursive secret redaction for anything that may reach a Phoenix span.

Behaviour is copied from Cato's ``cato/core/approval_policy.py:redact()`` so the
two systems agree on what counts as a secret. Two independent defences:

1. **Key-shaped** — a dict key containing ``api_key`` / ``authorization`` /
   ``_key`` / ``secret`` / ... is redacted at ANY nesting depth. This is what
   catches ``{"headers": {"authorization": "..."}}``.
2. **Value-shaped** — a credential-looking string (``sk-...``, ``Bearer ...``,
   a JWT, ``AKIA...``) is redacted even under an innocent key.

A bare ``key`` is deliberately NOT sensitive (``"_key" not in "key"``), matching
the reference implementation — dict keys named ``key`` carry ordinary data.
"""

from __future__ import annotations

import re
import threading
from typing import Any

REDACTED = "[redacted]"

# Substring match against the lowercased dict key.
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "api-key",
    "_key",
    "authorization",
    "auth_token",
    "access_token",
    "refresh_token",
    "id_token",
    "bearer",
    "token",
    "secret",
    "password",
    "passwd",
    "passphrase",
    "credential",
    "private_key",
    # "privkey" has no underscore before "key", so the "_key" part above does
    # NOT match GENESIS_GATEWAY_PRIVKEY_B64 (the gateway signing key).
    "privkey",
    "seckey",
    "client_secret",
    "session_key",
    "cookie",
    "vault",
    "signature",
    "otp",
    # Genesis/AWS specifics that are not covered by the generic parts above.
    "database_url",
    "dsn",
    "aws_access_key_id",
    "connection_string",
)

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password)(['\"\s:=]+)([^,'\"\s}]+)"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{12,})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_\-]{30,}\b"),  # telegram bot token
    # postgres://user:password@host/db  -> mask the userinfo section
    re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@"),
]

_MAX_REDACT_DEPTH = 24

#: Env vars whose *values* must never appear in a trace. Their live values are
#: substituted out by :func:`redact` regardless of where they are embedded.
SECRET_ENV_VARS = (
    "GATEWAY_API_KEY",
    "AGENT_GATEWAY_SECRET",
    "LLM_API_KEY",
    "SWARMSYNC_ROUTING_API_KEY",
    "ROUTING_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "PHOENIX_API_KEY",
    "DATABASE_URL",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GENESIS_SESSION_VAULT_KEY",
    "GENESIS_GATEWAY_PRIVKEY_B64",
    "R2_API_TOKEN",
)

# Values registered here are stripped verbatim from every string. Populated
# from the environment at import time and refreshable via refresh_env_secrets().
_LITERAL_SECRETS: set[str] = set()

# Below this length a "secret" is too generic to blind-replace without
# corrupting ordinary text.
_MIN_LITERAL_SECRET_LEN = 8


#: Guards _LITERAL_SECRETS. ``refresh_env_secrets`` is called from request
#: threads (every ``_load_redactor``) and from eval runs, while ``redact_text``
#: iterates the same set. Mutating a set during iteration raises RuntimeError,
#: and a redaction that raises is a redaction that did not happen.
_secrets_lock = threading.Lock()


def register_literal_secret(value: str) -> None:
    """Register a literal value that must never appear in any traced string."""
    val = (value or "").strip()
    if len(val) >= _MIN_LITERAL_SECRET_LEN:
        with _secrets_lock:
            _LITERAL_SECRETS.add(val)


def refresh_env_secrets(environ: dict[str, str] | None = None) -> None:
    """Re-read :data:`SECRET_ENV_VARS` from the environment.

    Call after mutating os.environ (tests, credential rotation). Never logs or
    returns the values it reads.
    """
    import os

    env = os.environ if environ is None else environ
    for name in SECRET_ENV_VARS:
        register_literal_secret(env.get(name) or "")


def is_sensitive_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact_text(text: str) -> str:
    """Mask credential-shaped values inside a free-text string."""
    redacted = text
    with _secrets_lock:
        snapshot = tuple(_LITERAL_SECRETS)
    for secret in snapshot:
        if secret in redacted:
            redacted = redacted.replace(secret, REDACTED)
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 3:
            redacted = pattern.sub(r"\1\2" + REDACTED, redacted)
        else:
            redacted = pattern.sub(REDACTED, redacted)
    return redacted


def redact(value: Any, key: str = "", _depth: int = 0) -> Any:
    """Recursively redact a payload before it is traced, persisted or displayed.

    Redacts on the *key* (so ``{"headers": {"authorization": "..."}}`` is caught
    at any nesting depth) and on the *value* shape (so a bare ``sk-...`` under an
    innocent key is caught too).
    """
    if _depth > _MAX_REDACT_DEPTH:
        return REDACTED

    if key and is_sensitive_key(key):
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return REDACTED if value else value

    if isinstance(value, dict):
        return {str(k): redact(v, str(k), _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(item, "", _depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: {redact_text(str(value))}"
    return redact_text(str(value))


refresh_env_secrets()
