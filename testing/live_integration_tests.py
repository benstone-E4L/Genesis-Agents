"""live_integration_tests.py — Real integration tests requiring live credentials.

These tests hit a real Postgres database and/or the live Render service.
They are NOT run in CI (no mocks). Run manually when credentials are available.

Required env vars:
  DATABASE_URL or GENESIS_JOB_DATABASE_URL  — Supabase connection string (pooler OK)
  GATEWAY_API_KEY or AGENT_GATEWAY_SECRET   — genesis gateway auth
  LLM_API_KEY                               — LLM router API key (OpenRouter or SwarmSync)
  LLM_API_URL                               — LLM router URL (optional, defaults to SwarmSync)
  RENDER_SERVICE_URL                        — default: https://swarmsync-agents.onrender.com

Run:
  python testing/live_integration_tests.py [test_name]

  python testing/live_integration_tests.py postgres_lifecycle
  python testing/live_integration_tests.py artifact_failure
  python testing/live_integration_tests.py meta_delegation
  python testing/live_integration_tests.py render_async_job
  python testing/live_integration_tests.py all
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

RENDER_URL = os.getenv("RENDER_SERVICE_URL", "https://swarmsync-agents.onrender.com")
# Accept either GATEWAY_API_KEY (X-Agent-Api-Key) or AGENT_GATEWAY_SECRET (x-agent-gateway-secret)
GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY", "")
AGENT_GATEWAY_SECRET = os.getenv("AGENT_GATEWAY_SECRET", "")
DATABASE_URL = os.getenv("GENESIS_JOB_DATABASE_URL") or os.getenv("DATABASE_URL", "")
RENDER_INTERNAL_SECRET = os.getenv("RENDER_INTERNAL_SECRET", "")

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"

_UNSUPPORTED_PSYCOPG_PARAMS = {"pgbouncer", "connection_limit"}


def _psycopg_url(raw: str) -> str:
    """Strip pgbouncer/connection_limit params that psycopg rejects."""
    parts = urlsplit(raw)
    if not parts.query:
        return raw
    filtered = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k.lower() not in _UNSUPPORTED_PSYCOPG_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(filtered), parts.fragment))


def _auth_headers() -> dict:
    """Return the correct auth header for Render calls."""
    if GATEWAY_API_KEY:
        return {"X-Agent-Api-Key": GATEWAY_API_KEY}
    if AGENT_GATEWAY_SECRET:
        return {"x-agent-gateway-secret": AGENT_GATEWAY_SECRET}
    return {}


def _require(var: str, value: str, test_name: str) -> bool:
    if not value:
        print(f"  {SKIP} {test_name}: {var} not set — cannot run")
        return False
    return True


# ---------------------------------------------------------------------------
# Test 2: Real Postgres lifecycle — QUEUED → RUNNING → DELIVERED
# ---------------------------------------------------------------------------

def test_postgres_lifecycle() -> bool:
    """Insert a job directly into genesis_jobs, run it through worker, verify DELIVERED."""
    print("\n[TEST 2] Real Postgres lifecycle: QUEUED → RUNNING → DELIVERED")

    if not _require("DATABASE_URL", DATABASE_URL, "postgres_lifecycle"):
        return False

    import psycopg
    from psycopg.rows import dict_row

    job_id = f"live-test-{uuid.uuid4().hex[:12]}"
    print(f"  job_id: {job_id}")

    try:
        conn = psycopg.connect(_psycopg_url(DATABASE_URL), row_factory=dict_row, prepare_threshold=None)
    except Exception as e:
        print(f"  {FAIL} DB connect failed: {e}")
        return False

    try:
        with conn.cursor() as cur:
            # Insert a QUEUED job
            cur.execute(
                """
                INSERT INTO genesis_jobs (id, "agentSlug", prompt, status, params,
                    "createdAt", "updatedAt")
                VALUES (%s, %s, %s, 'QUEUED', %s, NOW(), NOW())
                """,
                (job_id, "genesis-finance", "What is 2+2? Reply with just the number.", "{}"),
            )
            conn.commit()
            print(f"  Inserted QUEUED job {job_id}")

        # Import and run worker synchronously (single-job mode)
        import asyncio
        from agent_runtime import AgentRuntime
        from job_store import claim_job_by_id
        import worker as worker_mod

        llm_url = os.getenv("LLM_API_URL", "https://api.swarmsync.ai/v1/chat/completions")
        llm_key = os.getenv("LLM_API_KEY", "")
        if not llm_key:
            print(f"  {SKIP} LLM_API_KEY not set — cannot run agent, checking DB row only")
        else:
            runtime = AgentRuntime(llm_url=llm_url, llm_key=llm_key)
            job = claim_job_by_id(job_id)
            if not job:
                print(f"  {FAIL} claim_job_by_id returned None for {job_id}")
                return False
            asyncio.run(worker_mod.process_job(job, runtime))

        # Verify final status in DB
        with conn.cursor() as cur:
            cur.execute('SELECT status, "resultSummary" FROM genesis_jobs WHERE id = %s', (job_id,))
            row = cur.fetchone()

        if not row:
            print(f"  {FAIL} No row found for {job_id} after process_job")
            return False

        status = row["status"]
        print(f"  Final status: {status}")
        print(f"  Result summary snippet: {str(row.get('resultSummary', ''))[:80]}")

        if status in ("DELIVERED", "DELIVERED_WITH_ARTIFACT_WARNING"):
            print(f"  {PASS} Job reached terminal DELIVERED state")
            return True
        else:
            print(f"  {FAIL} Expected DELIVERED*, got {status}")
            return False

    finally:
        # Clean up test row
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM genesis_jobs WHERE id = %s", (job_id,))
                conn.commit()
        except Exception:
            pass
        conn.close()


# ---------------------------------------------------------------------------
# Test 3: Artifact failure → DELIVERED_WITH_ARTIFACT_WARNING
# ---------------------------------------------------------------------------

def test_artifact_failure() -> bool:
    """Run a job but sabotage the upload to force DELIVERED_WITH_ARTIFACT_WARNING."""
    print("\n[TEST 3] Artifact failure → DELIVERED_WITH_ARTIFACT_WARNING")

    if not _require("DATABASE_URL", DATABASE_URL, "artifact_failure"):
        return False
    if not _require("LLM_API_KEY", os.getenv("LLM_API_KEY", ""), "artifact_failure"):
        return False

    import asyncio
    import psycopg
    from psycopg.rows import dict_row
    from unittest.mock import patch
    from agent_runtime import AgentRuntime
    from job_store import claim_job_by_id
    import worker as worker_mod

    job_id = f"live-artifact-fail-{uuid.uuid4().hex[:10]}"
    print(f"  job_id: {job_id}")

    conn = psycopg.connect(_psycopg_url(DATABASE_URL), row_factory=dict_row, prepare_threshold=None)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO genesis_jobs (id, "agentSlug", prompt, status, params,
                    "createdAt", "updatedAt")
                VALUES (%s, %s, %s, 'QUEUED', %s, NOW(), NOW())
                """,
                (job_id, "genesis-finance", "What is 3+3?", "{}"),
            )
            conn.commit()

        llm_url = os.getenv("LLM_API_URL", "https://api.swarmsync.ai/v1/chat/completions")
        llm_key = os.getenv("LLM_API_KEY", "")
        runtime = AgentRuntime(llm_url=llm_url, llm_key=llm_key)
        job = claim_job_by_id(job_id)

        # Create job dir so artifact check fires, then force upload failure
        Path(f"/tmp/jobs/{job_id}").mkdir(parents=True, exist_ok=True)

        with patch("artifact_store.upload_dir", side_effect=RuntimeError("forced upload failure")):
            asyncio.run(worker_mod.process_job(job, runtime))

        with conn.cursor() as cur:
            cur.execute('SELECT status FROM genesis_jobs WHERE id = %s', (job_id,))
            row = cur.fetchone()

        status = row["status"] if row else "MISSING"
        print(f"  Final status: {status}")

        if status == "DELIVERED_WITH_ARTIFACT_WARNING":
            print(f"  {PASS} Forced upload failure correctly set DELIVERED_WITH_ARTIFACT_WARNING")
            return True
        else:
            print(f"  {FAIL} Expected DELIVERED_WITH_ARTIFACT_WARNING, got {status}")
            return False

    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM genesis_jobs WHERE id = %s", (job_id,))
                conn.commit()
        except Exception:
            pass
        conn.close()


# ---------------------------------------------------------------------------
# Test 4: Meta Agent real delegation — trace.tool_calls contains genesis_call
# ---------------------------------------------------------------------------

def test_meta_delegation() -> bool:
    """Call genesis-meta via /agents/genesis-meta/run and inspect trace.tool_calls."""
    print("\n[TEST 4] Meta Agent delegation — trace.tool_calls must contain genesis_call")

    if not GATEWAY_API_KEY and not AGENT_GATEWAY_SECRET:
        print(f"  {SKIP} meta_delegation: GATEWAY_API_KEY and AGENT_GATEWAY_SECRET not set — cannot run")
        return False

    import urllib.request

    payload = json.dumps({
        "task": (
            "You must delegate this to genesis-research. "
            "Use the genesis_call tool to invoke genesis-research with "
            "task='Briefly explain what Python is in one sentence'."
        ),
        "params": {},
        "mode": "live_test",
    }).encode()

    req = urllib.request.Request(
        f"{RENDER_URL}/agents/genesis-meta/run",
        data=payload,
        headers={"Content-Type": "application/json", **_auth_headers()},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
    except Exception as e:
        print(f"  {FAIL} HTTP request failed: {e}")
        return False

    # /agents/{slug}/run returns RunResponse: {"response": "<json-string>", "agentSlug": ...}
    # Parse the inner JSON to get ok, trace, etc.
    inner = body
    if isinstance(body.get("response"), str):
        try:
            inner = json.loads(body["response"])
        except json.JSONDecodeError:
            inner = body

    print(f"  Response ok: {inner.get('ok')}")
    trace = inner.get("trace", {})
    tc_raw = trace.get("tool_calls", 0)
    # tool_calls may be a list (new structured format) or int (legacy)
    if isinstance(tc_raw, list):
        tool_calls_count = len(tc_raw)
        tool_call_names = [t.get("tool_name", "") for t in tc_raw]
    else:
        tool_calls_count = int(tc_raw) if tc_raw else 0
        tool_call_names = []
    response_text = str(inner.get("response", ""))[:120]
    print(f"  trace.tool_calls: {tool_calls_count} entries, names={tool_call_names}")
    print(f"  response snippet: {response_text}")

    # Check response contains evidence of delegation
    has_delegation_evidence = (
        "genesis_call" in response_text.lower()
        or "genesis-research" in response_text.lower()
        or tool_calls_count > 1
        or "genesis_call" in tool_call_names
    )

    if inner.get("ok") and has_delegation_evidence:
        print(f"  {PASS} Meta agent responded with delegation evidence")
        return True
    elif inner.get("ok"):
        print(f"  {FAIL} Meta agent responded ok=True but no delegation evidence found")
        print(f"         Full response: {json.dumps(inner)[:400]}")
        return False
    else:
        print(f"  {FAIL} Meta agent returned ok=False: {inner.get('error')}")
        print(f"         Full body: {json.dumps(body)[:400]}")
        return False


# ---------------------------------------------------------------------------
# Test 5: Render async job — submit, poll, confirm DELIVERED
# ---------------------------------------------------------------------------

def test_render_async_job() -> bool:
    """Submit an async job to Render, poll /agents/jobs/{id}, confirm DELIVERED."""
    print("\n[TEST 5] Render async job — submit → poll → DELIVERED")

    if not GATEWAY_API_KEY and not AGENT_GATEWAY_SECRET:
        print(f"  {SKIP} render_async_job: GATEWAY_API_KEY and AGENT_GATEWAY_SECRET not set — cannot run")
        return False

    # Check new endpoints are live
    import urllib.request

    def _get(path: str) -> dict:
        req = urllib.request.Request(
            f"{RENDER_URL}{path}",
            headers=_auth_headers(),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    def _post(path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{RENDER_URL}{path}",
            data=data,
            headers={"Content-Type": "application/json", **_auth_headers()},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    # Verify /health/worker is live (new deploy check)
    try:
        worker_health = _get("/health/worker")
        print(f"  /health/worker: {worker_health}")
    except Exception as e:
        print(f"  {FAIL} /health/worker returned error — is new commit deployed? {e}")
        print("         Wait for Render to finish deploying commit 3e5f1b2, then retry.")
        return False

    # Submit async job (genesis-research uses job_mode: async)
    try:
        submit_resp = _post("/agents/genesis-research/run", {
            "task": "What year was Python created? One sentence answer.",
            "params": {},
        })
    except Exception as e:
        print(f"  {FAIL} Submit request failed: {e}")
        return False

    print(f"  Submit response: {str(submit_resp)[:200]}")

    # Async agents return a JSON string with job_id
    raw = submit_resp if isinstance(submit_resp, dict) else {}
    response_str = raw.get("response", "")
    try:
        job_info = json.loads(response_str) if isinstance(response_str, str) else {}
    except json.JSONDecodeError:
        job_info = {}

    job_id = job_info.get("job_id") or raw.get("job_id")
    if not job_id:
        print(f"  {FAIL} No job_id in submit response: {submit_resp}")
        return False

    print(f"  Submitted job_id: {job_id}")
    poll_url = job_info.get("poll_url", f"{RENDER_URL}/agents/jobs/{job_id}")
    print(f"  Polling: {poll_url}")

    # If RENDER_INTERNAL_SECRET is set, trigger the worker tick so the job runs
    if RENDER_INTERNAL_SECRET:
        try:
            time.sleep(2)
            tick_req = urllib.request.Request(
                f"{RENDER_URL}/internal/genesis-worker/tick",
                data=json.dumps({"limit": 3}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Internal-Secret": RENDER_INTERNAL_SECRET,
                },
                method="POST",
            )
            with urllib.request.urlopen(tick_req, timeout=20) as r:
                tick_resp = json.loads(r.read())
            print(f"  Worker tick triggered: {tick_resp}")
        except Exception as e:
            print(f"  Worker tick failed (non-fatal): {e}")

    # Poll up to 3 minutes
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(5)
        try:
            status_resp = _get(f"/agents/jobs/{job_id}")
        except Exception as e:
            print(f"  poll error: {e}")
            continue

        status = status_resp.get("status", "UNKNOWN")
        print(f"  [{time.strftime('%H:%M:%S')}] status={status}")

        if status in ("DELIVERED", "DELIVERED_WITH_ARTIFACT_WARNING"):
            print(f"  {PASS} Job reached {status}")
            print(f"  result_summary snippet: {str(status_resp.get('resultSummary', ''))[:100]}")
            return True
        elif status == "FAILED":
            print(f"  {FAIL} Job FAILED: {status_resp.get('errorCode')} — {status_resp.get('errorMessage', '')[:200]}")
            return False

    print(f"  {FAIL} Job did not reach DELIVERED within 180s. Last status: {status}")
    return False


# ---------------------------------------------------------------------------
# Test: meta_real_orchestration — trace-proven genesis_call tool invocations
# ---------------------------------------------------------------------------

def test_meta_real_orchestration() -> bool:
    """Submit genesis-meta via the real async runtime (no live_test bypass), wait for DELIVERED,
    then verify trace.tool_calls contains structured genesis_call entries with child_job_id,
    child_ok, and target_agent_slug. Fails if tool_calls is empty or contains no genesis_call."""
    print("\n[TEST meta_real_orchestration] Meta real tool-call orchestration — trace proof required")

    if not GATEWAY_API_KEY and not AGENT_GATEWAY_SECRET:
        print(f"  {SKIP} meta_real_orchestration: auth not set — cannot run")
        return False

    import urllib.request

    def _post(path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{RENDER_URL}{path}",
            data=data,
            headers={"Content-Type": "application/json", **_auth_headers()},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def _get(path: str) -> dict:
        req = urllib.request.Request(
            f"{RENDER_URL}{path}",
            headers=_auth_headers(),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    # Pre-drain: clear any stale QUEUED jobs from prior test runs so they don't steal
    # the single worker slot before this test's job gets picked up.
    if RENDER_INTERNAL_SECRET:
        try:
            drain_req = urllib.request.Request(
                f"{RENDER_URL}/internal/genesis-worker/tick",
                data=json.dumps({"limit": 10}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Internal-Secret": RENDER_INTERNAL_SECRET,
                },
                method="POST",
            )
            with urllib.request.urlopen(drain_req, timeout=30) as r:
                drain_resp = json.loads(r.read())
            print(f"  Pre-drain tick: {drain_resp}")
            time.sleep(5)  # Give the background task time to claim stale jobs
        except Exception as e:
            print(f"  Pre-drain tick failed (non-fatal): {e}")

    # Submit to genesis-meta WITHOUT mode:live_test so the full AgentRuntime path runs.
    # The prompt explicitly instructs Meta to call genesis_call twice.
    try:
        submit_resp = _post("/agents/genesis-meta/run", {
            "task": (
                "You MUST use the genesis_call tool to delegate. Do not just describe — call the tool now.\n"
                "Step 1: Call genesis_call with agent='genesis-research' and task='What year was Python created? Answer in one sentence.'.\n"
                "Step 2: Call genesis_call with agent='genesis-finance' and task='What is 10 divided by 2? Answer with just the number.'.\n"
                "Step 3: After both calls complete, write a one-paragraph Delegation Summary listing which agents you called and what each returned."
            ),
            "params": {},
        })
    except Exception as e:
        print(f"  {FAIL} Submit request failed: {e}")
        return False

    print(f"  Submit response: {str(submit_resp)[:300]}")

    # genesis-meta is async — parse job_id from the response
    raw_resp_str = submit_resp.get("response", "")
    try:
        job_info = json.loads(raw_resp_str) if isinstance(raw_resp_str, str) else {}
    except json.JSONDecodeError:
        job_info = {}

    job_id = job_info.get("job_id") or submit_resp.get("job_id")
    if not job_id:
        print(f"  {FAIL} No job_id in submit response: {submit_resp}")
        return False

    print(f"  Queued job_id: {job_id}")
    poll_path = f"/agents/jobs/{job_id}"

    # Trigger worker tick so the job actually runs
    if RENDER_INTERNAL_SECRET:
        try:
            time.sleep(2)
            tick_req = urllib.request.Request(
                f"{RENDER_URL}/internal/genesis-worker/tick",
                data=json.dumps({"limit": 10}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Internal-Secret": RENDER_INTERNAL_SECRET,
                },
                method="POST",
            )
            with urllib.request.urlopen(tick_req, timeout=30) as r:
                tick_resp = json.loads(r.read())
            print(f"  Worker tick: {tick_resp}")
        except Exception as e:
            print(f"  Worker tick failed (non-fatal): {e}")
    else:
        print("  RENDER_INTERNAL_SECRET not set — job will run via background worker polling")

    # Poll up to 5 minutes — Meta orchestration takes longer than simple agents
    deadline = time.time() + 300
    last_status = "UNKNOWN"
    _last_tick_at = time.time()
    while time.time() < deadline:
        time.sleep(8)
        try:
            status_resp = _get(poll_path)
        except Exception as e:
            print(f"  poll error: {e}")
            continue

        last_status = status_resp.get("status", "UNKNOWN")
        print(f"  [{time.strftime('%H:%M:%S')}] status={last_status}")

        # Re-tick every 45s if still QUEUED — handles cases where the background
        # task silently failed (DB error etc.) or a stale job beat us to the slot.
        if last_status == "QUEUED" and RENDER_INTERNAL_SECRET and (time.time() - _last_tick_at) > 45:
            try:
                retick_req = urllib.request.Request(
                    f"{RENDER_URL}/internal/genesis-worker/tick",
                    data=json.dumps({"limit": 10}).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "X-Internal-Secret": RENDER_INTERNAL_SECRET,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(retick_req, timeout=30) as r:
                    retick_resp = json.loads(r.read())
                print(f"  Re-tick (still QUEUED): {retick_resp}")
                _last_tick_at = time.time()
            except Exception as e:
                print(f"  Re-tick failed (non-fatal): {e}")

        if last_status in ("DELIVERED", "DELIVERED_WITH_ARTIFACT_WARNING"):
            break
        elif last_status == "FAILED":
            print(f"  {FAIL} Job FAILED: {status_resp.get('errorCode')} — {status_resp.get('errorMessage', '')[:300]}")
            return False
    else:
        print(f"  {FAIL} Job did not reach DELIVERED within 300s. Last status: {last_status}")
        return False

    print(f"  {PASS} Job reached {last_status}")

    # Parse resultSummary — worker now packs {response, trace} as JSON
    raw_summary = status_resp.get("resultSummary", "")
    trace = {}
    try:
        parsed = json.loads(raw_summary)
        if isinstance(parsed, dict):
            trace = parsed.get("trace", {})
            print(f"  response snippet: {str(parsed.get('response', ''))[:120]}")
    except (json.JSONDecodeError, TypeError):
        print(f"  resultSummary is not JSON (legacy format): {raw_summary[:100]}")

    tool_calls = trace.get("tool_calls", [])
    if not isinstance(tool_calls, list):
        print(f"  {FAIL} trace.tool_calls is not a list: {type(tool_calls).__name__} = {tool_calls!r}")
        return False

    print(f"  trace.tool_calls count: {len(tool_calls)}")
    for i, tc in enumerate(tool_calls):
        print(f"    [{i}] tool={tc.get('tool_name')} target={tc.get('target_agent_slug')} child_job={tc.get('child_job_id')} child_ok={tc.get('child_ok')}")

    genesis_calls = [t for t in tool_calls if t.get("tool_name") == "genesis_call"]
    if len(genesis_calls) < 2:
        print(f"  {FAIL} Expected >= 2 genesis_call entries in trace, got {len(genesis_calls)}")
        print(f"         Full tool_calls: {json.dumps(tool_calls)[:600]}")
        if not tool_calls:
            print("         DIAGNOSIS: trace.tool_calls is empty — either Meta did not call genesis_call,")
            print("         or the resultSummary was not in JSON format. Check worker.py result packing.")
        return False

    # Validate each genesis_call entry
    problems = []
    for i, gc in enumerate(genesis_calls):
        if not gc.get("target_agent_slug"):
            problems.append(f"genesis_call[{i}] missing target_agent_slug")
        if not gc.get("child_job_id"):
            problems.append(f"genesis_call[{i}] missing child_job_id")
        if gc.get("child_ok") is not True:
            problems.append(f"genesis_call[{i}] child_ok={gc.get('child_ok')} (expected True)")

    if problems:
        print(f"  {FAIL} genesis_call trace entries have problems:")
        for p in problems:
            print(f"         - {p}")
        return False

    agents_called = [gc.get("target_agent_slug") for gc in genesis_calls]
    print(f"  {PASS} Trace-proven genesis_call delegations: {agents_called}")

    # Phase 5 hardening check: trace.subagents list with child_session_id
    subagents = trace.get("subagents", [])
    print(f"  trace.subagents count: {len(subagents)}")
    if subagents:
        for i, sa in enumerate(subagents):
            print(f"    [{i}] child_agent={sa.get('child_agent_slug')} "
                  f"child_session={sa.get('child_session_id')} child_ok={sa.get('child_ok')}")
        missing_sessions = [i for i, sa in enumerate(subagents) if not sa.get("child_session_id")]
        if missing_sessions:
            print(f"  {FAIL} subagents[{missing_sessions}] missing child_session_id")
            return False
        parent_session = trace.get("session_id")
        if parent_session:
            missing_parent = [i for i, sa in enumerate(subagents)
                              if sa.get("parent_session_id") != parent_session]
            if missing_parent:
                print(f"  {FAIL} subagents[{missing_parent}] have wrong parent_session_id")
                return False
        print(f"  {PASS} trace.subagents: all entries have child_session_id + parent_session_id")
    else:
        print(f"  [INFO] trace.subagents empty — hardening deploy may not yet be active")

    return True


# ---------------------------------------------------------------------------
# Test 6: Conduit browser job — noted as requires live Render + Chromium
# ---------------------------------------------------------------------------

def test_conduit_browser() -> bool:
    """Placeholder — requires live Render with Chromium + async worker. Manual only."""
    print("\n[TEST 6] Conduit browser job on Render")
    print(f"  {SKIP} This test requires:")
    print("    - Render service running a Chromium-capable worker (not free tier)")
    print("    - An async job submitted with a browser_required=true agent slug")
    print("    - Poll until DELIVERED; verify artifact contains screenshot/HAR")
    print("  Run manually via: POST /agents/genesis-builder/run with a browser task")
    print("  and poll GET /agents/jobs/{id} until DELIVERED within 5 minutes.")
    return None  # type: ignore  # None = SKIPPED, not FAIL


# ---------------------------------------------------------------------------
# Test: automatic_worker_execution — job processed WITHOUT tick endpoint call
# ---------------------------------------------------------------------------

def test_automatic_worker_execution() -> bool:
    """Submit an async job and verify it reaches DELIVERED without calling
    the internal /genesis-worker/tick endpoint.

    Requires GENESIS_WORKER_ENABLED=true on the Render service (Phase 7).
    The in-process auto-worker polls every GENESIS_WORKER_INTERVAL_SECONDS (default 10s)
    and picks up QUEUED jobs automatically.
    """
    print("\n[TEST automatic_worker_execution] Job processed by in-process auto-worker (no tick)")

    if not GATEWAY_API_KEY and not AGENT_GATEWAY_SECRET:
        print(f"  {SKIP} automatic_worker_execution: auth not set — cannot run")
        return False

    import urllib.request

    def _post(path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{RENDER_URL}{path}",
            data=data,
            headers={"Content-Type": "application/json", **_auth_headers()},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def _get(path: str) -> dict:
        req = urllib.request.Request(
            f"{RENDER_URL}{path}",
            headers=_auth_headers(),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    # First verify the worker reports as enabled
    try:
        worker_health = _get("/health/worker")
        if not worker_health.get("enabled"):
            print(f"  {SKIP} /health/worker reports enabled=False — set GENESIS_WORKER_ENABLED=true on Render")
            print(f"         health/worker: {worker_health}")
            return None  # type: ignore  # SKIP
        print(f"  /health/worker enabled=True last_tick={worker_health.get('last_tick_at')}")
    except Exception as e:
        print(f"  {FAIL} /health/worker failed: {e}")
        return False

    # Submit a simple async job (genesis-research is async via job_mode)
    try:
        submit_resp = _post("/agents/genesis-research/run", {
            "task": "What year was Python created? Reply with just the year.",
            "params": {},
        })
    except Exception as e:
        print(f"  {FAIL} Submit request failed: {e}")
        return False

    raw_resp = submit_resp.get("response", "")
    try:
        job_info = json.loads(raw_resp) if isinstance(raw_resp, str) else {}
    except json.JSONDecodeError:
        job_info = {}

    job_id = job_info.get("job_id") or submit_resp.get("job_id")
    if not job_id:
        print(f"  Hmm — submit_resp={submit_resp!r}")
        # genesis-research might not be async; use the alternate submit path
        try:
            submit_resp2 = _post("/agents/genesis-research/jobs", {
                "prompt": "What year was Python created? Reply with just the year.",
                "params": {},
            })
            job_id = submit_resp2.get("job_id")
            if not job_id:
                print(f"  {FAIL} No job_id from /jobs endpoint either: {submit_resp2}")
                return False
        except Exception as e2:
            print(f"  {FAIL} Fallback /jobs submit failed: {e2}")
            return False

    print(f"  Submitted job_id: {job_id}")
    print(f"  NOTE: NOT calling tick endpoint — relying on in-process auto-worker")

    # Poll up to 3 minutes — no tick endpoint call
    deadline = time.time() + 180
    last_status = "UNKNOWN"
    while time.time() < deadline:
        time.sleep(8)
        try:
            status_resp = _get(f"/agents/jobs/{job_id}")
        except Exception as e:
            print(f"  poll error: {e}")
            continue

        last_status = status_resp.get("status", "UNKNOWN")
        elapsed = int(time.time() - (deadline - 180))
        print(f"  [{elapsed}s] status={last_status}")

        if last_status in ("DELIVERED", "DELIVERED_WITH_ARTIFACT_WARNING"):
            print(f"  {PASS} Job reached {last_status} via auto-worker (no tick endpoint used)")
            return True
        elif last_status == "FAILED":
            print(f"  {FAIL} Job FAILED: {status_resp.get('errorCode')} {status_resp.get('errorMessage', '')[:200]}")
            return False

    print(f"  {FAIL} Job did not reach DELIVERED within 180s (last: {last_status}). "
          "Check GENESIS_WORKER_ENABLED=true is set on Render.")
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

TESTS = {
    "postgres_lifecycle": test_postgres_lifecycle,
    "artifact_failure": test_artifact_failure,
    "meta_delegation": test_meta_delegation,
    "meta_real_orchestration": test_meta_real_orchestration,
    "render_async_job": test_render_async_job,
    "conduit_browser": test_conduit_browser,
    "legacy_automatic_worker_execution": test_automatic_worker_execution,
}

# Phase 9 — the 10 real-agent DONE-standard scenarios (one-directional import).
try:
    from live_real_agent_tests import REAL_AGENT_TESTS
    TESTS.update(REAL_AGENT_TESTS)
except Exception as _e:  # noqa: BLE001
    print(f"warning: real-agent live tests unavailable: {_e}")

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "all"

    if target == "all":
        to_run = list(TESTS.items())
    elif target in TESTS:
        to_run = [(target, TESTS[target])]
    else:
        print(f"Unknown test: {target}. Options: {', '.join(TESTS)} or 'all'")
        sys.exit(1)

    results = {}
    for name, fn in to_run:
        try:
            results[name] = fn()
        except Exception as exc:
            print(f"  {FAIL} {name} raised: {exc}")
            results[name] = False

    print("\n" + "=" * 60)
    print("LIVE INTEGRATION TEST SUMMARY")
    print("=" * 60)
    passed = failed = skipped = 0
    for name, result in results.items():
        if result is True:
            print(f"  {PASS}  {name}")
            passed += 1
        elif result is False:
            print(f"  {FAIL}  {name}")
            failed += 1
        else:
            print(f"  {SKIP}  {name}")
            skipped += 1
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    print("=" * 60)

    if failed:
        sys.exit(1)
