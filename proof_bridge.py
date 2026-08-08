"""Phase 7 — VCAP proof bundles.

After each agent invocation, calls conduit_bridge.export_proof() to get
a tarball of the audit chain, uploads it to S3 (or local fallback) via
artifact_store, computes input/output hashes, builds a VCAP wrapper JWT,
and persists a GenesisProof record in Postgres.
"""
from __future__ import annotations
import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    _CRYPTO_OK = True
except ImportError:
    log.warning("cryptography missing; proof signing degraded")
    Ed25519PrivateKey = None  # type: ignore
    _CRYPTO_OK = False


def _gateway_signing_key() -> Ed25519PrivateKey | None:
    """Load the gateway's Ed25519 signing key from env GENESIS_GATEWAY_PRIVKEY_B64.

    Generated once via:
      python -c "import base64; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; from cryptography.hazmat.primitives import serialization; k = Ed25519PrivateKey.generate(); print(base64.b64encode(k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode())"
    """
    if not _CRYPTO_OK:
        return None
    b64 = os.getenv("GENESIS_GATEWAY_PRIVKEY_B64")
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        return Ed25519PrivateKey.from_private_bytes(raw)
    except Exception:
        log.exception("invalid GENESIS_GATEWAY_PRIVKEY_B64")
        return None


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _gen_id() -> str:
    import uuid
    return "p" + uuid.uuid4().hex[:24]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_vcap_wrapper_jwt(payload: dict[str, Any]) -> str | None:
    """Build an EdDSA-signed JWT wrapping the proof reference.

    Header: {"alg": "EdDSA", "typ": "JWT"}
    Payload: {iat, jti, agent_slug, job_id, proof_bundle_uri, input_hash,
              output_hash, conduit_pubkey, conduit_session_id}
    Signature: Ed25519 over base64url(header) + '.' + base64url(payload)

    Falls back to an unsigned (alg=none) token if no signing key is configured;
    callers should treat that as dev-only.
    """
    key = _gateway_signing_key()
    if key is None:
        log.warning("no gateway signing key — emitting unsigned wrapper")
        header = {"alg": "none", "typ": "JWT"}
        h_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        p_b64 = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        return f"{h_b64}.{p_b64}."

    header = {"alg": "EdDSA", "typ": "JWT"}
    h_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_b64 = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{h_b64}.{p_b64}".encode("utf-8")
    sig = key.sign(signing_input)
    s_b64 = _b64url(sig)
    return f"{h_b64}.{p_b64}.{s_b64}"


def verify_vcap_wrapper_jwt(token: str, pubkey_b64: str | None = None) -> dict[str, Any]:
    """Verify the wrapper JWT signature.

    pubkey_b64 defaults to env GENESIS_GATEWAY_PUBKEY_B64.
    """
    if not _CRYPTO_OK:
        return {"ok": False, "error": "crypto_unavailable"}

    parts = token.split(".")
    if len(parts) != 3:
        return {"ok": False, "error": "invalid_jwt_shape"}
    h_b64, p_b64, s_b64 = parts

    # Decode payload (restore base64url padding)
    try:
        pad = "=" * (-len(p_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(p_b64 + pad))
    except Exception as e:
        return {"ok": False, "error": "payload_decode_failed", "message": str(e)}

    if not s_b64:
        return {"ok": True, "verified": False, "payload": payload, "note": "unsigned token"}

    pubkey_b64 = pubkey_b64 or os.getenv("GENESIS_GATEWAY_PUBKEY_B64", "")
    if not pubkey_b64:
        return {"ok": False, "error": "no_verifier_pubkey", "payload": payload}

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
        raw_pub = base64.b64decode(pubkey_b64)
        pub = Ed25519PublicKey.from_public_bytes(raw_pub)
        pad_s = "=" * (-len(s_b64) % 4)
        sig = base64.urlsafe_b64decode(s_b64 + pad_s)
        signing_input = f"{h_b64}.{p_b64}".encode("utf-8")
        pub.verify(sig, signing_input)
        return {"ok": True, "verified": True, "payload": payload}
    except InvalidSignature:
        return {"ok": False, "error": "signature_invalid", "payload": payload}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e), "payload": payload}


async def generate_proof_for_job(
    *,
    job_id: str,
    agent_slug: str,
    bridge: Any,
    job_dir: Path,
    input_data: dict[str, Any],
    output_data: dict[str, Any],
    started_at: float,
    completed_at: float,
    buyer_client_id: str | None = None,
    buyer_wallet_id: str | None = None,
) -> dict[str, Any]:
    """End-of-job proof generation pipeline.

    1. Hash input and output canonically.
    2. Call bridge.export_proof() -> tarball under job_dir/proof/.
    3. Upload tarball via artifact_store.upload_file.
    4. Build VCAP wrapper JWT.
    5. Persist GenesisProof record (best-effort; non-fatal if Postgres absent).

    Returns {ok, proof_id, proof_bundle_uri, signed_url, vcap_wrapper_jwt,
             input_hash, output_hash} or {ok: False, error, ...}.
    """
    try:
        input_bytes = json.dumps(input_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        output_bytes = json.dumps(output_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        input_hash = _sha256_hex(input_bytes)
        output_hash = _sha256_hex(output_bytes)

        # 2. Export Conduit proof bundle
        proof_dir = job_dir / "proof"
        proof_dir.mkdir(parents=True, exist_ok=True)
        proof_path = proof_dir / "proof.tar.gz"

        if bridge is None:
            return {
                "ok": False,
                "error": "no_bridge",
                "message": "ConduitBridge required for proof export",
            }

        try:
            # ConduitBridge.export_proof is synchronous; it returns
            # {"success": True, "path": str, "action_count": int, ...}
            # or {"success": False, "error": ...}.
            export_result = bridge.export_proof(output_dir=str(proof_dir))
            actual_path: Path | None = None
            if isinstance(export_result, dict):
                if not export_result.get("success", True) and export_result.get("error"):
                    return {
                        "ok": False,
                        "error": "proof_export_failed",
                        "message": str(export_result.get("error")),
                    }
                cand = (
                    export_result.get("path")
                    or export_result.get("output_path")
                    or str(proof_path)
                )
                actual_path = Path(cand)
            else:
                actual_path = proof_path

            if actual_path is None or not actual_path.exists():
                if proof_path.exists():
                    actual_path = proof_path
                else:
                    return {
                        "ok": False,
                        "error": "proof_export_no_file",
                        "expected": str(proof_path),
                    }
        except Exception as e:
            log.exception("Conduit proof export failed for job %s", job_id)
            return {
                "ok": False,
                "error": "proof_export_failed",
                "type": type(e).__name__,
                "message": str(e),
            }

        # 3. Upload to S3 (or local fallback)
        from artifact_store import upload_file
        upload_result = upload_file(
            job_id=job_id,
            local_path=actual_path,
            object_name="proof.tar.gz",
            content_type="application/gzip",
        )
        if not upload_result.get("ok"):
            log.warning("proof upload failed for %s: %s", job_id, upload_result)
            # Continue anyway - we have the local proof; just no signed URL.

        # 4. Conduit metadata (best-effort)
        conduit_session_id = getattr(bridge, "_session_id", None) or getattr(
            bridge, "session_id", "unknown"
        )
        conduit_pubkey = ""
        try:
            identity = getattr(bridge, "_identity", None)
            if identity is not None:
                conduit_pubkey = getattr(identity, "public_key_hex", "") or ""
            if not conduit_pubkey:
                conduit_pubkey = getattr(bridge, "public_key_b64", "") or ""
        except Exception:
            conduit_pubkey = ""

        # 5. Build VCAP wrapper JWT
        jwt_payload = {
            "iat": int(time.time()),
            "jti": _gen_id(),
            "agent_slug": agent_slug,
            "job_id": job_id,
            "proof_bundle_uri": upload_result.get("uri", f"file://{actual_path}"),
            "input_hash": input_hash,
            "output_hash": output_hash,
            "conduit_pubkey": conduit_pubkey,
            "conduit_session_id": conduit_session_id,
            "duration_s": round(completed_at - started_at, 3),
        }
        wrapper_jwt = build_vcap_wrapper_jwt(jwt_payload)

        # 6. Persist (best-effort)
        proof_id = _persist_proof(
            job_id=job_id,
            agent_slug=agent_slug,
            buyer_client_id=buyer_client_id,
            buyer_wallet_id=buyer_wallet_id,
            proof_bundle_uri=upload_result.get("uri", ""),
            proof_bundle_signed_url=upload_result.get("signed_url", ""),
            conduit_session_id=conduit_session_id,
            conduit_pubkey=conduit_pubkey,
            input_hash=input_hash,
            output_hash=output_hash,
            vcap_jwt=wrapper_jwt or "",
            duration_s=completed_at - started_at,
        )

        return {
            "ok": True,
            "proof_id": proof_id,
            "proof_bundle_uri": upload_result.get("uri"),
            "signed_url": upload_result.get("signed_url"),
            "vcap_wrapper_jwt": wrapper_jwt,
            "input_hash": input_hash,
            "output_hash": output_hash,
        }
    except Exception as e:
        log.exception("proof generation failed for job %s", job_id)
        return {
            "ok": False,
            "error": "proof_pipeline_exception",
            "type": type(e).__name__,
            "message": str(e),
        }


def _persist_proof(**fields: Any) -> str | None:
    """Persist a GenesisProof record. Returns id or None if DB unavailable."""
    try:
        import psycopg
        from psycopg.rows import dict_row
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            return None
        proof_id = _gen_id()
        with psycopg.connect(db_url, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO genesis_proofs
                  (id, "jobId", "agentSlug", "buyerClientId", "buyerWalletId",
                   "proofBundleUri", "proofBundleSignedUrl", "conduitSessionId",
                   "conduitPublicKey", "inputHash", "outputHash", "vcapWrapperJwt",
                   "durationSeconds", "createdAt")
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT ("jobId") DO UPDATE SET
                  "proofBundleUri" = EXCLUDED."proofBundleUri",
                  "vcapWrapperJwt" = EXCLUDED."vcapWrapperJwt"
                RETURNING id
                """,
                (
                    proof_id, fields["job_id"], fields["agent_slug"],
                    fields["buyer_client_id"], fields["buyer_wallet_id"],
                    fields["proof_bundle_uri"], fields["proof_bundle_signed_url"],
                    fields["conduit_session_id"], fields["conduit_pubkey"],
                    fields["input_hash"], fields["output_hash"],
                    fields["vcap_jwt"], fields["duration_s"],
                ),
            )
            row = cur.fetchone()
            conn.commit()

            # Link back to job
            if fields.get("job_id"):
                cur.execute(
                    'UPDATE genesis_jobs SET "proofId" = %s WHERE id = %s',
                    (row["id"], fields["job_id"]),
                )
                conn.commit()

            return row["id"]
    except Exception:
        log.exception("proof persist failed; non-fatal")
        return None
