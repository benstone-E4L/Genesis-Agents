"""Fire-and-forget Trigger.dev task dispatch for Genesis async jobs."""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

TRIGGER_API_URL = os.getenv("TRIGGER_API_URL", "https://api.trigger.dev").rstrip("/")
TASK_ID = "genesis-job-process"


def dispatch_genesis_job(job_id: str) -> bool:
    """Queue a Trigger.dev run for one genesis_jobs row. Best-effort; never raises."""
    secret = os.getenv("TRIGGER_SECRET_KEY", "").strip()
    if not secret:
        log.warning("TRIGGER_SECRET_KEY not set — genesis job %s will not auto-run", job_id)
        return False

    url = f"{TRIGGER_API_URL}/api/v1/tasks/{TASK_ID}/trigger"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Content-Type": "application/json",
                },
                json={"payload": {"jobId": job_id}},
            )
        if 200 <= resp.status_code < 300:
            log.info("triggered genesis-job-process for job=%s", job_id)
            return True
        log.warning(
            "trigger dispatch failed job=%s status=%s body=%s",
            job_id,
            resp.status_code,
            resp.text[:300],
        )
        return False
    except Exception:
        log.exception("trigger dispatch raised for job=%s", job_id)
        return False
