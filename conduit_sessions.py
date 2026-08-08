"""Conduit session vault - encrypted per-job storage of buyer browser sessions.

Buyers authorize Genesis agents to act on their behalf by uploading a
Conduit session export (cookies + local storage). The agent loads the
session into its ConduitBridge for the duration of the job. Every
action is audited (Conduit's Ed25519 chain). Sessions are deleted
after job completion.

Encryption: AES-256-GCM, key from GENESIS_SESSION_VAULT_KEY env var
(32 bytes, base64). Per-session random nonce. Sessions stored under
{LOCAL_DIR}/sessions/{job_id}/session.enc.
"""
from __future__ import annotations
import base64, json, logging, os, secrets
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _CRYPTO_AVAILABLE = True
except ImportError:
    log.warning("cryptography not installed; session vault disabled")
    AESGCM = None  # type: ignore
    _CRYPTO_AVAILABLE = False

LOCAL_DIR = Path(os.getenv("GENESIS_SESSION_VAULT_DIR", "/var/data/genesis-sessions"))
KEY_B64 = os.getenv("GENESIS_SESSION_VAULT_KEY", "")


def _get_key() -> bytes | None:
    if not KEY_B64 or not _CRYPTO_AVAILABLE:
        return None
    try:
        key = base64.b64decode(KEY_B64)
        if len(key) != 32:
            log.error("GENESIS_SESSION_VAULT_KEY must be 32 bytes (base64); got %d", len(key))
            return None
        return key
    except Exception:
        log.exception("invalid GENESIS_SESSION_VAULT_KEY")
        return None


def store_session(*, job_id: str, session_data: dict[str, Any]) -> dict[str, Any]:
    """Encrypt + store a session for a job. session_data is the Conduit session export."""
    key = _get_key()
    if key is None:
        return {"ok": False, "error": "vault_not_configured",
                "message": "GENESIS_SESSION_VAULT_KEY env var missing or invalid"}

    aes = AESGCM(key)
    nonce = secrets.token_bytes(12)
    plaintext = json.dumps(session_data).encode("utf-8")
    ciphertext = aes.encrypt(nonce, plaintext, associated_data=job_id.encode("utf-8"))

    dest_dir = LOCAL_DIR / "sessions" / job_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "session.enc"
    dest.write_bytes(nonce + ciphertext)

    return {"ok": True, "stored_at": str(dest), "size_bytes": len(plaintext)}


def load_session(*, job_id: str) -> dict[str, Any]:
    """Load + decrypt a session for a job. Returns the session_data or error."""
    key = _get_key()
    if key is None:
        return {"ok": False, "error": "vault_not_configured"}

    src = LOCAL_DIR / "sessions" / job_id / "session.enc"
    if not src.exists():
        return {"ok": False, "error": "session_not_found", "job_id": job_id}

    blob = src.read_bytes()
    nonce, ciphertext = blob[:12], blob[12:]

    aes = AESGCM(key)
    try:
        plaintext = aes.decrypt(nonce, ciphertext, associated_data=job_id.encode("utf-8"))
        return {"ok": True, "session_data": json.loads(plaintext.decode("utf-8"))}
    except Exception as e:
        log.exception("session decrypt failed for job %s", job_id)
        return {"ok": False, "error": "decrypt_failed", "type": type(e).__name__}


def delete_session(*, job_id: str) -> dict[str, Any]:
    """Securely delete a session after job completion. Overwrites then unlinks."""
    src = LOCAL_DIR / "sessions" / job_id / "session.enc"
    if not src.exists():
        return {"ok": True, "deleted": False, "reason": "not_found"}

    try:
        # Best-effort overwrite before unlink
        size = src.stat().st_size
        with open(src, "rb+") as f:
            f.write(secrets.token_bytes(size))
            f.flush()
            os.fsync(f.fileno())
        src.unlink()
        # Try to remove the job dir if empty
        try:
            src.parent.rmdir()
        except OSError:
            pass
        return {"ok": True, "deleted": True}
    except Exception as e:
        log.exception("session delete failed for job %s", job_id)
        return {"ok": False, "error": "delete_failed", "type": type(e).__name__, "message": str(e)}


def list_session_ids() -> list[str]:
    """List job IDs with stored sessions. For admin/cleanup tooling."""
    base = LOCAL_DIR / "sessions"
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())
