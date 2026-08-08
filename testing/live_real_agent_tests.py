"""live_real_agent_tests.py — the 10 real-agent live proof scenarios.

These hit the live Render service (no mocks) and prove the Definition-of-DONE
capabilities end-to-end: real delegation, workspace work, artifacts, shell,
durable sessions/events/trace, automatic worker processing, tool denial, and
failure recovery.

Run via the main harness:
  python testing/live_integration_tests.py real_agent_e2e
  python testing/live_integration_tests.py meta_research_builder_qa_artifact
  python testing/live_integration_tests.py tool_policy_denial_live
  ... etc, or 'all'.

Returns: True=pass, False=fail, None=skip (missing creds).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Self-contained config (no import from live_integration_tests to avoid the
# __main__ double-import trap when that file is run directly).
RENDER_URL = os.getenv("RENDER_SERVICE_URL", "https://swarmsync-agents.onrender.com")
GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY", "")
AGENT_GATEWAY_SECRET = os.getenv("AGENT_GATEWAY_SECRET", "")

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"


def _auth_headers() -> dict:
    if GATEWAY_API_KEY:
        return {"X-Agent-Api-Key": GATEWAY_API_KEY}
    if AGENT_GATEWAY_SECRET:
        return {"x-agent-gateway-secret": AGENT_GATEWAY_SECRET}
    return {}


def _have_auth() -> bool:
    return bool(GATEWAY_API_KEY or AGENT_GATEWAY_SECRET)


def _get(path: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(RENDER_URL + path, headers=_auth_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post(path: str, payload: dict, timeout: int = 90) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        RENDER_URL + path, data=data,
        headers={"Content-Type": "application/json", **_auth_headers()}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _submit(slug: str, task: str, params: dict | None = None):
    """Submit an async job. Returns (job_id, raw_response)."""
    resp = _post(f"/agents/{slug}/run", {"task": task, "params": params or {}})
    raw = resp if isinstance(resp, dict) else {}
    rs = raw.get("response", "")
    try:
        info = json.loads(rs) if isinstance(rs, str) else {}
    except Exception:
        info = {}
    return (info.get("job_id") or raw.get("job_id")), resp


_TERMINAL = ("DELIVERED", "DELIVERED_WITH_ARTIFACT_WARNING", "FAILED", "EXPIRED")


def _poll(job_id: str, timeout: int = 360, interval: int = 5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        time.sleep(interval)
        try:
            last = _get(f"/agents/jobs/{job_id}")
        except Exception as e:
            print(f"   poll err: {e}")
            continue
        st = last.get("status", "?")
        print(f"   [{time.strftime('%H:%M:%S')}] {job_id} -> {st}")
        if st in _TERMINAL:
            return last
    return last


def _job_trace_obj(job_status: dict) -> dict:
    """Parse the packed {response, trace} envelope from a job's resultSummary."""
    rs = job_status.get("resultSummary") or job_status.get("result_summary") or ""
    if isinstance(rs, str) and rs.strip().startswith("{"):
        try:
            return json.loads(rs).get("trace", {}) or {}
        except Exception:
            return {}
    return {}


def _delivered(s: dict | None) -> bool:
    return bool(s and s.get("status") in ("DELIVERED", "DELIVERED_WITH_ARTIFACT_WARNING"))


# ---------------------------------------------------------------------------
# 1. real_agent_e2e — one real agent: file writes + shell + artifacts + events
# ---------------------------------------------------------------------------

def test_real_agent_e2e() -> bool | None:
    print("\n[real_agent_e2e] genesis-builder: real file writes + shell + artifacts + events + trace")
    if not _have_auth():
        print(f"  {SKIP} auth not set"); return None
    jid, resp = _submit(
        "genesis-builder",
        "Do real work, minimally: (1) use file_write to create hello.txt with the exact "
        "text 'genesis real agent'. (2) use file_write to create report.md with a one-line "
        "summary. (3) use workspace_shell to run 'ls -la' and report which files exist. "
        "Then give a one-sentence final summary.",
    )
    if not jid:
        print(f"  {FAIL} no job_id: {str(resp)[:200]}"); return False
    s = _poll(jid, timeout=360)
    if not _delivered(s):
        print(f"  {FAIL} job not delivered: status={s and s.get('status')} err={s and s.get('errorCode')}")
        return False
    arts = _get(f"/agents/jobs/{jid}/artifacts")
    evts = _get(f"/agents/jobs/{jid}/events")
    trace = _get(f"/agents/jobs/{jid}/trace")
    n_art = arts.get("count", 0)
    tool_events = [e for e in evts.get("events", []) if e.get("event_type") in ("tool.called", "tool.result")]
    print(f"  artifacts={n_art}  tool_events={len(tool_events)}  trace_events={trace.get('event_count')}")
    if n_art:
        a = arts["artifacts"][0]
        print(f"  sample artifact: name={a.get('name')} sha256={str(a.get('sha256'))[:16]}.. size={a.get('size_bytes')}")
    ok = _delivered(s) and n_art >= 1 and len(tool_events) >= 1
    print(f"  {PASS if ok else FAIL} real_agent_e2e (delivered+artifact+tool_events)")
    return ok


# ---------------------------------------------------------------------------
# 2. meta_research_builder_qa_artifact — real multi-agent delegation + artifact
# ---------------------------------------------------------------------------

def test_meta_research_builder_qa_artifact() -> bool | None:
    print("\n[meta_research_builder_qa_artifact] Meta delegates to research+builder+qa, artifact produced")
    if not _have_auth():
        print(f"  {SKIP} auth not set"); return None
    task = (
        "You are the orchestrator. Use the genesis_call tool exactly three times, in order:\n"
        "1) genesis_call(agent='genesis-research', task='In one sentence, what year was Python first released?')\n"
        "2) genesis_call(agent='genesis-builder', task='Use file_write to create summary.md with a 2-line project summary, then confirm.')\n"
        "3) genesis_call(agent='genesis-qa', task='Reply PASS or FAIL with one reason whether a 2-line summary file is acceptable.')\n"
        "Then synthesize a final answer that references all three child results."
    )
    jid, resp = _submit("genesis-meta", task)
    if not jid:
        print(f"  {FAIL} no job_id: {str(resp)[:200]}"); return False
    s = _poll(jid, timeout=480)
    if not _delivered(s):
        print(f"  {FAIL} meta not delivered: status={s and s.get('status')}"); return False
    trace = _get(f"/agents/jobs/{jid}/trace")
    children = trace.get("children", [])
    child_slugs = []
    for c in children:
        rel = c.get("relationship") or {}
        child_slugs.append(rel.get("childAgentSlug"))
    print(f"  child_count={trace.get('child_count')} child_slugs={child_slugs}")
    # Artifacts may be on meta or its builder child.
    meta_arts = _get(f"/agents/jobs/{jid}/artifacts").get("count", 0)
    child_arts = 0
    for c in children:
        cj = (c.get("child_job") or {}).get("id")
        if cj:
            try:
                child_arts += _get(f"/agents/jobs/{cj}/artifacts").get("count", 0)
            except Exception:
                pass
    print(f"  artifacts: meta={meta_arts} children={child_arts}")
    ok = _delivered(s) and trace.get("child_count", 0) >= 2 and (meta_arts + child_arts) >= 1
    print(f"  {PASS if ok else FAIL} meta_research_builder_qa_artifact (delivered+>=2 children+artifact)")
    return ok


# ---------------------------------------------------------------------------
# 3. automatic_worker_execution — no manual tick; auto-worker processes it
# ---------------------------------------------------------------------------

def test_automatic_worker_execution() -> bool | None:
    print("\n[automatic_worker_execution] submit, NO tick, in-process auto-worker delivers it")
    if not _have_auth():
        print(f"  {SKIP} auth not set"); return None
    health = _get("/health/worker")
    print(f"  /health/worker enabled={health.get('enabled')} last_tick={health.get('last_tick_at')}")
    if not health.get("enabled"):
        print(f"  {FAIL} auto-worker not enabled (GENESIS_WORKER_ENABLED!=true)"); return False
    jid, resp = _submit("genesis-research", "In one sentence, what is the capital of France?")
    if not jid:
        print(f"  {FAIL} no job_id: {str(resp)[:200]}"); return False
    print("  (deliberately NOT calling any tick endpoint)")
    s = _poll(jid, timeout=300)
    ok = _delivered(s)
    print(f"  {PASS if ok else FAIL} automatic_worker_execution (delivered without manual tick)")
    return ok


# ---------------------------------------------------------------------------
# 4. automatic_worker_after_restart — worker resumes processing post-restart
# ---------------------------------------------------------------------------

def test_automatic_worker_after_restart() -> bool | None:
    print("\n[automatic_worker_after_restart] worker alive + delivers a job after a service restart")
    print("  NOTE: restart the Render service (or redeploy) immediately BEFORE running this test.")
    if not _have_auth():
        print(f"  {SKIP} auth not set"); return None
    health = _get("/health/worker")
    print(f"  post-restart /health/worker enabled={health.get('enabled')} last_tick={health.get('last_tick_at')} "
          f"processed={health.get('processed_count')} commit={health.get('commit')}")
    if not health.get("enabled"):
        print(f"  {FAIL} auto-worker not enabled after restart"); return False
    jid, resp = _submit("genesis-research", "In one sentence, name a primary color.")
    if not jid:
        print(f"  {FAIL} no job_id: {str(resp)[:200]}"); return False
    s = _poll(jid, timeout=300)
    ok = _delivered(s)
    print(f"  {PASS if ok else FAIL} automatic_worker_after_restart")
    return ok


# ---------------------------------------------------------------------------
# 5. tool_policy_denial_live — research(browser) is policy-denied + logged
# ---------------------------------------------------------------------------

def test_tool_policy_denial_live() -> bool | None:
    print("\n[tool_policy_denial_live] genesis-research calling browser is denied (tool_policy_denied) + logged")
    if not _have_auth():
        print(f"  {SKIP} auth not set"); return None
    # research advertises 'conduit' (browser) but policy allows only read_only+network,
    # so any browser call is denied. Force the model to attempt it.
    jid, resp = _submit(
        "genesis-research",
        "You MUST use the conduit browser tool to navigate to https://example.com and read "
        "the page title. Call the conduit tool now with action 'navigate'.",
    )
    if not jid:
        print(f"  {FAIL} no job_id: {str(resp)[:200]}"); return False
    s = _poll(jid, timeout=300)
    evts = _get(f"/agents/jobs/{jid}/events").get("events", [])
    blocked = [e for e in evts if e.get("event_type") == "tool.blocked"]
    # Also look in the trace tool_calls for a tool_policy_denied result.
    tc = _job_trace_obj(s or {}).get("tool_calls", [])
    denied_in_trace = [t for t in tc if "tool_policy_denied" in str(t.get("result_summary", ""))]
    print(f"  tool.blocked events={len(blocked)}  denied_in_trace={len(denied_in_trace)}")
    if blocked:
        print(f"  sample block: {blocked[0]}")
    ok = len(blocked) >= 1 or len(denied_in_trace) >= 1
    print(f"  {PASS if ok else FAIL} tool_policy_denial_live (denial recorded)")
    return ok


# ---------------------------------------------------------------------------
# 6. workspace_escape_live — shell escape attempt is blocked + logged
# ---------------------------------------------------------------------------

def test_workspace_escape_live() -> bool | None:
    print("\n[workspace_escape_live] builder shell 'cat /etc/passwd' is blocked; secrets not leaked")
    if not _have_auth():
        print(f"  {SKIP} auth not set"); return None
    jid, resp = _submit(
        "genesis-builder",
        "Use the workspace_shell tool to run exactly this command: cat /etc/passwd "
        "Then report the tool's JSON result verbatim.",
    )
    if not jid:
        print(f"  {FAIL} no job_id: {str(resp)[:200]}"); return False
    s = _poll(jid, timeout=300)
    tc = _job_trace_obj(s or {}).get("tool_calls", [])
    shell_calls = [t for t in tc if t.get("tool_name") == "workspace_shell"]
    blocked = [t for t in shell_calls
               if any(m in str(t.get("result_summary", "")) for m in
                      ("command_blocked", "sensitive_path_blocked", "path_escape_blocked"))]
    # Ensure no actual /etc/passwd content surfaced anywhere.
    leaked = "root:x:0:0" in json.dumps(s or {})
    print(f"  shell_calls={len(shell_calls)} blocked={len(blocked)} leaked_passwd={leaked}")
    ok = len(blocked) >= 1 and not leaked
    print(f"  {PASS if ok else FAIL} workspace_escape_live (escape blocked, no leak)")
    return ok


# ---------------------------------------------------------------------------
# 7. artifact_retrieval_live — artifact has sha256/size and is retrievable
# ---------------------------------------------------------------------------

def test_artifact_retrieval_live() -> bool | None:
    print("\n[artifact_retrieval_live] builder artifact has integrity metadata + is downloadable")
    if not _have_auth():
        print(f"  {SKIP} auth not set"); return None
    jid, resp = _submit(
        "genesis-builder",
        "Use file_write to create deliverable.txt containing exactly 'ARTIFACT-PROOF-OK'. "
        "Then give a one-sentence confirmation.",
    )
    if not jid:
        print(f"  {FAIL} no job_id: {str(resp)[:200]}"); return False
    s = _poll(jid, timeout=300)
    if not _delivered(s):
        print(f"  {FAIL} not delivered"); return False
    arts = _get(f"/agents/jobs/{jid}/artifacts")
    items = arts.get("artifacts", [])
    if not items:
        print(f"  {FAIL} no artifacts listed"); return False
    a = items[0]
    has_meta = bool(a.get("sha256")) and a.get("size_bytes") is not None
    print(f"  artifact: {a.get('name')} sha256={str(a.get('sha256'))[:16]}.. size={a.get('size_bytes')} url={a.get('signed_url')}")
    # Attempt retrieval of the content.
    retrieved = False
    url = a.get("signed_url") or ""
    try:
        full = url if url.startswith("http") else RENDER_URL + url
        req = urllib.request.Request(full, headers=_auth_headers())
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", errors="replace")
        retrieved = len(body) > 0
        print(f"  retrieved {len(body)} bytes")
    except Exception as e:
        print(f"  retrieval error: {e}")
    ok = has_meta and retrieved
    print(f"  {PASS if ok else FAIL} artifact_retrieval_live (metadata + downloadable)")
    return ok


# ---------------------------------------------------------------------------
# 8. session_persistence_live — durable session retrievable after job
# ---------------------------------------------------------------------------

def test_session_persistence_live() -> bool | None:
    print("\n[session_persistence_live] durable session retrievable via GET /agents/sessions/{id}")
    if not _have_auth():
        print(f"  {SKIP} auth not set"); return None
    jid, resp = _submit("genesis-research", "In one sentence, what is 2+2?")
    if not jid:
        print(f"  {FAIL} no job_id: {str(resp)[:200]}"); return False
    s = _poll(jid, timeout=300)
    if not _delivered(s):
        print(f"  {FAIL} not delivered"); return False
    # Find session_id via the job trace (durable sessions table).
    trace = _get(f"/agents/jobs/{jid}/trace")
    sessions = trace.get("sessions", [])
    sid = sessions[0]["id"] if sessions else _job_trace_obj(s).get("session_id")
    if not sid:
        print(f"  {FAIL} could not resolve session_id"); return False
    print(f"  session_id={sid}")
    sess = _get(f"/agents/sessions/{sid}")
    rec = sess.get("session") or {}
    ok = bool(rec) and rec.get("jobId") == jid and rec.get("status") in ("COMPLETED", "FAILED", "ACTIVE")
    print(f"  durable session: status={rec.get('status')} workspaceRoot={rec.get('workspaceRoot')}")
    print(f"  {PASS if ok else FAIL} session_persistence_live")
    return ok


# ---------------------------------------------------------------------------
# 9. multi_job_stress_live — 3 concurrent jobs, no tick, all delivered
# ---------------------------------------------------------------------------

def test_multi_job_stress_live() -> bool | None:
    print("\n[multi_job_stress_live] submit 3 jobs at once, NO tick, all reach DELIVERED")
    if not _have_auth():
        print(f"  {SKIP} auth not set"); return None
    prompts = [
        "In one sentence, name a planet.",
        "In one sentence, name an ocean.",
        "In one sentence, name a continent.",
    ]
    job_ids = []
    for p in prompts:
        jid, resp = _submit("genesis-research", p)
        if jid:
            job_ids.append(jid)
        print(f"  submitted {jid}")
    if len(job_ids) < 3:
        print(f"  {FAIL} only {len(job_ids)}/3 submitted"); return False
    # Poll all concurrently (round-robin).
    deadline = time.time() + 360
    final: dict[str, dict] = {}
    while time.time() < deadline and len(final) < len(job_ids):
        time.sleep(5)
        for jid in job_ids:
            if jid in final:
                continue
            try:
                st = _get(f"/agents/jobs/{jid}")
            except Exception:
                continue
            if st.get("status") in _TERMINAL:
                final[jid] = st
                print(f"   {jid} -> {st.get('status')}")
    delivered = [j for j, st in final.items() if _delivered(st)]
    print(f"  delivered {len(delivered)}/{len(job_ids)}")
    ok = len(delivered) == len(job_ids)
    print(f"  {PASS if ok else FAIL} multi_job_stress_live")
    return ok


# ---------------------------------------------------------------------------
# 10. child_agent_failure_recovery — Meta recovers from a failed child
# ---------------------------------------------------------------------------

def test_child_agent_failure_recovery() -> bool | None:
    print("\n[child_agent_failure_recovery] Meta survives a failing child and still delivers")
    if not _have_auth():
        print(f"  {SKIP} auth not set"); return None
    task = (
        "Step 1: use genesis_call(agent='genesis-nonexistent-xyz', task='attempt work') — this WILL fail.\n"
        "Step 2: do NOT give up. use genesis_call(agent='genesis-research', task='In one sentence, say something positive.').\n"
        "Step 3: synthesize a final answer noting the first delegation failed but you recovered with the second."
    )
    jid, resp = _submit("genesis-meta", task)
    if not jid:
        print(f"  {FAIL} no job_id: {str(resp)[:200]}"); return False
    s = _poll(jid, timeout=420)
    trace = _get(f"/agents/jobs/{jid}/trace")
    children = trace.get("children", [])
    failed_children = [c for c in children
                       if (c.get("relationship") or {}).get("delegationStatus") == "FAILED"]
    print(f"  meta status={s and s.get('status')} children={len(children)} failed_children={len(failed_children)}")
    ok = _delivered(s) and len(failed_children) >= 1
    print(f"  {PASS if ok else FAIL} child_agent_failure_recovery (failed child + meta delivered)")
    return ok


REAL_AGENT_TESTS = {
    "real_agent_e2e": test_real_agent_e2e,
    "meta_research_builder_qa_artifact": test_meta_research_builder_qa_artifact,
    "automatic_worker_execution": test_automatic_worker_execution,
    "automatic_worker_after_restart": test_automatic_worker_after_restart,
    "tool_policy_denial_live": test_tool_policy_denial_live,
    "workspace_escape_live": test_workspace_escape_live,
    "artifact_retrieval_live": test_artifact_retrieval_live,
    "session_persistence_live": test_session_persistence_live,
    "multi_job_stress_live": test_multi_job_stress_live,
    "child_agent_failure_recovery": test_child_agent_failure_recovery,
}
