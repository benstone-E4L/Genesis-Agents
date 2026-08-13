"""Arize Phoenix (OpenInference/OTLP) tracing for the Genesis agent runtime.

Why this exists separately from ``eval/traceable.py``:

``eval/`` is the *offline* evaluation harness. It traces to LangSmith from the
outside in — the harness is the client, Genesis is the server under test, and
``eval/__init__.py`` states plainly that nothing in that package is imported by
the Genesis service. This module is the *runtime* tracer: it runs inside the
service process and records what an agent actually did. The two never run in
the same process, so this is not "two tracing backends" — it is one backend per
layer. If the LangSmith eval harness is ever retired in favour of Phoenix
experiments, this module is the thing it would fold into.

Hard guarantees, in priority order:

1. **Observability never blocks execution.** Every public entry point in this
   module swallows every exception. A missing package, an unset endpoint, a DNS
   failure, a 500 from the collector, a hung TCP connect — all degrade to "no
   trace" and the agent run proceeds untouched. Export happens on a background
   thread via ``BatchSpanProcessor``; nothing in the request path ever waits on
   Phoenix.
2. **No secret ever reaches a span.** Prompt/response content is *not* recorded
   at all unless ``PHOENIX_TRACE_CONTENT`` is explicitly enabled, and even then
   it passes through the same redaction logic the LangSmith path uses. If that
   redaction module cannot be loaded, content is dropped rather than sent —
   fail *open* on tracing, fail *closed* on secrets.

Configuration (all optional; absence simply disables tracing):

* ``PHOENIX_COLLECTOR_ENDPOINT`` — Phoenix base URL, e.g. ``http://localhost:6006``
  or ``https://app.phoenix.arize.com/s/<space>``. ``PHOENIX_ENDPOINT`` and
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` are accepted as aliases.
* ``PHOENIX_API_KEY`` — sent as ``Authorization: Bearer <key>``.
* ``PHOENIX_PROJECT_NAME`` — Phoenix project; defaults to ``genesis-agents``.
* ``PHOENIX_TRACING`` — set to a falsey value to disable while the endpoint stays set.
* ``PHOENIX_TRACE_CONTENT`` — opt in to redacted prompt/response capture.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Optional

log = logging.getLogger(__name__)

DEFAULT_PROJECT = "genesis-agents"
_FALSEY = {"0", "false", "no", "off", ""}

# OpenInference semantic conventions. Hard-coded rather than imported so that a
# missing ``openinference-semantic-conventions`` package cannot break tracing;
# these string constants are a stable part of the OpenInference spec.
SPAN_KIND = "openinference.span.kind"
INPUT_VALUE = "input.value"
OUTPUT_VALUE = "output.value"
LLM_MODEL_NAME = "llm.model_name"
LLM_PROVIDER = "llm.provider"
LLM_TOKEN_PROMPT = "llm.token_count.prompt"
LLM_TOKEN_COMPLETION = "llm.token_count.completion"
LLM_TOKEN_TOTAL = "llm.token_count.total"
TOOL_NAME = "tool.name"

_lock = threading.Lock()
_tracer: Any = None
_provider: Any = None
_init_attempted = False
_disabled_reason: Optional[str] = None


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def collector_endpoint() -> str:
    """Phoenix base URL from the first configured alias, or ""."""
    for name in (
        "PHOENIX_COLLECTOR_ENDPOINT",
        "PHOENIX_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ):
        value = _env(name)
        if value:
            return value.rstrip("/")
    return ""


def tracing_enabled() -> bool:
    """True only when an endpoint is configured and tracing is not switched off.

    Deliberately does not require an API key: a self-hosted Phoenix with auth
    disabled is a valid target, and the decided E4L architecture is self-hosted.
    """
    if not collector_endpoint():
        return False
    flag = os.getenv("PHOENIX_TRACING")
    if flag is not None and flag.strip().lower() in _FALSEY:
        return False
    return True


_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def endpoint_is_offbox() -> bool:
    """True when the collector is not on this machine.

    Used to decide whether prompt/retrieval content may be exported. Anything
    that is not plainly loopback is treated as off-box, including private-range
    addresses — "not the internet" is not the same as "approved for content".
    """
    endpoint = collector_endpoint()
    if not endpoint:
        return False
    try:
        from urllib.parse import urlparse

        host = (urlparse(endpoint).hostname or "").lower()
        return host not in _LOCAL_HOSTS
    except Exception:
        return True


def content_tracing_enabled() -> bool:
    """Whether prompt/response/tool content may be attached to spans.

    Two gates, deliberately. ``PHOENIX_TRACE_CONTENT`` turns content capture on
    at all. When the collector is off-box — Phoenix Cloud is a third-party SaaS —
    a second, explicit ``PHOENIX_ALLOW_CONTENT_OFFBOX`` is also required, because
    Genesis prompts can carry knowledge_backbone and estate/ledger material that
    E4L has not cleared for a third party. Default posture therefore exports
    ids, counts, model names, tool names, latencies and verdicts only.
    """
    flag = _env("PHOENIX_TRACE_CONTENT").lower()
    if not flag or flag in _FALSEY:
        return False
    if endpoint_is_offbox():
        allow = _env("PHOENIX_ALLOW_CONTENT_OFFBOX").lower()
        if not allow or allow in _FALSEY:
            return False
    return True


def _load_redactor():
    """Load ``eval.redaction`` by file path, bypassing its package ``__init__``.

    ``eval/__init__.py`` imports the whole LangSmith harness. The service must
    not pull that in just to reuse one pure-Python redaction function, so the
    module is loaded directly. Returns ``None`` if unavailable, which callers
    must treat as "do not emit content at all".
    """
    try:
        import importlib.util

        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "eval", "redaction.py")
        if not os.path.isfile(path):
            return None
        spec = importlib.util.spec_from_file_location("_phoenix_redaction", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.refresh_env_secrets()
        return module
    except Exception:
        return None


def safe_content(value: Any, limit: int = 4000) -> Optional[str]:
    """Redacted, truncated string for a span, or None if content must not be sent."""
    if not content_tracing_enabled():
        return None
    redactor = _load_redactor()
    if redactor is None:
        return None
    try:
        text = value if isinstance(value, str) else repr(value)
        return redactor.redact_text(text)[:limit]
    except Exception:
        return None


def _build_tracer() -> Any:
    """Construct the OTLP tracer. Returns None on any failure."""
    global _provider
    endpoint = collector_endpoint()
    if not endpoint:
        return None

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    headers = {}
    api_key = _env("PHOENIX_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    exporter = OTLPSpanExporter(
        endpoint=f"{endpoint}/v1/traces",
        headers=headers or None,
        # Bounded so a black-holed collector cannot pin an export thread forever.
        timeout=5,
    )
    project = _env("PHOENIX_PROJECT_NAME") or DEFAULT_PROJECT
    provider = TracerProvider(
        resource=Resource.create({"openinference.project.name": project,
                                  "service.name": project})
    )
    # Batch, never Simple: export must happen off the request path so that a slow
    # or dead Phoenix adds zero latency to an agent run.
    provider.add_span_processor(
        BatchSpanProcessor(exporter, schedule_delay_millis=1000,
                           export_timeout_millis=5000)
    )
    _provider = provider
    return provider.get_tracer("genesis.agent_runtime")


def get_tracer() -> Any:
    """Cached tracer, or None when tracing is disabled/unavailable. Never raises."""
    global _tracer, _init_attempted, _disabled_reason
    if _tracer is not None:
        return _tracer
    with _lock:
        if _tracer is not None:
            return _tracer
        if _init_attempted:
            return None
        _init_attempted = True
        if not tracing_enabled():
            _disabled_reason = "no PHOENIX_COLLECTOR_ENDPOINT configured"
            return None
        try:
            _tracer = _build_tracer()
            if _tracer is None:
                _disabled_reason = "tracer construction returned None"
            return _tracer
        except Exception as exc:  # pragma: no cover - exercised via fault injection
            _disabled_reason = f"{type(exc).__name__}: {exc}"
            log.warning(
                "Phoenix tracing disabled (%s); agent execution continues normally",
                _disabled_reason,
            )
            return None


def disabled_reason() -> Optional[str]:
    return _disabled_reason


def reset_for_tests() -> None:
    """Drop cached tracer state so a test can re-read the environment."""
    global _tracer, _provider, _init_attempted, _disabled_reason
    with _lock:
        _tracer = None
        _provider = None
        _init_attempted = False
        _disabled_reason = None


def _coerce(value: Any) -> Any:
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)) and all(
        isinstance(v, (bool, int, float, str)) for v in value
    ):
        return list(value)
    return str(value)


def set_attributes(span: Any, attributes: Mapping[str, Any]) -> None:
    """Best-effort attribute write. Never raises, skips None values."""
    if span is None:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        try:
            span.set_attribute(key, _coerce(value))
        except Exception:
            continue


@contextmanager
def span(
    name: str,
    *,
    kind: str = "CHAIN",
    attributes: Optional[Mapping[str, Any]] = None,
) -> Iterator[Any]:
    """Start a Phoenix span, yielding it (or ``None`` when tracing is off).

    The yielded value is always safe to pass to :func:`set_attributes` and
    :func:`record_error`. Any tracing failure — at start, during, or at end —
    degrades to ``None`` and the caller's body still runs to completion. An
    exception raised by the caller's body is recorded (if possible) and then
    re-raised unchanged.
    """
    tracer = get_tracer()
    if tracer is None:
        yield None
        return

    ctx = None
    current = None
    try:
        ctx = tracer.start_as_current_span(name)
        current = ctx.__enter__()
        set_attributes(current, {SPAN_KIND: kind, **(attributes or {})})
    except Exception:
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass
        yield None
        return

    try:
        yield current
    except BaseException as exc:
        try:
            record_error(current, exc)
            ctx.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:
            pass
        raise
    else:
        try:
            ctx.__exit__(None, None, None)
        except Exception:
            pass


def emit_completed_span(
    name: str,
    *,
    kind: str = "TOOL",
    attributes: Optional[Mapping[str, Any]] = None,
    start_time_s: Optional[float] = None,
    end_time_s: Optional[float] = None,
    ok: bool = True,
    error_message: Optional[str] = None,
) -> Optional[str]:
    """Emit an already-finished span with its real wall-clock start/end.

    For work whose outcome is only assembled after the fact (Genesis builds one
    structured record per tool call at the end of a long branchy dispatch), this
    records true timing without threading a context manager through every branch.
    Returns the hex span id, or None. Never raises.
    """
    tracer = get_tracer()
    if tracer is None:
        return None
    try:
        from opentelemetry.trace import Status, StatusCode

        start_ns = int(start_time_s * 1e9) if start_time_s else None
        span_obj = tracer.start_span(name, start_time=start_ns)
        set_attributes(span_obj, {SPAN_KIND: kind, **(attributes or {})})
        if not ok:
            span_obj.set_status(Status(StatusCode.ERROR, (error_message or "tool failed")[:500]))
        else:
            span_obj.set_status(Status(StatusCode.OK))
        span_id = format(span_obj.get_span_context().span_id, "016x")
        span_obj.end(end_time=int(end_time_s * 1e9) if end_time_s else None)
        return span_id
    except Exception:
        return None


def record_error(span_obj: Any, exc: BaseException) -> None:
    """Mark a span as errored without leaking secret-bearing exception text."""
    if span_obj is None:
        return
    try:
        from opentelemetry.trace import Status, StatusCode

        redactor = _load_redactor()
        message = str(exc)
        message = redactor.redact_text(message) if redactor else type(exc).__name__
        span_obj.set_status(Status(StatusCode.ERROR, message[:500]))
        span_obj.set_attribute("exception.type", type(exc).__name__)
    except Exception:
        pass


def current_trace_id() -> Optional[str]:
    """Hex trace id of the active span, for correlating logs to Phoenix. None if untraced."""
    try:
        from opentelemetry import trace as _trace

        ctx = _trace.get_current_span().get_span_context()
        if not ctx or not ctx.trace_id:
            return None
        return format(ctx.trace_id, "032x")
    except Exception:
        return None


def flush(timeout_millis: int = 5000) -> bool:
    """Force-export pending spans. Bounded, best effort, never raises.

    Only for process shutdown and for tests that must assert a span landed —
    never call this on a request path.
    """
    try:
        if _provider is None:
            return False
        return bool(_provider.force_flush(timeout_millis))
    except Exception:
        return False
