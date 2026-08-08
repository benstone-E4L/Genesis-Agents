"""Genesis job worker — polls for QUEUED jobs and executes them via agent_runtime."""
from __future__ import annotations
import asyncio, json, logging, os, signal, sys, time
from typing import Any
import httpx
from job_store import claim_job_by_id, claim_queued_jobs, heartbeat, update_job_status, expire_stale_running_jobs
from agent_runtime import AgentRuntime

log = logging.getLogger("worker")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

POLL_INTERVAL_S = float(os.getenv("WORKER_POLL_INTERVAL_S", "2"))
HEARTBEAT_INTERVAL_S = float(os.getenv("WORKER_HEARTBEAT_INTERVAL_S", "20"))
STALE_CHECK_INTERVAL_S = float(os.getenv("WORKER_STALE_CHECK_INTERVAL_S", "60"))
WORKER_CONCURRENCY = int(os.getenv("WORKER_CONCURRENCY", "3"))
CALLBACK_TIMEOUT_S = float(os.getenv("WORKER_CALLBACK_TIMEOUT_S", "10"))
CALLBACK_MAX_ATTEMPTS = int(os.getenv("WORKER_CALLBACK_MAX_ATTEMPTS", "3"))

_shutdown = False

# Worker state for /health/worker endpoint
_worker_state: dict[str, Any] = {
    "enabled": False,
    "last_tick_at": None,
    "last_claimed_job_id": None,
    "currently_running_job_id": None,
    "processed_count": 0,
    "failed_count": 0,
}


async def fire_callback(
    callback_url: str,
    *,
    job_id: str,
    status: str,
    output: str | None = None,
    error: str | None = None,
    external_escrow_id: str | None = None,
) -> bool:
    """POST job-completion payload to the marketplace callback URL.

    Retries up to CALLBACK_MAX_ATTEMPTS with exponential backoff. The
    X-Internal-Secret header is set from the INTERNAL_SECRET env var (the
    same shared secret the gateway uses for swarmsync-api calls), so the
    marketplace's InternalSecretGuard accepts the request.

    Returns True if a 2xx response was received, False otherwise.
    """
    payload = {
        "job_id": job_id,
        "status": status,
        "output": output,
        "error": error,
        "external_escrow_id": external_escrow_id,
    }
    headers = {"Content-Type": "application/json"}
    internal_secret = os.getenv("INTERNAL_SECRET", "")
    if internal_secret:
        headers["X-Internal-Secret"] = internal_secret

    backoff = 1.0
    for attempt in range(1, CALLBACK_MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=CALLBACK_TIMEOUT_S) as client:
                resp = await client.post(callback_url, json=payload, headers=headers)
            if 200 <= resp.status_code < 300:
                log.info(
                    "callback succeeded job=%s url=%s status=%d attempt=%d",
                    job_id, callback_url, resp.status_code, attempt,
                )
                return True
            log.warning(
                "callback non-2xx job=%s url=%s http=%d attempt=%d body=%s",
                job_id, callback_url, resp.status_code, attempt, resp.text[:200],
            )
        except Exception as exc:
            log.warning(
                "callback raise job=%s url=%s attempt=%d err=%s",
                job_id, callback_url, attempt, exc,
            )
        if attempt < CALLBACK_MAX_ATTEMPTS:
            await asyncio.sleep(backoff)
            backoff *= 2

    log.error(
        "callback unreachable after %d attempts job=%s url=%s",
        CALLBACK_MAX_ATTEMPTS, job_id, callback_url,
    )
    return False


def _handle_signal(sig, frame):
    global _shutdown
    log.info("received signal %s — initiating graceful shutdown", sig)
    _shutdown = True


async def process_job(job: dict[str, Any], runtime: AgentRuntime) -> None:
    job_id = job["id"]
    slug = job["agentSlug"]
    prompt = job["prompt"] or ""
    params = job["params"] or {}

    log.info("processing job %s (slug=%s)", job_id, slug)
    _worker_state["currently_running_job_id"] = job_id

    # Heartbeat coroutine
    async def hb_loop():
        while True:
            heartbeat(job_id)
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
    hb_task = asyncio.create_task(hb_loop())

    escrow_id = job.get("escrowId")
    callback_url = job.get("webhookUrl")
    # External-escrow detection: when a callback_url is set, the marketplace
    # owns the escrow and we MUST NOT call complete_escrow / release_escrow.
    # We fire the callback instead and let the marketplace settle/refund.
    is_external_escrow = bool(callback_url)

    # Track terminal state and output for the callback fire below.
    final_status: str | None = None
    final_output: str | None = None
    final_error: str | None = None

    try:
        result = await runtime.execute_agent(slug, prompt, params, job_id=job_id)
        if result.get("ok"):
            artifact_uris: list[str] = []
            _artifact_upload_ok = True
            _session_id = (result.get("trace") or {}).get("session_id")
            try:
                from pathlib import Path
                from artifact_store import upload_dir
                job_artifact_dir = Path(f"/tmp/jobs/{job_id}")
                if job_artifact_dir.exists():
                    upload_result = upload_dir(
                        job_id=job_id, local_dir=job_artifact_dir,
                        session_id=_session_id, agent_slug=slug,
                    )
                    if upload_result.get("ok"):
                        artifact_uris = [
                            f.get("signed_url") or f.get("uri", "")
                            for f in upload_result.get("files", [])
                            if f.get("ok")
                        ]
                        if artifact_uris:
                            log.info("job %s uploaded %d artifact(s)", job_id, len(artifact_uris))
                            # Back-fill artifact URIs onto the durable session.
                            if _session_id:
                                try:
                                    import durable_store
                                    durable_store.session_finish(
                                        _session_id, status="COMPLETED",
                                        artifact_uris=artifact_uris,
                                    )
                                except Exception:
                                    log.debug("session artifact back-fill failed for %s", job_id, exc_info=True)
            except Exception:
                _artifact_upload_ok = False
                log.exception("artifact upload failed for job %s — marking DELIVERED_WITH_ARTIFACT_WARNING", job_id)
            _delivery_status = "DELIVERED" if _artifact_upload_ok else "DELIVERED_WITH_ARTIFACT_WARNING"
            # Pack response + trace into resultSummary so poll clients can inspect tool calls.
            _response_text = str(result.get("response", ""))
            _trace = result.get("trace", {})
            _result_payload = json.dumps({
                "response": _response_text[:1800],
                "trace": _trace,
            })[:4000]
            update_job_status(
                job_id, _delivery_status,
                result_summary=_result_payload,
                output_artifact_uris=artifact_uris if artifact_uris else None,
            )
            log.info("job %s %s", job_id, _delivery_status)
            final_status = _delivery_status
            final_output = str(result.get("response", ""))
            _worker_state["processed_count"] += 1
            _worker_state["last_claimed_job_id"] = job_id
            # Phase 6 — settle the escrow on success, but ONLY when WE own it.
            # When the marketplace owns the escrow (callback_url is set), it
            # settles via its own /billing/escrow/:id/agent-callback handler.
            if escrow_id and not is_external_escrow:
                try:
                    # ESCROW BLOCKER 9/11 — escrow_client.complete_escrow.
                    # PERMANENTLY_PROHIBITED (contract 6.1 item 17). This is the
                    # single worst path in the repo: it settled escrow with
                    # status="SETTLED" on job success with NO human in the loop
                    # at any point. Automation paying is the exact inversion of
                    # the governing rule. The local import bypassed main.py's
                    # binding, so the guard is here at the call site.
                    from escrow_guard import escrow_blocked, escrow_permitted
                    if not escrow_permitted():
                        comp = escrow_blocked("complete_escrow")
                    else:
                        from escrow_client import complete_escrow
                        comp = await complete_escrow(escrow_id=escrow_id, status="SETTLED")
                    if comp.get("ok"):
                        log.info("job %s escrow %s SETTLED", job_id, escrow_id)
                    else:
                        log.warning(
                            "job %s escrow %s settle failed: %s",
                            job_id, escrow_id, comp.get("error"),
                        )
                except Exception:
                    log.exception("escrow_client.complete_escrow raised for job %s", job_id)
        else:
            update_job_status(
                job_id, "FAILED",
                error_code=result.get("error", "unknown"),
                error_message=str(result.get("message", ""))[:4000],
            )
            log.warning("job %s FAILED: %s", job_id, result.get("error"))
            final_status = "FAILED"
            final_error = str(result.get("error", "agent_failure"))
            _worker_state["failed_count"] += 1
            # Phase 6 — refund the escrow on agent failure, but ONLY when WE own it.
            if escrow_id and not is_external_escrow:
                try:
                    # ESCROW BLOCKER 10/11 — escrow_client.release_escrow
                    # (refund on agent failure). PERMANENTLY_PROHIBITED
                    # (contract 6.1 item 18): a refund is still automation
                    # moving money. Guarded at the call site because the local
                    # import bypasses main.py's binding.
                    from escrow_guard import escrow_blocked, escrow_permitted
                    if not escrow_permitted():
                        rel = escrow_blocked("release_escrow")
                    else:
                        from escrow_client import release_escrow
                        rel = await release_escrow(
                            escrow_id=escrow_id,
                            reason=str(result.get("error", "agent_failure")),
                        )
                    if rel.get("ok"):
                        log.info("job %s escrow %s REFUNDED", job_id, escrow_id)
                    else:
                        log.warning(
                            "job %s escrow %s release failed: %s",
                            job_id, escrow_id, rel.get("error"),
                        )
                except Exception:
                    log.exception("escrow_client.release_escrow raised for job %s", job_id)
    except Exception as e:
        log.exception("job %s raised", job_id)
        update_job_status(
            job_id, "FAILED",
            error_code=type(e).__name__,
            error_message=str(e)[:4000],
        )
        final_status = "FAILED"
        final_error = f"runtime_exception:{type(e).__name__}: {e}"
        _worker_state["failed_count"] += 1
        # Phase 6 — refund the escrow on runtime exception, but ONLY when WE own it.
        if escrow_id and not is_external_escrow:
            try:
                # ESCROW BLOCKER 11/11 — escrow_client.release_escrow (refund on
                # runtime exception). PERMANENTLY_PROHIBITED (contract 6.1 item
                # 18). Guarded at the call site because the local import
                # bypasses main.py's binding.
                from escrow_guard import escrow_blocked, escrow_permitted
                if not escrow_permitted():
                    escrow_blocked("release_escrow")
                else:
                    from escrow_client import release_escrow
                    await release_escrow(
                        escrow_id=escrow_id,
                        reason=f"runtime_exception:{type(e).__name__}",
                    )
            except Exception:
                log.exception("escrow release raised for job %s", job_id)
    finally:
        _worker_state["currently_running_job_id"] = None
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

        # Fire the marketplace callback if one is configured. Done LAST so the
        # job row + (optional) escrow state are already settled before the
        # marketplace queries them. Failures are logged but do not raise —
        # the marketplace can also poll /agents/jobs/{id} as a fallback.
        if callback_url and final_status:
            try:
                await fire_callback(
                    callback_url,
                    job_id=job_id,
                    status=final_status,
                    output=final_output,
                    error=final_error,
                    external_escrow_id=escrow_id if is_external_escrow else None,
                )
            except Exception:
                log.exception("fire_callback raised unexpectedly for job %s", job_id)


def _make_runtime() -> AgentRuntime:
    from main import _llm_api_key, _llm_api_url  # reuse the gateway's env-driven LLM config

    return AgentRuntime(llm_url=_llm_api_url(), llm_key=_llm_api_key())


async def execute_job_by_id(job_id: str) -> dict[str, Any]:
    """Claim and process a single QUEUED job (Trigger.dev event-driven path)."""
    expired = expire_stale_running_jobs(stale_minutes=5)
    job = claim_job_by_id(job_id)
    if not job:
        return {"ok": False, "job_id": job_id, "error": "not_queued_or_missing", "expired": expired}

    runtime = _make_runtime()
    await process_job(job, runtime)
    return {"ok": True, "job_id": job_id, "expired": expired}


async def run_tick(*, limit: int | None = None, expire_stale: bool = True) -> dict[str, Any]:
    """Process one worker iteration — used by Trigger.dev HTTP dispatch."""
    slots = limit if limit is not None else WORKER_CONCURRENCY
    slots = max(0, int(slots))

    expired = 0
    if expire_stale:
        expired = expire_stale_running_jobs(stale_minutes=5)
        if expired:
            log.info("expired %d stale RUNNING jobs", expired)

    if slots <= 0:
        return {"expired": expired, "claimed": 0, "processed": 0, "job_ids": []}

    jobs = claim_queued_jobs(limit=slots)
    if not jobs:
        return {"expired": expired, "claimed": 0, "processed": 0, "job_ids": []}

    runtime = _make_runtime()
    job_ids: list[str] = []
    for job in jobs:
        await process_job(job, runtime)
        job_ids.append(job["id"])

    return {
        "expired": expired,
        "claimed": len(jobs),
        "processed": len(job_ids),
        "job_ids": job_ids,
    }


async def main():
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    _worker_state["enabled"] = True
    runtime = _make_runtime()

    sem = asyncio.Semaphore(WORKER_CONCURRENCY)
    in_flight: set[asyncio.Task] = set()
    last_stale_check = 0.0

    log.info("worker started — concurrency=%d poll=%.1fs", WORKER_CONCURRENCY, POLL_INTERVAL_S)

    while not _shutdown:
        try:
            # Stale check
            now = time.time()
            if now - last_stale_check > STALE_CHECK_INTERVAL_S:
                expired = expire_stale_running_jobs(stale_minutes=5)
                if expired:
                    log.info("expired %d stale RUNNING jobs", expired)
                last_stale_check = now

            # Reap finished tasks
            done = {t for t in in_flight if t.done()}
            in_flight -= done

            # Claim up to (concurrency - in_flight) jobs
            slots = WORKER_CONCURRENCY - len(in_flight)
            if slots > 0:
                jobs = claim_queued_jobs(limit=slots)
                _worker_state["last_tick_at"] = time.time()
                for j in jobs:
                    task = asyncio.create_task(process_job(j, runtime))
                    in_flight.add(task)

            await asyncio.sleep(POLL_INTERVAL_S)
        except Exception:
            log.exception("worker loop error — sleeping 10s")
            await asyncio.sleep(10)

    log.info("waiting for %d in-flight jobs to finish before exit", len(in_flight))
    if in_flight:
        await asyncio.gather(*in_flight, return_exceptions=True)
    log.info("worker exiting")


if __name__ == "__main__":
    asyncio.run(main())
