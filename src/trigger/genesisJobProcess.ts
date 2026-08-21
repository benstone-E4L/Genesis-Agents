import { logger, task } from "@trigger.dev/sdk";

/**
 * Real Trigger.dev task backing `trigger_dispatch.py`'s
 * `POST /api/v1/tasks/genesis-job-process/trigger` call (see main.py:2448-2450).
 *
 * When a Genesis job is created, the FastAPI gateway fires this task with
 * `{ payload: { jobId } }`. This task's job is to call back into the
 * gateway's internal worker-execute endpoint
 * (`POST {GENESIS_BASE_URL}/internal/genesis-worker/jobs/{jobId}/execute`,
 * guarded by `X-Internal-Secret` — see main.py:2692-2714 / worker.py's
 * `execute_job_by_id`), which claims and runs the QUEUED job synchronously
 * and returns its result.
 *
 * GENESIS_BASE_URL / INTERNAL_SECRET are deliberately read from the
 * Trigger.dev environment's own env vars (set per-environment in the
 * Trigger.dev dashboard, or via `trigger.dev deploy`'s env passthrough),
 * NOT hardcoded here. If they are not configured (e.g. this local dev
 * environment has neither set), the task still runs for real — it just
 * cannot reach a live Genesis gateway, and says so explicitly instead of
 * silently no-op'ing.
 */
export const genesisJobProcessTask = task({
  id: "genesis-job-process",
  run: async (payload: { jobId?: string } = {}) => {
    const jobId = payload.jobId;
    logger.log("genesis-job-process task running", { jobId });

    const baseUrl = process.env.GENESIS_BASE_URL;
    const internalSecret = process.env.INTERNAL_SECRET;

    if (!jobId) {
      return { ok: false, reason: "no_job_id", jobId: null };
    }

    if (!baseUrl || !internalSecret) {
      logger.warn(
        "GENESIS_BASE_URL / INTERNAL_SECRET not set in this Trigger.dev environment — " +
          "cannot call the Genesis gateway. Task ran for real but has nothing to call.",
      );
      return {
        ok: false,
        reason: "genesis_gateway_not_configured",
        jobId,
        missing: [
          !baseUrl ? "GENESIS_BASE_URL" : null,
          !internalSecret ? "INTERNAL_SECRET" : null,
        ].filter(Boolean),
      };
    }

    const url = `${baseUrl.replace(/\/$/, "")}/internal/genesis-worker/jobs/${jobId}/execute`;
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Internal-Secret": internalSecret,
      },
    });

    const body = await resp.json().catch(() => ({}));
    logger.log("genesis-job-process gateway response", { status: resp.status, body });

    return { ok: resp.ok, status: resp.status, jobId, body };
  },
});
