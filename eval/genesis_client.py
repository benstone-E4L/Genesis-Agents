"""Thin async client for ``POST /agents/{slug}/run`` on the Genesis gateway.

Genesis is a plain FastAPI service that speaks raw HTTP; there is no LangChain,
no OpenAI SDK and no Claude Agent SDK anywhere in the call path. This module is
therefore an ordinary HTTP client with a swappable transport, so every test in
this package runs against a fake and never touches the live service.

Design notes that are load-bearing:

* **Timeout > proxy timeout.** Render's free tier kills a proxied request at
  30s. The client default is 60s so that "the proxy gave up" (a 502/504 arriving
  at ~30s) is distinguishable from "our own client gave up".
* **Read/write timeouts are INDETERMINATE, not failures.** If the request bytes
  reached the wire and the response never came back, the agent may well have
  run. Reporting that as a failure is wrong, and retrying a non-idempotent
  ``/run`` could double-invoke a paid agent. Both are refused here.
* **4xx is never retried.** Ever. A 401 does not become a 200 by asking again.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol

from .redaction import redact, redact_text

DEFAULT_BASE_URL = "https://swarmsync-agents.onrender.com"
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_WARMUP_TIMEOUT_S = 20.0
DEFAULT_MAX_ATTEMPTS = 3  # 1 initial attempt + at most 2 retries
DEFAULT_BACKOFF_BASE_S = 0.5
DEFAULT_BACKOFF_MAX_S = 8.0

#: Live gateway slugs, snapshotted verbatim from an unauthenticated
#: ``GET /agents`` on 2026-08-03. Independently confirmed identical to
#: ``main.py::AGENT_PERSONAS`` (lines 354-719).
#:
#: This allowlist is a correctness control, not a convenience. A slug the
#: gateway does not recognise does NOT 404 on ``/run``: ``run_agent()`` falls
#: back to a generic persona built from ``slug.title()`` and returns HTTP 200
#: with a bland answer. Only ``GET /capabilities`` 404s. So a typo'd slug
#: silently produces a plausible-looking success that would quietly poison an
#: evaluation. The allowlist is what catches that — see ``strict_slugs``.
#:
#: Keep in sync with the live service. ``tests/test_slugs.py`` pins the count
#: and ``tests/test_live_catalogue.py`` fails loudly if live diverges.
LIVE_SLUGS: frozenset[str] = frozenset(
    {
        # x402 marketplace agents (15)
        "genesis_research_x402", "genesis_builder_x402", "genesis_deploy_x402",
        "genesis_content_x402", "genesis_email_x402", "genesis_commerce_x402",
        "genesis_qa_x402", "genesis_support_x402", "genesis_finance_x402",
        "genesis_security_x402", "genesis_billing_x402", "genesis_analyst_x402",
        "genesis_marketing_x402", "genesis_seo_x402", "genesis_meta_x402",
        # core named agents (29)
        "genesis_meta_agent", "builder_agent", "builder_agent_enhanced",
        "deploy_agent", "qa_agent", "research_discovery_agent", "spec_agent",
        "security_agent", "maintenance_agent", "seo_agent", "content_agent",
        "marketing_agent", "support_agent", "analyst_agent", "finance_agent",
        "pricing_agent", "email_agent", "billing_agent", "commerce_agent",
        "darwin_agent", "domain_name_agent", "legal_agent", "onboarding_agent",
        "reflection_agent", "waltzrl_conversation_agent",
        "waltzrl_feedback_agent", "se_darwin_agent", "ring1t_reasoning_agent",
        "business_idea_generator",
        # hyphenated product agents (13)
        "genesis-ai-vision-api", "genesis-workflow-automator",
        "genesis-data-pipeline-agent", "unit-test-generator",
        "api-documentation-generator", "social-media-scheduler",
        "web-scraper-pro", "meeting-summarizer", "expense-tracker",
        "review-responder", "onboarding-automation", "image-optimizer",
        "backup-manager",
    }
)

#: Pinned size of :data:`LIVE_SLUGS`. Asserted at import and against the live
#: service by the opt-in catalogue test.
LIVE_SLUG_COUNT = 57

if len(LIVE_SLUGS) != LIVE_SLUG_COUNT:  # pragma: no cover - import-time guard
    raise RuntimeError(
        f"LIVE_SLUGS holds {len(LIVE_SLUGS)} entries but LIVE_SLUG_COUNT is "
        f"{LIVE_SLUG_COUNT}; re-derive the snapshot from GET /agents"
    )

#: Bundle-file slugs whose live counterpart does not follow the
#: ``hyphen -> underscore + _x402`` rule. Inverse of
#: ``bundle_loader.BUNDLE_SLUG_ALIASES``.
BUNDLE_TO_LIVE_SLUG: Mapping[str, str] = {
    "genesis-meta": "genesis_meta_x402",
    "genesis-legal": "legal_agent",
    "genesis-hr": "onboarding_agent",
    "genesis-onboarding": "onboarding_agent",
    "genesis-domain": "domain_name_agent",
    "genesis-maintenance": "maintenance_agent",
    "genesis-pricing": "pricing_agent",
    "genesis-ai-vision": "genesis-ai-vision-api",
    "genesis-data-pipeline": "genesis-data-pipeline-agent",
}

#: Money-domain agents. Evaluating these can move real value, so they are
#: refused before a request is ever built. Override only with an explicit
#: ``allow_money_domain=True``.
MONEY_DOMAIN_MARKERS = (
    "finance", "billing", "commerce", "pricing",
    "escrow", "payment", "payout", "invoice",
)


class Outcome(str, Enum):
    """The five mutually exclusive outcomes of an agent invocation."""

    SUCCESS = "success"
    #: 401/403 — credential rejected. Never retried.
    AUTH_ERROR = "auth_error"
    #: 404 — slug not served in that form. Never retried.
    NOT_FOUND = "not_found"
    #: The call definitively did not produce a result: 5xx, connect failure,
    #: or a malformed request (other 4xx). ``error_kind`` disambiguates.
    UPSTREAM_ERROR = "upstream_error"
    #: The request reached the wire and the outcome is UNKNOWN — the agent may
    #: have run. Not a failure. Never retried.
    INDETERMINATE = "indeterminate"


class MoneyDomainBlocked(RuntimeError):
    """Raised instead of invoking a finance/billing/commerce/pricing agent."""


class UnknownSlug(RuntimeError):
    """Raised instead of invoking a slug the live catalogue does not contain.

    This exists because the failure mode is *silent success*, not a 404. The
    gateway answers an unrecognised slug with a generic persona derived from
    ``slug.title()`` and returns HTTP 200. Scoring that output would be scoring
    a stub. Refusing up front is the only way to catch a typo'd slug.
    """


@dataclass(frozen=True)
class RawResponse:
    status_code: int
    text: str
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        import json

        try:
            return json.loads(self.text)
        except Exception:
            return None


class TransportFailure(Exception):
    """A request failed below the HTTP-status layer.

    ``kind`` drives retry and outcome classification:

    ``connect`` / ``pool`` / ``dns``
        The request never reached the server. Safe to retry.
    ``read`` / ``write``
        Bytes were sent. The server may have executed the call. NOT safe to
        retry and NOT reportable as a failure.
    ``protocol`` / ``other``
        Unknown position. Treated as not-sent (retryable) because a protocol
        error normally means the exchange never completed a request.
    """

    SENT_KINDS = frozenset({"read", "write"})

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(f"{kind}: {redact_text(message)}")
        self.kind = kind
        self.message = redact_text(message)

    @property
    def request_sent(self) -> bool:
        return self.kind in self.SENT_KINDS


class Transport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Any | None,
        timeout_s: float,
    ) -> RawResponse: ...

    async def aclose(self) -> None: ...


class HttpxTransport:
    """Default transport. httpx exceptions are mapped to TransportFailure kinds."""

    def __init__(self) -> None:
        self._client: Any = None

    async def _ensure(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(follow_redirects=True)
        return self._client

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Any | None,
        timeout_s: float,
    ) -> RawResponse:
        import httpx

        client = await self._ensure()
        try:
            resp = await client.request(
                method,
                url,
                headers=dict(headers),
                json=json_body,
                timeout=timeout_s,
            )
        except httpx.ConnectTimeout as exc:
            raise TransportFailure("connect", str(exc)) from None
        except httpx.ReadTimeout as exc:
            raise TransportFailure("read", str(exc)) from None
        except httpx.WriteTimeout as exc:
            raise TransportFailure("write", str(exc)) from None
        except httpx.PoolTimeout as exc:
            raise TransportFailure("pool", str(exc)) from None
        except httpx.ConnectError as exc:
            raise TransportFailure("connect", str(exc)) from None
        except httpx.ProtocolError as exc:
            raise TransportFailure("protocol", str(exc)) from None
        except httpx.HTTPError as exc:
            raise TransportFailure("other", str(exc)) from None
        return RawResponse(
            status_code=resp.status_code,
            text=resp.text,
            headers=dict(resp.headers),
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


@dataclass(frozen=True)
class AgentRunResult:
    """Structured outcome of one agent invocation. Never carries a secret."""

    outcome: Outcome
    slug: str
    requested_slug: str
    mode: str
    http_status: int | None = None
    elapsed_ms: int = 0
    attempts: int = 0
    error_kind: str | None = None
    error_message: str | None = None
    body: Any = None
    text: str = ""
    slug_resolution: str = "verified"
    warmed: bool = False

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.SUCCESS

    @property
    def determinate(self) -> bool:
        """False when the outcome is genuinely unknown. Do NOT score these."""
        return self.outcome is not Outcome.INDETERMINATE

    @property
    def response_text(self) -> str:
        """The agent's textual answer, per ``RunResponse.response`` in main.py."""
        if isinstance(self.body, dict):
            value = self.body.get("response")
            if isinstance(value, str):
                return value
        return self.text

    @property
    def agent_name(self) -> str | None:
        if isinstance(self.body, dict):
            name = self.body.get("agentName")
            if isinstance(name, str):
                return name
        return None


def resolve_live_slug(slug: str) -> tuple[str, str]:
    """Map any known slug spelling onto the form the LIVE gateway serves.

    ``skill_bundles/*.json`` uses hyphens (``genesis-research``); the deployed
    gateway serves underscores with an ``_x402`` suffix
    (``genesis_research_x402``). Sending the bundle form 404s.

    Returns ``(live_slug, resolution)`` where resolution is ``verified`` (present
    in the live catalogue), ``aliased`` (mapped via the alias table) or
    ``unverified`` (best-effort — a 404 from this is expected and explainable).
    """
    raw = (slug or "").strip()
    if not raw:
        raise ValueError("slug is required")

    if raw in LIVE_SLUGS:
        return raw, "verified"

    alias = BUNDLE_TO_LIVE_SLUG.get(raw) or BUNDLE_TO_LIVE_SLUG.get(raw.replace("_", "-"))
    if alias:
        return alias, "aliased"

    underscored = raw.replace("-", "_")
    if underscored in LIVE_SLUGS:
        return underscored, "verified"

    suffixed = underscored if underscored.endswith("_x402") else f"{underscored}_x402"
    if suffixed in LIVE_SLUGS:
        return suffixed, "verified"

    return underscored, "unverified"


def is_money_domain(slug: str) -> bool:
    lowered = (slug or "").lower()
    return any(marker in lowered for marker in MONEY_DOMAIN_MARKERS)


class GenesisClient:
    """Async client for the Genesis agent gateway.

    The credential is read from the environment at call time and is never
    stored on the result, logged, or included in an exception.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        warmup_timeout_s: float = DEFAULT_WARMUP_TIMEOUT_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
        jitter: bool = True,
        transport: Transport | None = None,
        warmup: bool = True,
        allow_money_domain: bool = False,
        strict_slugs: bool = True,
        api_key: str | None = None,
        gateway_secret: str | None = None,
        sleep: Any = None,
        clock: Any = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.warmup_timeout_s = warmup_timeout_s
        self.max_attempts = max_attempts
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.jitter = jitter
        self.transport: Transport = transport or HttpxTransport()
        self.warmup = warmup
        self.allow_money_domain = allow_money_domain
        self.strict_slugs = strict_slugs
        self._api_key_override = api_key
        self._gateway_secret_override = gateway_secret
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or time.monotonic
        self._warmed = False
        self._warm_attempted = False
        self._warm_lock = asyncio.Lock()

    # -- credentials ------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Build auth headers. Verified against ``main.py::verify_gateway_key``:
        ``X-Agent-Api-Key`` is checked against ``GATEWAY_API_KEY`` and
        ``X-Agent-Gateway-Secret`` against ``AGENT_GATEWAY_SECRET``; either is
        sufficient. Values are never logged or returned to callers.
        """
        headers: dict[str, str] = {}
        api_key = self._api_key_override
        if api_key is None:
            api_key = os.getenv("GATEWAY_API_KEY")
        if api_key:
            headers["X-Agent-Api-Key"] = api_key
        secret = self._gateway_secret_override
        if secret is None:
            secret = os.getenv("AGENT_GATEWAY_SECRET")
        if secret:
            headers["X-Agent-Gateway-Secret"] = secret
        return headers

    def has_credential(self) -> bool:
        """True if some credential is configured. Reveals nothing about it."""
        return bool(self._auth_headers())

    # -- warmup -----------------------------------------------------------

    async def ensure_warm(self) -> bool:
        """One-shot ``GET /health`` to absorb Render cold start.

        Runs at most once per client. Never raises, never blocks the run: a
        failed warmup just means the first real call pays the cold start.
        """
        if not self.warmup or self._warm_attempted:
            return self._warmed
        async with self._warm_lock:
            if self._warm_attempted:
                return self._warmed
            self._warm_attempted = True
            try:
                resp = await self.transport.request(
                    "GET",
                    f"{self.base_url}/health",
                    headers={"Accept": "application/json"},
                    json_body=None,
                    timeout_s=self.warmup_timeout_s,
                )
                self._warmed = 200 <= resp.status_code < 300
            except Exception:
                self._warmed = False
            return self._warmed

    # -- retry ------------------------------------------------------------

    def _backoff_delay(self, attempt: int) -> float:
        delay = min(self.backoff_base_s * (2 ** (attempt - 1)), self.backoff_max_s)
        if self.jitter:
            delay *= 0.5 + random.random() * 0.5
        return delay

    # -- main entry point -------------------------------------------------

    async def run_agent(
        self,
        slug: str,
        task: Any,
        *,
        mode: str = "live_test",
        require_artifact: bool = False,
        extra_body: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> AgentRunResult:
        """Invoke ``POST /agents/{slug}/run`` and classify the outcome.

        ``mode`` is ``"live_test"`` (sets ``mode: "live_test"`` +
        ``testContext: true``, which skips AgentRuntime/ConduitBridge and uses
        the fast persona LLM path — required on the Render free tier) or
        ``"full"`` (no bypass; the real runtime, which may exceed the 30s proxy
        timeout or return an async job envelope).
        """
        live_slug, resolution = resolve_live_slug(slug)

        if is_money_domain(live_slug) and not self.allow_money_domain:
            raise MoneyDomainBlocked(
                f"refusing to invoke money-domain agent {live_slug!r}; "
                "pass allow_money_domain=True to override"
            )
        if resolution == "unverified" and self.strict_slugs:
            # NOT a 404 risk — an unrecognised slug returns HTTP 200 from a
            # generic persona. Refuse rather than score a stub.
            raise UnknownSlug(
                f"{slug!r} resolved to {live_slug!r}, which is not in the live "
                f"catalogue of {LIVE_SLUG_COUNT} agents. The gateway would "
                "answer it with a generic persona and return 200, so this is "
                "refused. Pass strict_slugs=False to send it anyway."
            )
        if mode not in ("live_test", "full"):
            raise ValueError("mode must be 'live_test' or 'full'")

        body: dict[str, Any] = {"prompt": task if isinstance(task, str) else None}
        if not isinstance(task, str):
            body["input"] = task
        if mode == "live_test":
            body["mode"] = "live_test"
            body["testContext"] = True
        if require_artifact:
            body["require_artifact"] = True
        if extra_body:
            body.update(dict(extra_body))
        body = {k: v for k, v in body.items() if v is not None}

        warmed = await self.ensure_warm()

        url = f"{self.base_url}/agents/{live_slug}/run"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(self._auth_headers())
        effective_timeout = self.timeout_s if timeout_s is None else timeout_s

        started = self._clock()
        attempts = 0

        def _finish(**kw: Any) -> AgentRunResult:
            return AgentRunResult(
                slug=live_slug,
                requested_slug=slug,
                mode=mode,
                elapsed_ms=int((self._clock() - started) * 1000),
                attempts=attempts,
                slug_resolution=resolution,
                warmed=warmed,
                **kw,
            )

        while True:
            attempts += 1
            try:
                resp = await self.transport.request(
                    "POST",
                    url,
                    headers=headers,
                    json_body=body,
                    timeout_s=effective_timeout,
                )
            except TransportFailure as exc:
                if exc.request_sent:
                    # Bytes went out; the agent may have run. Unknown != failed,
                    # and a blind retry could invoke it twice.
                    return _finish(
                        outcome=Outcome.INDETERMINATE,
                        error_kind=f"transport_{exc.kind}",
                        error_message=exc.message,
                    )
                if attempts < self.max_attempts:
                    await self._sleep(self._backoff_delay(attempts))
                    continue
                return _finish(
                    outcome=Outcome.UPSTREAM_ERROR,
                    error_kind=f"transport_{exc.kind}",
                    error_message=exc.message,
                )

            status = resp.status_code
            parsed = resp.json()
            safe_body = redact(parsed) if parsed is not None else None
            safe_text = redact_text(resp.text or "")

            if 200 <= status < 300:
                return _finish(
                    outcome=Outcome.SUCCESS,
                    http_status=status,
                    body=safe_body,
                    text=safe_text,
                )

            if status in (401, 403):
                return _finish(  # never retried
                    outcome=Outcome.AUTH_ERROR,
                    http_status=status,
                    error_kind="auth_rejected",
                    error_message=safe_text[:500],
                    body=safe_body,
                )

            if status == 404:
                return _finish(  # never retried
                    outcome=Outcome.NOT_FOUND,
                    http_status=status,
                    error_kind="slug_not_found",
                    error_message=safe_text[:500],
                    body=safe_body,
                )

            if 400 <= status < 500:
                # Every other 4xx (400/422/429/...) is a definitive rejection of
                # THIS request. Retrying cannot change it, so we never do.
                return _finish(
                    outcome=Outcome.UPSTREAM_ERROR,
                    http_status=status,
                    error_kind=f"client_error_{status}",
                    error_message=safe_text[:500],
                    body=safe_body,
                )

            # 5xx — retryable up to the attempt cap.
            if attempts < self.max_attempts:
                await self._sleep(self._backoff_delay(attempts))
                continue
            return _finish(
                outcome=Outcome.UPSTREAM_ERROR,
                http_status=status,
                error_kind=f"server_error_{status}",
                error_message=safe_text[:500],
                body=safe_body,
            )

    async def aclose(self) -> None:
        await self.transport.aclose()

    async def __aenter__(self) -> "GenesisClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()
