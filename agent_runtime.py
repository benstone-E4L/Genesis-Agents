"""Genesis agent runtime - multi-turn LLM loop with tool dispatch, per-slug parameterized."""
from __future__ import annotations
import json
import hmac
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from bundle_loader import load_bundle
from tools import get_tool, tool_schemas_for, register_default_tools

# Phase 2-8 hardening modules — imported tolerantly so the runtime boots in
# stripped environments (tests without full deps, etc.)
try:
    from runtime.workspace_manager import create_workspace, set_workspace_status, get_workspace
    _WS_MANAGER_OK = True
except Exception:
    _WS_MANAGER_OK = False

try:
    from runtime.observability import (
        emit_event,
        EVT_JOB_CREATED, EVT_AGENT_STARTED, EVT_LLM_REQUESTED, EVT_LLM_RESPONDED,
        EVT_TOOL_CALLED, EVT_TOOL_BLOCKED, EVT_TOOL_RESULT,
        EVT_SUBAGENT_DISPATCHED, EVT_SUBAGENT_RETURNED,
        EVT_JOB_COMPLETED, EVT_JOB_FAILED, EVT_SANDBOX_STATUS,
    )
    _OBS_OK = True
except Exception:
    _OBS_OK = False
    def emit_event(job_id: str, event_type: str, data: Any = None) -> None:  # type: ignore[misc]
        pass

try:
    from runtime import phoenix_tracing as _phoenix
    _PHOENIX_OK = True
except Exception:  # pragma: no cover - the module is stdlib-only at import time
    _PHOENIX_OK = False

    class _phoenix:  # type: ignore[no-redef]
        """Null tracer. Observability is never allowed to break the runtime."""

        SPAN_KIND = INPUT_VALUE = OUTPUT_VALUE = "noop"
        LLM_MODEL_NAME = LLM_PROVIDER = "noop"
        LLM_TOKEN_PROMPT = LLM_TOKEN_COMPLETION = LLM_TOKEN_TOTAL = "noop"
        TOOL_NAME = "noop"

        @staticmethod
        def span(*_a: Any, **_k: Any) -> Any:
            from contextlib import nullcontext
            return nullcontext(None)

        @staticmethod
        def set_attributes(*_a: Any, **_k: Any) -> None:
            pass

        @staticmethod
        def record_error(*_a: Any, **_k: Any) -> None:
            pass

        @staticmethod
        def safe_content(*_a: Any, **_k: Any) -> None:
            return None

        @staticmethod
        def emit_completed_span(*_a: Any, **_k: Any) -> Any:
            return None

        @staticmethod
        def get_tracer() -> Any:
            return None

        @staticmethod
        def current_trace_id() -> Any:
            return None

try:
    from runtime.tool_policy import check_tool_policy
    _POLICY_OK = True
except Exception:
    _POLICY_OK = False
    def check_tool_policy(agent_slug: str, tool_name: str) -> dict:  # type: ignore[misc]
        return {"ok": True, "tool_name": tool_name, "agent_slug": agent_slug}

# PERMANENTLY_PROHIBITED enforcement (FINANCE-TOOL-CONTRACTS.md Section 6.2
# Layer 4). Deliberately NOT a tolerant import: the tolerant import above falls
# back to allowing everything, and a prohibition that can be disabled by an
# ImportError is not a prohibition. runtime.tool_policy is pure standard
# library, so this import cannot fail anywhere agent_runtime itself imports.
from runtime.tool_policy import (  # noqa: E402
    RISK_DEPLOYMENT,
    RISK_READ_ONLY,
    assert_prohibitions_intact,
    is_prohibited,
    prohibition_group,
)
from tools._envelope import prohibited_refusal  # noqa: E402
from runtime.genesis_audit import append_tool_event, append_tool_intent

# Phase 3/6: durable session + relationship store (Postgres-backed, best-effort).
try:
    import durable_store
    _DURABLE_OK = True
except Exception:
    _DURABLE_OK = False
    durable_store = None  # type: ignore

log = logging.getLogger(__name__)

# Resource limits — Phase 11 sandbox enforcement.
MAX_TURNS = 10
MAX_LLM_CALLS = 10  # Same as MAX_TURNS but tracked explicitly for clarity.
MAX_TOKENS_PER_JOB = 50_000  # Aggregate over all turns (response.usage.total_tokens).
MAX_FILES_WRITTEN = 20  # Hard cap on file_write tool successes per job.
DEFAULT_TIMEOUT_S = 300  # 5 minutes wall-time.
DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024  # 4 MB per tool result.
DEFAULT_SWARMSYNC_MODEL = "auto"
OPENROUTER_HOST_MARKERS = ("openrouter.ai",)


def _llm_client_session(timeout: "Any") -> "Any":
    """Build the ClientSession every LLM call uses.

    Uses ThreadedResolver (the OS resolver via a thread pool) rather than
    aiohttp's default async resolver: on Windows the default resolver returns
    "Could not contact DNS servers" for hosts the OS resolves fine, which
    surfaced as errorCode=llm_call_failed on every agent run. Cato hit and
    fixed the identical defect — see cato/tools/genesis.py::_ensure_session.
    """
    import aiohttp

    connector = aiohttp.TCPConnector(
        resolver=aiohttp.ThreadedResolver(),
        family=0,  # IPv4 + IPv6 — let the OS pick
        ssl=True,
    )
    return aiohttp.ClientSession(timeout=timeout, connector=connector)


def _check_success_criteria(
    criteria: list[dict] | None,
    result: dict,
    *,
    require_tool_evidence: bool = False,
) -> dict[str, Any]:
    """Validate result against bundle's success_criteria. Returns {ok, failed: [...]}.

    Supported criteria types:
      - non_empty           : response must be non-empty.
      - contains_keys       : response (JSON-parseable) must contain `keys`.
      - max_latency_s       : result.elapsed_s must be <= configured seconds.
      - min_successful_tool_calls: trace must contain at least N successful calls.
    Unknown types are ignored (forward-compatible).
    """
    criteria = list(criteria or [{"type": "non_empty"}])
    if require_tool_evidence and not any(
        c.get("type") == "min_successful_tool_calls" for c in criteria
    ):
        # A bundle may add prose/shape/latency criteria, but it cannot opt out
        # of execution evidence while advertising tools.
        criteria.append({"type": "min_successful_tool_calls", "config": {"count": 1}})
    failed: list[dict[str, Any]] = []
    for c in criteria:
        ct = c.get("type")
        if ct == "non_empty":
            if not result.get("response"):
                failed.append({"type": ct, "reason": "response is empty"})
        elif ct == "contains_keys":
            keys = (c.get("config") or {}).get("keys", [])
            try:
                response_obj = json.loads(result.get("response") or "{}")
                if not isinstance(response_obj, dict):
                    failed.append({"type": ct, "reason": "response not a JSON object"})
                else:
                    for k in keys:
                        if k not in response_obj:
                            failed.append({"type": ct, "reason": f"missing key: {k}"})
            except Exception:
                failed.append({"type": ct, "reason": "response not parseable as JSON"})
        elif ct == "max_latency_s":
            max_s = (c.get("config") or {}).get("seconds", 300)
            elapsed = result.get("elapsed_s", 0) or 0
            if elapsed > max_s:
                failed.append({
                    "type": ct,
                    "reason": f"elapsed {elapsed}s > {max_s}s",
                })
        elif ct == "min_successful_tool_calls":
            minimum = int((c.get("config") or {}).get("count", 1))
            calls = ((result.get("trace") or {}).get("tool_calls") or [])
            successful = sum(1 for call in calls if call.get("ok") is True)
            if successful < minimum:
                failed.append({
                    "type": ct,
                    "reason": f"successful tool calls {successful} < {minimum}",
                })
    return {"ok": len(failed) == 0, "failed": failed}


def _deployment_side_effect_authorized(params: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    """Strip the signed action grant and owner context before model exposure."""
    clean = dict(params or {})
    context = {
        "grant": str(clean.pop("_action_grant", "") or ""),
        "principal_id": str(clean.pop("_request_principal_id", "") or ""),
        "tenant_id": str(clean.pop("_request_tenant_id", "") or ""),
    }
    # Reject and strip the obsolete static credential rather than supporting a
    # compatibility path that recreates bearer-token deployment authority.
    clean.pop("_deployment_approval_token", None)
    return context, clean

# Lazy module init
_DEFAULTS_REGISTERED = False


def _ensure_tools_registered() -> None:
    global _DEFAULTS_REGISTERED
    if not _DEFAULTS_REGISTERED:
        register_default_tools()
        # Layer 2 — fail to boot, not fail to deny. If a prohibited tool has
        # been re-registered, or the frozen manifest no longer matches the
        # prohibition list, this raises and the runtime does not come up.
        assert_prohibitions_intact()
        _DEFAULTS_REGISTERED = True


class AgentRuntime:
    """Runs a single agent invocation."""

    def __init__(self, llm_url: str, llm_key: str):
        self.llm_url = llm_url
        self.llm_key = llm_key
        # Cumulative resources already consumed by DELEGATED children, keyed by the
        # delegating parent's job_id. Without this, a parent that calls genesis_call N times
        # hands each child the same "remaining" budget: the parent's own total_tokens only
        # counts its OWN turns, and conduit_budget_cents was passed as the parent's full
        # configured ceiling rather than what is left. Nine siblings therefore inherited
        # 9x the ceiling and the delegation budget was advisory, not enforced.
        self._delegated_tokens_spent: dict[str, int] = {}
        self._delegated_cents_spent: dict[str, int] = {}
        _ensure_tools_registered()

    def remaining_delegation_budget(
        self, job_id: str, *, token_budget: int, total_tokens: int, cost_budget_cents: int
    ) -> tuple[int, int]:
        """What is genuinely LEFT for the next delegated child of this job.

        Extracted from the tool-dispatch context so the per-tree (not per-child) ceiling is
        directly testable — the bug it fixes is invisible in any single call.
        """
        spent_tokens = self._delegated_tokens_spent.get(job_id, 0)
        spent_cents = self._delegated_cents_spent.get(job_id, 0)
        return (
            max(0, int(token_budget) - int(total_tokens) - spent_tokens),
            max(0, int(cost_budget_cents) - spent_cents),
        )

    def record_delegated_spend(
        self, parent_job_id: str | None, *, tokens: int = 0, cents: int = 0
    ) -> None:
        """Charge a child's resource use back to the delegating parent's remaining budget."""
        if not parent_job_id:
            return
        if tokens:
            self._delegated_tokens_spent[parent_job_id] = (
                self._delegated_tokens_spent.get(parent_job_id, 0) + max(0, int(tokens))
            )
        if cents:
            self._delegated_cents_spent[parent_job_id] = (
                self._delegated_cents_spent.get(parent_job_id, 0) + max(0, int(cents))
            )

    async def execute_agent(
        self,
        slug: str,
        task: str,
        params: dict[str, Any],
        *,
        job_id: Optional[str] = None,
        session_id: Optional[str] = None,
        parent_job_id: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        delegation_chain: tuple[str, ...] = (),
        delegation_depth: int = 0,
        delegated_allowed_risks: frozenset[str] | None = None,
        inherited_token_budget: int | None = None,
        inherited_cost_budget_cents: int | None = None,
    ) -> dict[str, Any]:
        """Traced entry point. Delegates to :meth:`_execute_agent_inner`.

        Wrapping rather than inlining the span keeps the 600-line implementation
        untouched and its many early returns intact — every one of them still
        flows through here, so the span always closes with the real outcome.
        Nested delegation (``genesis_call``) re-enters this method, so child
        agents appear as child spans of their parent automatically.

        The full parameter list is repeated rather than collapsed into
        ``**kwargs`` because the worker contract is asserted by signature
        introspection (``test_runtime_hardening.TestAgentRuntimeSignature``) —
        adding a trace must not change the public shape of this method.
        """
        kwargs: dict[str, Any] = {
            "job_id": job_id,
            "session_id": session_id,
            "parent_job_id": parent_job_id,
            "parent_session_id": parent_session_id,
            "delegation_chain": delegation_chain,
            "delegation_depth": delegation_depth,
            "delegated_allowed_risks": delegated_allowed_risks,
            "inherited_token_budget": inherited_token_budget,
            "inherited_cost_budget_cents": inherited_cost_budget_cents,
        }
        with _phoenix.span(
            f"agent.{slug}",
            kind="AGENT",
            attributes={
                "genesis.agent.slug": slug,
                "genesis.job.id": job_id,
                "genesis.session.id": session_id,
                "genesis.parent_job.id": parent_job_id,
                "genesis.delegation.depth": delegation_depth,
                _phoenix.INPUT_VALUE: _phoenix.safe_content(task),
            },
        ) as sp:
            result = await self._execute_agent_inner(slug, task, params, **kwargs)
            try:
                self._annotate_agent_span(sp, result)
            except Exception:  # pragma: no cover - annotation must never fail a run
                pass
            return result

    @staticmethod
    def _annotate_agent_span(sp: Any, result: dict[str, Any]) -> None:
        """Copy the run's structured outcome onto the Phoenix span."""
        if sp is None or not isinstance(result, dict):
            return
        usage = result.get("resource_usage") or {}
        trace = result.get("trace") or {}
        tool_calls = trace.get("tool_calls") or []
        criteria = result.get("success_criteria_eval") or {}
        _phoenix.set_attributes(sp, {
            "genesis.run.ok": bool(result.get("ok")),
            "genesis.run.error": result.get("error"),
            "genesis.job.id": result.get("job_id"),
            "genesis.turn.count": result.get("turns"),
            "genesis.llm.calls": usage.get("llm_calls"),
            "genesis.files_written": usage.get("files_written"),
            _phoenix.LLM_TOKEN_TOTAL: usage.get("total_tokens"),
            "genesis.tool_calls.count": len(tool_calls),
            "genesis.tool_calls.failed": sum(
                1 for t in tool_calls if isinstance(t, dict) and not t.get("ok")
            ),
            "genesis.tool_calls.names": [
                str(t.get("tool_name")) for t in tool_calls if isinstance(t, dict)
            ],
            # The success-criteria verdict is the thing an operator actually
            # wants to filter on in Phoenix: "show me runs that failed the bar".
            "genesis.success_criteria.ok": criteria.get("ok"),
            "genesis.success_criteria.failed": [str(f) for f in (criteria.get("failed") or [])],
            _phoenix.OUTPUT_VALUE: _phoenix.safe_content(result.get("response")),
        })

    async def _execute_agent_inner(
        self,
        slug: str,
        task: str,
        params: dict[str, Any],
        *,
        job_id: Optional[str] = None,
        session_id: Optional[str] = None,
        parent_job_id: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        delegation_chain: tuple[str, ...] = (),
        delegation_depth: int = 0,
        delegated_allowed_risks: frozenset[str] | None = None,
        inherited_token_budget: int | None = None,
        inherited_cost_budget_cents: int | None = None,
    ) -> dict[str, Any]:
        """Execute one agent invocation. Returns structured result."""
        bundle = load_bundle(slug)
        if bundle is None:
            return {"ok": False, "error": "unknown_slug", "slug": slug}

        job_id = job_id or f"job-{uuid.uuid4().hex[:12]}"
        session_id = session_id or str(uuid.uuid4())

        bundle = dict(bundle)
        from accounting.specialists import is_e4l_specialist

        if is_e4l_specialist(str(bundle.get("slug") or slug)):
            try:
                from accounting.runtime_context import enrich_bundle

                bundle = enrich_bundle(bundle, params or {}, task or "")
            except Exception:
                log.exception("accounting entity pack load failed for %s", slug)
                return {
                    "ok": False,
                    "error": "accounting_entity_load_failed",
                    "slug": slug,
                    "job_id": job_id,
                }
        if inherited_token_budget is not None:
            bundle["token_budget"] = min(
                int(bundle.get("token_budget", inherited_token_budget)),
                max(0, int(inherited_token_budget)),
            )
        if inherited_cost_budget_cents is not None:
            bundle["conduit_budget_cents"] = min(
                int(bundle.get("conduit_budget_cents", inherited_cost_budget_cents)),
                max(0, int(inherited_cost_budget_cents)),
            )

        # Phase 2/6: Register workspace and set initial sandbox status
        if _WS_MANAGER_OK:
            ws = create_workspace(job_id, session_id)
            job_dir = ws.path
            set_workspace_status(job_id, "ACTIVE")
        else:
            job_dir = Path(f"/tmp/jobs/{job_id}")
            job_dir.mkdir(parents=True, exist_ok=True)

        # Phase 3: durable session record (survives restart; retrievable via
        # GET /agents/sessions/{id}). Best-effort — never blocks the job.
        if _DURABLE_OK:
            try:
                durable_store.session_create(
                    session_id=session_id,
                    job_id=job_id,
                    agent_slug=slug,
                    parent_job_id=parent_job_id,
                    parent_session_id=parent_session_id,
                    workspace_root=str(job_dir),
                )
            except Exception:
                log.debug("durable session_create failed for %s", job_id, exc_info=True)

        # Phase 4: emit job.created
        emit_event(job_id, EVT_JOB_CREATED if _OBS_OK else "job.created", {
            "session_id": session_id,
            "agent_slug": slug,
        })

        # Lazy-init Conduit bridge per job (only if conduit is in tools_advertised)
        bridge = None
        buyer_session: Any = None
        if "conduit" in bundle.get("tools_advertised", []):
            try:
                from conduit_browser import ConduitBridge
                bridge = ConduitBridge(
                    session_id=job_id,
                    budget_cents=bundle.get("conduit_budget_cents", 200),
                    data_dir=job_dir / "conduit",
                )
                # Phase 9b - if a buyer uploaded a Conduit session for this
                # job ("Concierge Mode"), pull it from the encrypted vault now
                # and inject it into the bridge AFTER start() (Playwright
                # context must exist before add_cookies will accept anything).
                try:
                    from conduit_sessions import load_session
                    sess_result = load_session(job_id=job_id)
                    if sess_result.get("ok") and sess_result.get("session_data"):
                        # The local Patchright bridge has no reviewed cookie
                        # import API. Refuse the entire job instead of silently
                        # dropping a buyer credential and claiming success.
                        from conduit_sessions import delete_session
                        delete_session(job_id=job_id)
                        return {
                            "ok": False,
                            "error": "buyer_session_injection_unsupported",
                            "slug": slug,
                            "job_id": job_id,
                        }
                        log.info(
                            "loading buyer session for job %s (concierge mode)",
                            job_id,
                        )
                except Exception:
                    log.exception("session load failed; continuing without buyer session")

                # Lazy browser: only launch Chromium up-front when we must
                # inject a buyer session (concierge mode). Otherwise the browser
                # starts on first conduit tool call (ensure_started), so jobs
                # that never browse use ZERO browser memory. This + the stop()
                # fix below prevents the per-job Chromium leak that OOM'd the
                # instance.
                if buyer_session is not None:
                    await bridge.start()
                    try:
                        # Conduit's session-import API is the BrowserTool's
                        # cookie-jar label system: write the cookie array as
                        # a label file under the bridge's _session_dir, then
                        # call ConduitBridge.load_cookies(label=...) which
                        # internally invokes Playwright's
                        # `BrowserContext.add_cookies(cookies)` via
                        # BrowserTool._load_cookies. Audit event is recorded
                        # by the bridge as part of the call.
                        #
                        # Accepted buyer formats:
                        #   - Playwright storage_state dict:
                        #       {"cookies": [...], "origins": [...]}
                        #     (origins/localStorage not yet wired; cookies only.)
                        #   - Raw cookie array: [{name, value, domain, ...}, ...]
                        if isinstance(buyer_session, dict):
                            cookies_list = buyer_session.get("cookies", [])
                        elif isinstance(buyer_session, list):
                            cookies_list = buyer_session
                        else:
                            cookies_list = []

                        if cookies_list:
                            browser_tool = getattr(bridge, "_browser_tool", None)
                            if browser_tool is None or getattr(browser_tool, "_session_dir", None) is None:
                                raise RuntimeError("bridge._browser_tool not initialised after start()")
                            label = "buyer"
                            session_file = browser_tool._session_dir / f"{label}.json"
                            session_file.parent.mkdir(parents=True, exist_ok=True)
                            session_file.write_text(
                                json.dumps(cookies_list), encoding="utf-8"
                            )
                            inject_result = await bridge.load_cookies(label=label)
                            if not (inject_result or {}).get("success"):
                                raise RuntimeError(
                                    f"load_cookies returned non-success: {inject_result}"
                                )
                            log.info(
                                "buyer session injected into bridge for job %s (cookies=%d)",
                                job_id,
                                inject_result.get("count", len(cookies_list)),
                            )
                        else:
                            log.warning(
                                "buyer session for job %s had no cookies; nothing injected",
                                job_id,
                            )
                    except Exception:
                        log.exception(
                            "buyer session injection failed for job %s; continuing without",
                            job_id,
                        )
            except Exception:
                log.exception("ConduitBridge failed to start for %s", slug)
                bridge = None

        _result: dict[str, Any] | None = None
        try:
            _result = await self._run_loop(
                bundle, task, params, job_id, job_dir, bridge, session_id,
                delegation_chain=delegation_chain,
                delegation_depth=delegation_depth,
                delegated_allowed_risks=delegated_allowed_risks,
            )
            return _result
        finally:
            if bridge is not None:
                try:
                    await bridge.stop()
                except Exception:
                    log.warning("bridge.stop() failed for %s", job_id)
            # Phase 3: finalize the durable session for every return path
            # (success, failure, timeout, exception). Best-effort.
            if _DURABLE_OK:
                try:
                    _ok = bool(_result and _result.get("ok"))
                    durable_store.session_finish(
                        session_id,
                        status="COMPLETED" if _ok else "FAILED",
                        trace=(_result or {}).get("trace"),
                        error=None if _ok else (_result or {}).get("error"),
                    )
                except Exception:
                    log.debug("durable session_finish failed for %s", job_id, exc_info=True)
            # Concierge cleanup: delete buyer session from vault after job
            # completion so credentials don't accumulate on disk. Best-effort.
            try:
                from conduit_sessions import delete_session
                delete_session(job_id=job_id)
            except Exception:
                log.warning("session cleanup failed for job %s", job_id)

    async def _run_loop(
        self,
        bundle: dict[str, Any],
        task: str,
        params: dict[str, Any],
        job_id: str,
        job_dir: Path,
        bridge: Any,
        session_id: str = "",
        *,
        delegation_chain: tuple[str, ...] = (),
        delegation_depth: int = 0,
        delegated_allowed_risks: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        slug = bundle["slug"]
        last_swarmsync: dict[str, Any] | None = None
        tools_advertised = bundle.get("tools_advertised", [])
        token_budget = bundle.get("token_budget", 4000)
        model = bundle.get("model_hint", "anthropic/claude-sonnet-4-5")
        timeout_s = bundle.get("timeout_s", DEFAULT_TIMEOUT_S)

        # Phase 4: agent.started
        emit_event(job_id, "agent.started", {"session_id": session_id, "agent_slug": slug})

        system_prompt = bundle["system_prompt"]
        deployment_context, safe_params = _deployment_side_effect_authorized(params)
        user_prompt = task + (f"\n\nAdditional params: {json.dumps(safe_params)}" if safe_params else "")

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        # Denied/prohibited schemas never enter the model prompt. This prevents
        # wasted turns and closes the gap where the dispatcher denied a tool
        # only after the LLM had already been invited to call it.
        model_tools = [name for name in tools_advertised if check_tool_policy(slug, name)["ok"]]
        tools = tool_schemas_for(model_tools)

        started = time.time()
        turn = 0
        llm_calls = 0
        total_tokens = 0
        files_written = 0
        success_criteria = bundle.get("success_criteria")

        tool_call_records: list[dict[str, Any]] = []
        # Phase 5: separate subagent trace list
        subagent_records: list[dict[str, Any]] = []

        while turn < MAX_TURNS:
            turn += 1
            if time.time() - started > timeout_s:
                return {
                    "ok": False,
                    "error": "timeout",
                    "slug": slug,
                    "turns_completed": turn - 1,
                }

            # Phase 11 — enforce LLM call cap (parallel to MAX_TURNS, makes
            # the limit explicit and easier to audit).
            if llm_calls >= MAX_LLM_CALLS:
                return {
                    "ok": False,
                    "error": "llm_call_limit_exceeded",
                    "slug": slug,
                    "llm_calls": llm_calls,
                    "limit": MAX_LLM_CALLS,
                }

            # Call LLM
            try:
                response = await self._call_llm(model, messages, tools, token_budget)
                llm_calls += 1
                if isinstance(response.get("swarmsync"), dict):
                    last_swarmsync = response["swarmsync"]
            except Exception as e:
                log.exception("LLM call failed turn=%d slug=%s", turn, slug)
                return {
                    "ok": False,
                    "error": "llm_call_failed",
                    "type": type(e).__name__,
                    "message": str(e),
                }

            # Phase 11 — aggregate token-budget enforcement.
            try:
                usage = response.get("usage") or {}
                total_tokens += int(usage.get("total_tokens", 0) or 0)
            except Exception:
                pass
            if total_tokens > MAX_TOKENS_PER_JOB:
                return {
                    "ok": False,
                    "error": "token_budget_exceeded",
                    "slug": slug,
                    "total_tokens": total_tokens,
                    "limit": MAX_TOKENS_PER_JOB,
                }

            # Parse response - OpenAI-format expected
            choices = response.get("choices", [])
            if not choices:
                return {"ok": False, "error": "no_choices_in_llm_response"}

            msg = choices[0].get("message", {})
            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")

            if not tool_calls:
                # Final answer
                _finished_at = time.time()
                result: dict[str, Any] = {
                    "ok": True,
                    "slug": slug,
                    "response": content,
                    "turns": turn,
                    "elapsed_s": round(_finished_at - started, 2),
                    "job_id": job_id,
                    "resource_usage": {
                        "llm_calls": llm_calls,
                        "total_tokens": total_tokens,
                        "files_written": files_written,
                    },
                    "trace": {
                        "job_id": job_id,
                        "session_id": session_id,
                        "agent_slug": slug,
                        "workspace_path": str(job_dir),
                        "artifact_count": files_written,
                        "tool_calls": tool_call_records,
                        "subagents": subagent_records,
                        "started_at": started,
                        "finished_at": _finished_at,
                        "status": "ok",
                    },
                }
                if last_swarmsync:
                    result["swarmsync"] = last_swarmsync
                    routed = last_swarmsync.get("routed_model") or ""
                    result["routing"] = {
                        "model": routed,
                        "provider": routed.split("/")[0] if "/" in routed else routed,
                        "tier": last_swarmsync.get("tier"),
                        "routing_reason": last_swarmsync.get("routing_reason"),
                        "estimated_cost": last_swarmsync.get("estimated_cost"),
                        "latency_ms": last_swarmsync.get("latency_ms"),
                    }

                # Phase 11 — validate success_criteria against the structured
                # result. If any fail, mark the job FAILED so the worker can
                # refund escrow (Phase 6) and reputation tracking updates.
                criteria_eval = _check_success_criteria(
                    success_criteria,
                    result,
                    require_tool_evidence=bool(tools_advertised),
                )
                result["success_criteria_eval"] = criteria_eval
                if not criteria_eval["ok"]:
                    result["ok"] = False
                    result["error"] = "success_criteria_failed"
                result["trace"]["status"] = "ok" if result["ok"] else "failed"

                # Phase 4/6: emit completion event + transition sandbox
                _final_evt = "job.completed" if result["ok"] else "job.failed"
                emit_event(job_id, _final_evt, {
                    "session_id": session_id,
                    "status": result["trace"]["status"],
                    "turns": turn,
                    "elapsed_s": result.get("elapsed_s"),
                })
                if _WS_MANAGER_OK:
                    set_workspace_status(job_id, "FINALIZING")

                # Phase 7 — generate VCAP proof bundle only if the browser was
                # actually used (started). Skipping when unused avoids launching
                # Chromium just to produce a proof for a non-browser job.
                if bridge is not None and getattr(bridge, "is_started", lambda: True)():
                    try:
                        from proof_bridge import generate_proof_for_job
                        proof = await generate_proof_for_job(
                            job_id=job_id,
                            agent_slug=slug,
                            bridge=bridge,
                            job_dir=job_dir,
                            input_data={"task": task, "params": params},
                            output_data={"response": content, "turns": turn},
                            started_at=started,
                            completed_at=time.time(),
                        )
                        if proof.get("ok"):
                            result["proof"] = {
                                "proof_id": proof.get("proof_id"),
                                "vcap_wrapper_jwt": proof.get("vcap_wrapper_jwt"),
                                "proof_bundle_signed_url": proof.get("signed_url"),
                                "input_hash": proof.get("input_hash"),
                                "output_hash": proof.get("output_hash"),
                            }
                        else:
                            result["proof"] = {
                                "ok": False,
                                "error": proof.get("error"),
                            }
                    except Exception:
                        log.exception("proof generation raised; continuing without proof")
                        result["proof"] = {
                            "ok": False,
                            "error": "proof_pipeline_exception",
                        }

                return result

            # Append the assistant message to history
            messages.append(msg)

            # Execute each tool call
            for tc in tool_calls:
                tc_id = tc.get("id", "unknown")
                fn_name = tc.get("function", {}).get("name", "")
                raw_args = tc.get("function", {}).get("arguments", "{}")
                try:
                    args = json.loads(raw_args)
                except Exception:
                    args = {}

                tc_started = time.time()
                tool_result: dict[str, Any]
                tool_executed = False
                # Layer 4 — prohibition pre-check. Runs BEFORE check_tool_policy
                # and before any tool is invoked, and is independent of TOOL_RISK
                # and slug risk grants, so a mistake in the risk table cannot
                # reach a prohibited tool. This is also the only place slug-scoped
                # prohibition is evaluated.
                tool = None if is_prohibited(fn_name, slug) else get_tool(fn_name)
                if is_prohibited(fn_name, slug):
                    # Reaching here means Layers 1-3 have failed. Severity is
                    # critical on purpose: this must page.
                    log.critical(
                        "PROHIBITED TOOL REACHED DISPATCHER: tool=%s slug=%s job=%s",
                        fn_name, slug, job_id,
                    )
                    emit_event(job_id, "tool.prohibited", {
                        "tool_name": fn_name,
                        "agent_slug": slug,
                        "severity": "critical",
                        "prohibition_group": prohibition_group(fn_name),
                        "session_id": session_id,
                    })
                    tool_result = prohibited_refusal(
                        fn_name,
                        group=prohibition_group(fn_name),
                        agent_slug=slug,
                    )
                elif tool is None or fn_name not in tools_advertised:
                    tool_result = {
                        "ok": False,
                        "error": "tool_not_allowed",
                        "tool": fn_name,
                    }
                else:
                    # Phase 8: policy check before execution
                    _policy = check_tool_policy(slug, fn_name)
                    if not _policy["ok"]:
                        emit_event(job_id, "tool.blocked", {
                            "tool_name": fn_name,
                            "agent_slug": slug,
                            "risk_class": _policy.get("risk_class"),
                            "session_id": session_id,
                        })
                        tool_result = {
                            "ok": False,
                            "error": "tool_policy_denied",
                            "tool": fn_name,
                            "risk_class": _policy.get("risk_class"),
                        }
                    elif (
                        delegated_allowed_risks is not None
                        and _policy.get("risk_class") not in delegated_allowed_risks
                    ):
                        tool_result = {
                            "ok": False,
                            "error": "delegation_privilege_escalation",
                            "tool": fn_name,
                            "risk_class": _policy.get("risk_class"),
                        }
                    elif _policy.get("risk_class") == RISK_DEPLOYMENT:
                        try:
                            from runtime.action_grants import consume_action_grant
                            consume_action_grant(
                                deployment_context["grant"],
                                principal_id=deployment_context["principal_id"],
                                tenant_id=deployment_context["tenant_id"],
                                tool=fn_name,
                                args=args,
                            )
                            deployment_authorized = True
                        except Exception:
                            deployment_authorized = False
                    if _policy.get("risk_class") == RISK_DEPLOYMENT and not deployment_authorized:
                        emit_event(job_id, "tool.blocked", {
                            "tool_name": fn_name,
                            "agent_slug": slug,
                            "risk_class": RISK_DEPLOYMENT,
                            "reason": "deployment_approval_required",
                            "session_id": session_id,
                        })
                        tool_result = {
                            "ok": False,
                            "error": "deployment_approval_required",
                            "tool": fn_name,
                            "risk_class": RISK_DEPLOYMENT,
                        }
                    # Phase 11 — file_write quota enforced BEFORE the call.
                    elif fn_name == "file_write" and files_written >= MAX_FILES_WRITTEN:
                        tool_result = {
                            "ok": False,
                            "error": "file_write_limit_exceeded",
                            "files_written": files_written,
                            "limit": MAX_FILES_WRITTEN,
                        }
                    else:
                        if _policy.get("risk_class") != RISK_READ_ONLY:
                            try:
                                append_tool_intent(
                                    session_id=session_id,
                                    tool_name=fn_name,
                                    inputs=args,
                                )
                            except Exception:
                                log.exception(
                                    "Genesis audit preflight failed job=%s tool=%s",
                                    job_id,
                                    fn_name,
                                )
                                tool_result = {
                                    "ok": False,
                                    "error": "audit_preflight_failed",
                                    "tool": fn_name,
                                }
                                tool = None
                        if tool is None:
                            pass
                        else:
                            _remaining_budget = self.remaining_delegation_budget(
                                job_id,
                                token_budget=token_budget,
                                total_tokens=total_tokens,
                                cost_budget_cents=int(bundle.get("conduit_budget_cents", 0)),
                            )
                            # Inject context: bridge, job_dir, runtime, parent_job_id, session_id
                            ctx = {
                                "_bridge": bridge,
                                "_job_dir": job_dir,
                                "_runtime": self,
                                "_parent_job_id": job_id,
                                "_session_id": session_id,
                                "_parent_agent_slug": slug,
                                "_delegation_chain": (*delegation_chain, slug),
                                "_delegation_depth": delegation_depth,
                                # Subtract what earlier siblings already spent, or the
                                # ceiling applies per-child instead of per-tree.
                                "_remaining_token_budget": _remaining_budget[0],
                                "_remaining_cost_budget_cents": _remaining_budget[1],
                            }
                            emit_event(job_id, "tool.called", {
                                "tool_name": fn_name,
                                "session_id": session_id,
                                "turn": turn,
                            })
                            try:
                                tool_executed = True
                                tool_result = await tool(**args, **ctx)
                                # Phase 11 — count successful file writes.
                                if (
                                    fn_name == "file_write"
                                    and isinstance(tool_result, dict)
                                    and tool_result.get("ok")
                                ):
                                    files_written += 1
                            except Exception as e:
                                log.exception("tool %s raised", fn_name)
                                tool_result = {
                                    "ok": False,
                                    "error": "tool_exception",
                                    "type": type(e).__name__,
                                    "message": str(e),
                                }

                tc_finished = time.time()

                if tool_executed:
                    try:
                        append_tool_event(
                            session_id=session_id,
                            tool_name=fn_name,
                            inputs=args,
                            outputs=tool_result if isinstance(tool_result, dict) else {"ok": False},
                        )
                    except Exception:
                        log.exception("Genesis audit append failed job=%s tool=%s", job_id, fn_name)
                        tool_result = {
                            "ok": False,
                            "error": "audit_append_failed",
                            "tool": fn_name,
                        }

                # Build structured trace record
                record: dict[str, Any] = {
                    "turn": turn,
                    "tool_name": fn_name,
                    "tool_call_id": tc_id,
                    "arguments": {k: v for k, v in args.items() if not k.startswith("_")},
                    "ok": bool(tool_result.get("ok")) if isinstance(tool_result, dict) else False,
                    "result_summary": json.dumps(tool_result)[:300] if isinstance(tool_result, dict) else str(tool_result)[:300],
                    "started_at": tc_started,
                    "finished_at": tc_finished,
                    "elapsed_s": round(tc_finished - tc_started, 3),
                    "parent_job_id": job_id,
                    "parent_agent_slug": slug,
                }
                # genesis_call gets extra linkage fields + Phase 5 subagent trace entry
                if fn_name == "genesis_call" and isinstance(tool_result, dict):
                    record["target_agent_slug"] = (
                        tool_result.get("target_agent_slug")
                        or tool_result.get("agent", "")
                    )
                    record["child_job_id"] = tool_result.get("child_job_id")
                    record["child_session_id"] = tool_result.get("child_session_id")
                    record["child_ok"] = tool_result.get(
                        "child_ok", bool(tool_result.get("ok"))
                    )
                    child_summary = tool_result.get("child_response_summary") or ""
                    if not child_summary and isinstance(tool_result.get("result"), dict):
                        child_summary = str(tool_result["result"].get("response", ""))[:300]
                    record["child_response_summary"] = child_summary

                    # Phase 5: dedicated subagent trace entry
                    subagent_records.append({
                        "parent_job_id": job_id,
                        "parent_session_id": session_id,
                        "parent_agent_slug": slug,
                        "child_job_id": tool_result.get("child_job_id"),
                        "child_session_id": tool_result.get("child_session_id"),
                        "child_agent_slug": record["target_agent_slug"],
                        "task": args.get("task", ""),
                        "status": "ok" if record["child_ok"] else "failed",
                        "child_ok": record["child_ok"],
                        "child_trace_uri": None,
                        "artifact_uris": [],
                    })

                    emit_event(job_id, "subagent.returned", {
                        "child_job_id": tool_result.get("child_job_id"),
                        "child_session_id": tool_result.get("child_session_id"),
                        "child_agent_slug": record["target_agent_slug"],
                        "child_ok": record["child_ok"],
                        "session_id": session_id,
                    })

                tool_call_records.append(record)

                # One Phoenix span per tool call. Emitted here because this is the
                # single point every dispatch branch (blocked, sandboxed, executed,
                # delegated, errored) converges on, so no outcome can be missed.
                try:
                    _phoenix.emit_completed_span(
                        f"tool.{fn_name or 'unknown'}",
                        kind="TOOL",
                        start_time_s=record.get("started_at"),
                        end_time_s=record.get("finished_at"),
                        ok=bool(record.get("ok")),
                        # Status text is a span field like any other, so it must go
                        # through the same content gate. Raw result_summary can hold
                        # retrieved knowledge_backbone text; never send it uncleared.
                        error_message=(
                            None if record.get("ok")
                            else (_phoenix.safe_content(record.get("result_summary"))
                                  or "tool_call_failed")
                        ),
                        attributes={
                            _phoenix.TOOL_NAME: fn_name,
                            "genesis.tool.call_id": tc_id,
                            "genesis.tool.executed": tool_executed,
                            "genesis.tool.ok": bool(record.get("ok")),
                            "genesis.tool.elapsed_s": record.get("elapsed_s"),
                            "genesis.turn": turn,
                            "genesis.agent.slug": slug,
                            "genesis.job.id": job_id,
                            "genesis.tool.target_agent_slug": record.get("target_agent_slug"),
                            "genesis.tool.child_job_id": record.get("child_job_id"),
                            _phoenix.INPUT_VALUE: _phoenix.safe_content(record.get("arguments")),
                            _phoenix.OUTPUT_VALUE: _phoenix.safe_content(record.get("result_summary")),
                        },
                    )
                except Exception:  # pragma: no cover - tracing must never break dispatch
                    pass

                # Append tool result message
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(tool_result)[:DEFAULT_MAX_OUTPUT_BYTES],
                })

        return {
            "ok": False,
            "error": "max_turns_reached",
            "slug": slug,
            "turns": turn,
            "resource_usage": {
                "llm_calls": llm_calls,
                "total_tokens": total_tokens,
                "files_written": files_written,
            },
        }

    def _is_anthropic(self) -> bool:
        """Provider selection: explicit env signal first, URL detection as the fallback.

        GENESIS_LLM_PROVIDER is authoritative when set, so an operator can force either path
        without editing a URL. Otherwise an api.anthropic.com URL selects the Anthropic wire
        format — pointing at Anthropic and getting OpenAI-shaped requests is never intended.
        """
        from runtime.anthropic_wire import is_anthropic_url

        declared = (os.getenv("GENESIS_LLM_PROVIDER") or "").strip().lower()
        if declared:
            return declared == "anthropic"
        return is_anthropic_url(self.llm_url)

    async def _call_anthropic(
        self, model: str, messages: list, tools: list, max_tokens: int
    ) -> dict[str, Any]:
        """POST /v1/messages, returning the OpenAI-shaped dict the agent loop expects."""
        import aiohttp

        from runtime.anthropic_wire import (
            ANTHROPIC_FALLBACK_MODELS,
            ANTHROPIC_VERSION,
            build_anthropic_request,
            from_anthropic_response,
            translate_model_id,
        )

        env_model = (os.getenv("GENESIS_LLM_MODEL") or "").strip()
        chosen = env_model if (env_model and env_model != "auto") else model
        candidates: list[str] = []
        for candidate in (translate_model_id(chosen), *ANTHROPIC_FALLBACK_MODELS):
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        headers = {
            "x-api-key": self.llm_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        url = self.llm_url if self.llm_url.rstrip("/").endswith("/v1/messages") else (
            self.llm_url.rstrip("/") + "/v1/messages"
        )

        timeout = aiohttp.ClientTimeout(total=120)
        last_error = "unknown"
        async with _llm_client_session(timeout) as session:
            for routed_model in candidates:
                body = build_anthropic_request(
                    model=routed_model, messages=messages, tools=tools, max_tokens=max_tokens
                )
                async with session.post(url, headers=headers, json=body) as resp:
                    if resp.status in (200, 201):
                        return from_anthropic_response(await resp.json())
                    text = await resp.text()
                    last_error = f"LLM HTTP {resp.status}: {text[:500]}"
                    # Only a model-availability or capacity problem is worth retrying on another
                    # model. A 401/403 is the same for every model, and retrying it three times
                    # just buries the real cause.
                    if resp.status in (404, 429, 529):
                        log.warning(
                            "anthropic call failed status=%s model=%s; trying next model",
                            resp.status, routed_model,
                        )
                        continue
                    raise RuntimeError(last_error)
        raise RuntimeError(last_error)

    async def _call_llm(
        self,
        model: str,
        messages: list,
        tools: list,
        max_tokens: int,
    ) -> dict[str, Any]:
        """Traced wrapper over :meth:`_call_llm_inner`.

        This is deliberately the instrumentation point for *both* providers.
        ``_call_llm_inner`` is where the Anthropic-vs-gateway fork happens, so a
        span opened here captures the direct-Anthropic path and the OpenAI-shaped
        SwarmSync gateway path identically — including the model actually routed
        to after fallback, which is read back off the response rather than
        assumed from the request.
        """
        provider = "anthropic" if self._is_anthropic() else "openai_gateway"
        with _phoenix.span(
            "llm.completion",
            kind="LLM",
            attributes={
                _phoenix.LLM_PROVIDER: provider,
                _phoenix.LLM_MODEL_NAME: model,
                "llm.invocation_parameters.max_tokens": max_tokens,
                "llm.tools.count": len(tools or []),
                "llm.messages.count": len(messages or []),
                _phoenix.INPUT_VALUE: _phoenix.safe_content(messages),
            },
        ) as sp:
            response = await self._call_llm_inner(model, messages, tools, max_tokens)
            try:
                usage = (response or {}).get("usage") or {}
                _phoenix.set_attributes(sp, {
                    # Routed model: after fallback this can differ from `model`.
                    "llm.model_name.routed": (response or {}).get("model"),
                    _phoenix.LLM_TOKEN_PROMPT: usage.get("prompt_tokens"),
                    _phoenix.LLM_TOKEN_COMPLETION: usage.get("completion_tokens"),
                    _phoenix.LLM_TOKEN_TOTAL: usage.get("total_tokens"),
                    _phoenix.OUTPUT_VALUE: _phoenix.safe_content(response),
                })
            except Exception:  # pragma: no cover
                pass
            return response

    async def _call_llm_inner(
        self,
        model: str,
        messages: list,
        tools: list,
        max_tokens: int,
    ) -> dict[str, Any]:
        """Call the configured LLM endpoint.

        Two providers, selected explicitly. The agent loop always speaks OpenAI shape; the
        Anthropic branch translates at the wire and translates the response back, so nothing
        downstream (trace, budget accounting, tool dispatch) has to know which provider ran.
        """
        import aiohttp

        if self._is_anthropic():
            return await self._call_anthropic(model, messages, tools, max_tokens)

        allow_openrouter_fallback = os.getenv("GENESIS_ALLOW_OPENROUTER_FALLBACK", "").lower() in {
            "1",
            "true",
            "yes",
        }
        if any(marker in self.llm_url for marker in OPENROUTER_HOST_MARKERS) and not allow_openrouter_fallback:
            raise RuntimeError(
                "OpenRouter is disabled for Genesis agents; set LLM_API_URL to "
                "https://api.swarmsync.ai/v1/chat/completions or explicitly enable "
                "GENESIS_ALLOW_OPENROUTER_FALLBACK=true"
            )

        env_model = (os.getenv("GENESIS_LLM_MODEL") or "").strip()
        # When the env var is "auto" or absent, respect the bundle's model_hint so
        # agents that need function-calling (e.g. genesis-meta) get a capable model.
        # A non-"auto" env value (e.g. "anthropic/claude-haiku-4-5") overrides all bundles.
        if env_model and env_model != "auto":
            primary = env_model
        else:
            primary = model if (model and model != "auto") else DEFAULT_SWARMSYNC_MODEL
        model_candidates = [primary, "openrouter/free", "minimax/minimax-m2.5:free"]
        deduped: list[str] = []
        for m in model_candidates:
            if m and m not in deduped:
                deduped.append(m)

        headers = {
            "Authorization": f"Bearer {self.llm_key}",
            "Content-Type": "application/json",
        }

        timeout = aiohttp.ClientTimeout(total=120)
        last_error = "unknown"
        async with _llm_client_session(timeout) as session:
            for routed_model in deduped:
                body: dict[str, Any] = {
                    "model": routed_model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "tools": tools if tools else None,
                    "tool_choice": "auto" if tools else None,
                }
                body = {k: v for k, v in body.items() if v is not None}
                async with session.post(self.llm_url, headers=headers, json=body) as resp:
                    if resp.status in (200, 201):
                        return await resp.json()
                    text = await resp.text()
                    last_error = f"LLM HTTP {resp.status}: {text[:500]}"
                    combined = text.lower()
                    if resp.status in (400, 402, 429) or any(
                        x in combined for x in ("402", "credit", "balance", "quota", "payment")
                    ):
                        log.warning(
                            "LLM call failed status=%s model=%s; trying next model",
                            resp.status,
                            routed_model,
                        )
                        continue
                    raise RuntimeError(last_error)
        raise RuntimeError(last_error)
