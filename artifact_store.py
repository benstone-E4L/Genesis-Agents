"""Artifact store - uploads job outputs to S3 with signed read URLs.

Falls back to a local Render persistent disk path if AWS credentials
are absent. Phase 9 of the marketplace plan.

Env vars:
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION
  GENESIS_S3_BUCKET (default: swarmsync-genesis-artifacts)
  GENESIS_LOCAL_ARTIFACT_DIR (fallback: /var/data/genesis-artifacts)
  GENESIS_SIGNED_URL_TTL_SECONDS (default: 604800 = 7 days)
"""
from __future__ import annotations
import hashlib, logging, mimetypes, os, shutil
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _guess_mime(name: str, fallback: str) -> str:
    guessed, _ = mimetypes.guess_type(name)
    return guessed or fallback

S3_BUCKET = os.getenv("GENESIS_S3_BUCKET", "swarmsync-genesis-artifacts")
LOCAL_DIR = Path(os.getenv("GENESIS_LOCAL_ARTIFACT_DIR", "/var/data/genesis-artifacts"))
SIGNED_URL_TTL_S = int(os.getenv("GENESIS_SIGNED_URL_TTL_SECONDS", str(7 * 24 * 3600)))

try:
    import boto3
    from botocore.exceptions import ClientError, BotoCoreError
    _BOTO_AVAILABLE = True
except ImportError:
    log.warning("boto3 not installed; artifact_store will use local disk only")
    boto3 = None  # type: ignore
    _BOTO_AVAILABLE = False


def _s3_available() -> bool:
    """True iff boto3 is importable AND we have AWS creds in env."""
    if not _BOTO_AVAILABLE:
        return False
    return bool(os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"))


def _s3_client():
    kwargs: dict = {
        "aws_access_key_id": os.getenv("AWS_ACCESS_KEY_ID"),
        "aws_secret_access_key": os.getenv("AWS_SECRET_ACCESS_KEY"),
        "region_name": os.getenv("AWS_REGION", "us-east-1"),
    }
    # Cloudflare R2 (and any other S3-compatible store) requires endpoint_url.
    endpoint = os.getenv("GENESIS_S3_ENDPOINT")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


def upload_file(
    *,
    job_id: str,
    local_path: Path,
    object_name: str | None = None,
    content_type: str = "application/octet-stream",
) -> dict[str, Any]:
    """Upload a single file. Returns {ok, uri, signed_url, expires_at} or {ok: False, error: ...}."""
    if not local_path.exists():
        return {"ok": False, "error": "local_file_missing", "path": str(local_path)}

    name = object_name or local_path.name
    key = f"{job_id}/{name}"
    mime_type = _guess_mime(name, content_type)
    size_bytes = local_path.stat().st_size
    sha256 = _sha256(local_path)
    meta = {"filename": name, "mime_type": mime_type,
            "size_bytes": size_bytes, "sha256": sha256}

    if _s3_available():
        try:
            client = _s3_client()
            client.upload_file(
                str(local_path), S3_BUCKET, key,
                ExtraArgs={"ContentType": mime_type},
            )
            signed_url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": S3_BUCKET, "Key": key},
                ExpiresIn=SIGNED_URL_TTL_S,
            )
            return {
                "ok": True,
                "backend": "s3",
                "uri": f"s3://{S3_BUCKET}/{key}",
                "signed_url": signed_url,
                "expires_in_seconds": SIGNED_URL_TTL_S,
                **meta,
            }
        except (ClientError, BotoCoreError):
            log.exception("S3 upload failed; falling back to local")
            # Fall through to local path

    # Local fallback
    dest = LOCAL_DIR / job_id / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local_path, dest)
    return {
        "ok": True,
        "backend": "local",
        "uri": f"file://{dest}",
        "signed_url": f"/artifacts/{job_id}/{name}",  # gateway endpoint serves these
        "expires_in_seconds": None,
        **meta,
    }


def upload_dir(
    *,
    job_id: str,
    local_dir: Path,
    session_id: str | None = None,
    agent_slug: str | None = None,
) -> dict[str, Any]:
    """Upload every file in a directory under {job_id}/. Returns {ok, files: [...]}.

    Each uploaded file is also recorded in the durable genesis_artifacts table
    (sha256 / size / mime / uri / signed_url) so artifacts are retrievable with
    integrity metadata after the job and after restart (Phase 4).
    """
    if not local_dir.exists() or not local_dir.is_dir():
        return {"ok": False, "error": "dir_missing", "path": str(local_dir)}

    try:
        import durable_store
    except Exception:  # noqa: BLE001
        durable_store = None  # type: ignore

    # Internal runtime dirs are not buyer artifacts — exclude them so the
    # artifact contract reflects genuine agent output only.
    _EXCLUDE_TOP = {"logs", "conduit", "__pycache__", ".git"}

    results: list[dict[str, Any]] = []
    for entry in local_dir.rglob("*"):
        if entry.is_file():
            rel = entry.relative_to(local_dir).as_posix()
            if rel.split("/", 1)[0] in _EXCLUDE_TOP:
                continue
            r = upload_file(job_id=job_id, local_path=entry, object_name=rel)
            results.append(r)
            if durable_store is not None and r.get("ok"):
                try:
                    durable_store.artifact_record(
                        job_id=job_id, path=rel, filename=r.get("filename", rel),
                        session_id=session_id, agent_slug=agent_slug,
                        mime_type=r.get("mime_type"), size_bytes=r.get("size_bytes"),
                        sha256=r.get("sha256"), storage_backend=r.get("backend"),
                        uri=r.get("uri"), signed_url=r.get("signed_url"),
                    )
                except Exception:  # noqa: BLE001
                    log.debug("artifact_record failed job=%s file=%s", job_id, rel, exc_info=True)

    return {
        "ok": True,
        "files": results,
        "backend": results[0]["backend"] if results else "none",
    }


def get_signed_url(*, job_id: str, name: str) -> dict[str, Any]:
    """Issue a fresh signed URL for an existing artifact."""
    key = f"{job_id}/{name}"
    if _s3_available():
        try:
            client = _s3_client()
            url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": S3_BUCKET, "Key": key},
                ExpiresIn=SIGNED_URL_TTL_S,
            )
            return {"ok": True, "signed_url": url, "expires_in_seconds": SIGNED_URL_TTL_S}
        except (ClientError, BotoCoreError) as e:
            return {"ok": False, "error": "s3_error", "message": str(e)}

    # Local fallback - return the gateway path
    return {
        "ok": True,
        "signed_url": f"/artifacts/{job_id}/{name}",
        "expires_in_seconds": None,
    }


def list_artifacts(*, job_id: str) -> dict[str, Any]:
    """List all artifacts for a job."""
    if _s3_available():
        try:
            client = _s3_client()
            resp = client.list_objects_v2(Bucket=S3_BUCKET, Prefix=f"{job_id}/")
            items = [
                {"name": obj["Key"].split("/", 1)[1], "size": obj["Size"]}
                for obj in resp.get("Contents", [])
            ]
            return {"ok": True, "backend": "s3", "items": items}
        except (ClientError, BotoCoreError) as e:
            return {"ok": False, "error": "s3_error", "message": str(e)}

    job_dir = LOCAL_DIR / job_id
    if not job_dir.exists():
        return {"ok": True, "backend": "local", "items": []}
    items = [
        {"name": str(p.relative_to(job_dir)), "size": p.stat().st_size}
        for p in job_dir.rglob("*") if p.is_file()
    ]
    return {"ok": True, "backend": "local", "items": items}
