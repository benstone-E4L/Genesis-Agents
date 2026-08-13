"""Arize Phoenix tracing for Genesis evaluation runs.

Phoenix is the tracing and evaluation backend. LangSmith has been removed
entirely — there is no dormant second path, no ``LANGSMITH_*`` variable, and no
``langsmith`` dependency. Anything that reads like a LangSmith fallback in an
older revision of this file is gone on purpose: a dormant second exporter is a
second place secrets can leak from, and "it only ships when the key is set" is
one stray environment variable away from shipping.

What this layer records, and where the rest comes from
------------------------------------------------------
This module traces the harness's view: one ``genesis.agent.run`` AGENT span per
invocation carrying slug resolution, mode, latency, HTTP status, attempt count
and outcome — plus, via :func:`emit_verdict_spans`, one EVALUATOR span per
rubric verdict. Verdict spans are what replace LangSmith's feedback API; without
them, dropping LangSmith would silently lose the scores.

Model ids, token usage, per-tool spans and tool outcomes are emitted by the
*service*, in ``runtime/phoenix_tracing.py`` and ``agent_runtime.py``, because
that is the only place those values exist — the gateway's ``RunResponse`` does
not carry them. Both layers export to the same Phoenix project and correlate by
trace id, so the capability set is preserved end to end rather than duplicated.

Two hard guarantees, unchanged from the LangSmith implementation:

1. **No secret ever reaches a trace.** Inputs, outputs, metadata, verdict
   comments and exception text all pass through :func:`eval.redaction.redact`
   before the tracing layer sees them, and free-text fields are additionally
   withheld unless content tracing is explicitly enabled — ``redact`` removes
   *secrets*, but an agent response can still quote confidential E4L material
   that has not been cleared for a third-party backend.
2. **Tracing never becomes a dependency of execution.** With no Phoenix
   collector configured, with an unreachable collector, or with a broken
   OpenTelemetry install, the agent call still runs and its result is still
   returned.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from .genesis_client import AgentRunResult, GenesisClient, Outcome
from .redaction import redact, redact_text, refresh_env_secrets

RUN_NAME = "genesis.agent.run"
RUN_TYPE = "chain"
#: Span name for one rubric verdict. One span per verdict, not one per example,
#: so Phoenix can aggregate a single rubric across a whole dataset.
VERDICT_SPAN_NAME = "genesis.eval.verdict"


def phoenix_enabled() -> bool:
    """True when Arize Phoenix is configured as the tracing backend."""
    try:
        from runtime import phoenix_tracing

        return phoenix_tracing.tracing_enabled()
    except Exception:
        return False


def tracing_enabled() -> bool:
    """True when eval runs will be traced.

    Phoenix is the only backend, so this is exactly "is Phoenix configured".
    ``PHOENIX_TRACING`` set to a falsey value turns tracing off while the
    endpoint stays in place.
    """
    return phoenix_enabled()


#: Attributes from :func:`build_metadata` that are safe to publish verbatim —
#: ids, counts, latencies, verdicts. Everything else in the metadata dict is
#: withheld unless content tracing is explicitly enabled.
_PHOENIX_SAFE_METADATA_KEYS = (
    "slug", "requested_slug", "slug_resolution", "mode", "elapsed_ms",
    "http_status", "attempts", "outcome", "determinate", "error_kind", "warmed",
)


def _emit_phoenix_span(
    result: AgentRunResult,
    extra_metadata: Mapping[str, Any] | None,
    inputs: Mapping[str, Any],
) -> None:
    """Record the eval run as an OpenInference AGENT span. Never raises."""
    try:
        from runtime import phoenix_tracing

        metadata = build_metadata(result, extra_metadata)
        outputs = build_outputs(result)
        attributes: dict[str, Any] = {
            f"genesis.eval.{key}": metadata.get(key)
            for key in _PHOENIX_SAFE_METADATA_KEYS
        }
        attributes.update({
            "genesis.eval.ok": outputs.get("ok"),
            "genesis.eval.agent_name": outputs.get("agent_name"),
            # Free text: gated, and redacted again on the way out.
            phoenix_tracing.INPUT_VALUE: phoenix_tracing.safe_content(
                redact_text(str(inputs.get("task", "")))
            ),
            phoenix_tracing.OUTPUT_VALUE: phoenix_tracing.safe_content(
                redact_text(str(outputs.get("response") or ""))
            ),
            "genesis.eval.error_message": phoenix_tracing.safe_content(
                redact_text(str(outputs.get("error_message") or ""))
            ),
        })
        with phoenix_tracing.span(RUN_NAME, kind="AGENT", attributes=attributes):
            pass
    except Exception:
        # Observability must never break execution.
        return


def build_verdict_attributes(verdict: Any, *, example_id: str = "", slug: str = "") -> dict[str, Any]:
    """Span attributes for one rubric verdict.

    ``score`` is published both raw and normalised: raw so a rubric's own scale
    is readable, normalised so rubrics with different maxima are comparable on
    one dashboard. ``comment`` is judge free text about the agent's answer and
    can quote it, so it goes through the same content gate as the response
    itself rather than being treated as metadata.
    """
    try:
        from runtime import phoenix_tracing

        score = getattr(verdict, "score", None)
        maximum = getattr(verdict, "max_score", 0) or 0
        attributes: dict[str, Any] = {
            "genesis.eval.example_id": example_id,
            "genesis.eval.slug": slug,
            "genesis.eval.rubric": getattr(verdict, "rubric", ""),
            "genesis.eval.dimension": getattr(verdict, "dimension", ""),
            "genesis.eval.status": getattr(verdict, "status", ""),
            "genesis.eval.source": getattr(verdict, "source", ""),
            "genesis.eval.max_score": maximum,
        }
        if score is not None:
            attributes["genesis.eval.score"] = score
            if maximum:
                attributes["genesis.eval.score_normalised"] = score / maximum
        comment = phoenix_tracing.safe_content(
            redact_text(str(getattr(verdict, "comment", "") or ""))
        )
        if comment is not None:
            attributes["genesis.eval.comment"] = comment
        return attributes
    except Exception:
        return {}


def emit_verdict_spans(score: Any) -> None:
    """Emit one EVALUATOR span per verdict in an ``ExampleScore``. Never raises.

    This is the Phoenix replacement for LangSmith feedback. Called by the
    experiment driver after scoring so verdicts land next to the run they judge.
    """
    try:
        from runtime import phoenix_tracing

        example_id = str(getattr(score, "example_id", "") or "")
        slug = str(getattr(score, "slug", "") or "")
        for verdict in getattr(score, "verdicts", ()) or ():
            attributes = build_verdict_attributes(
                verdict, example_id=example_id, slug=slug
            )
            if not attributes:
                continue
            with phoenix_tracing.span(
                VERDICT_SPAN_NAME, kind="EVALUATOR", attributes=attributes
            ):
                pass
    except Exception:
        return


def _redacted_copy(exc: BaseException) -> BaseException:
    """Rebuild an exception with its message redacted, preserving the type.

    Redaction itself must not be allowed to raise. This is called from inside an
    ``except`` block, so an exception escaping here is chained to the original by
    Python and the original's UNREDACTED message is printed in the traceback —
    the exact secret this function exists to remove, leaked by the act of
    removing it. ``str(exc)`` is equally untrusted: a custom ``__str__`` that
    raises has the same effect. Both are contained, and the fallback drops the
    message entirely rather than guessing at it.
    """
    try:
        safe = redact_text(str(exc))
    except BaseException:
        return RuntimeError(f"{type(exc).__name__}: [message withheld — redaction failed]")
    try:
        return type(exc)(safe)
    except Exception:
        return RuntimeError(f"{type(exc).__name__}: {safe}")


def build_metadata(result: AgentRunResult, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The trace metadata contract. Everything here is non-secret by construction."""
    meta: dict[str, Any] = {
        "slug": result.slug,
        "requested_slug": result.requested_slug,
        "slug_resolution": result.slug_resolution,
        # Which path was measured. live_test skips AgentRuntime/ConduitBridge
        # and uses the fast persona LLM path; full exercises the real runtime.
        # These are NOT the same measurement, so it is recorded explicitly.
        "mode": result.mode,
        "elapsed_ms": result.elapsed_ms,
        "http_status": result.http_status,
        "attempts": result.attempts,
        "outcome": result.outcome.value,
        "determinate": result.determinate,
        "error_kind": result.error_kind,
        "warmed": result.warmed,
    }
    if extra:
        meta.update(dict(extra))
    return redact(meta)


def build_outputs(result: AgentRunResult) -> dict[str, Any]:
    """The traced run's outputs. Redacted."""
    return redact(
        {
            "outcome": result.outcome.value,
            "ok": result.ok,
            "determinate": result.determinate,
            "response": result.response_text,
            "agent_name": result.agent_name,
            "http_status": result.http_status,
            "elapsed_ms": result.elapsed_ms,
            "attempts": result.attempts,
            "error_kind": result.error_kind,
            "error_message": result.error_message,
        }
    )


async def traced_agent_run(
    client: GenesisClient,
    *,
    slug: str,
    task: Any,
    mode: str = "live_test",
    require_artifact: bool = False,
    extra_metadata: Mapping[str, Any] | None = None,
    **run_kwargs: Any,
) -> AgentRunResult:
    """Invoke a Genesis agent inside a Phoenix span named ``genesis.agent.run``.

    Returns the same :class:`AgentRunResult` the client returns. Tracing is
    strictly additive: with no Phoenix collector configured this is a plain call.
    """
    refresh_env_secrets()

    safe_inputs = redact(
        {
            "slug": slug,
            "task": task,
            "mode": mode,
            "require_artifact": require_artifact,
        }
    )

    try:
        result = await client.run_agent(
            slug,
            task,
            mode=mode,
            require_artifact=require_artifact,
            **run_kwargs,
        )
    except Exception as exc:
        # Redact before the exception can reach a span, a log or an outer
        # handler — an unredacted message would otherwise be shipped verbatim.
        raise _redacted_copy(exc) from None

    _emit_phoenix_span(result, extra_metadata, safe_inputs)
    return result


__all__ = [
    "RUN_NAME",
    "RUN_TYPE",
    "VERDICT_SPAN_NAME",
    "AgentRunResult",
    "Outcome",
    "build_metadata",
    "build_outputs",
    "build_verdict_attributes",
    "emit_verdict_spans",
    "phoenix_enabled",
    "redact",
    "traced_agent_run",
    "tracing_enabled",
]
