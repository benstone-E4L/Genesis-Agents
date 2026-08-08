"""LangSmith instrumentation for Genesis agent runs.

Why the plain ``@traceable`` decorator and not an SDK integration: Genesis is a
FastAPI service that makes raw HTTP calls to the SwarmSync router. There is no
LangChain, no OpenAI SDK and no Claude Agent SDK in the call path, so there is
nothing for ``wrap_openai`` / ``configure_claude_agent_sdk`` to hook.
``@traceable`` wraps any Python callable regardless of what is underneath, which
is exactly the shape of this problem.

Two hard guarantees:

1. **No secret ever reaches a trace.** Inputs, outputs, metadata and exception
   text all pass through :func:`eval.redaction.redact` before the tracing layer
   can see them.
2. **Tracing never becomes a dependency of execution.** If ``LANGSMITH_API_KEY``
   is missing, ``langsmith`` is not installed, or the LangSmith backend is
   unreachable, the agent call still runs and its result is still returned.
"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable, Mapping

from .genesis_client import AgentRunResult, GenesisClient, Outcome
from .redaction import redact, redact_text, refresh_env_secrets

RUN_NAME = "genesis.agent.run"
RUN_TYPE = "chain"

#: Test seam. When set, used instead of ``langsmith.traceable``. Must be a
#: decorator factory with the same shape: ``factory(**kwargs) -> decorator``.
TRACEABLE_FACTORY: Callable[..., Callable[[Callable], Callable]] | None = None

#: Test seam. When set, used instead of ``langsmith.run_helpers``' run-tree
#: lookup. Must return an object with a mutable ``.metadata`` mapping, or None.
RUN_TREE_GETTER: Callable[[], Any] | None = None

_FALSEY = {"0", "false", "no", "off", ""}


def tracing_enabled() -> bool:
    """True only when LangSmith is both configured and not explicitly disabled.

    Requires ``LANGSMITH_API_KEY``. ``LANGSMITH_TRACING`` may be used to turn
    tracing off while the key stays in the environment.
    """
    if not (os.getenv("LANGSMITH_API_KEY") or "").strip():
        return False
    flag = os.getenv("LANGSMITH_TRACING")
    if flag is not None and flag.strip().lower() in _FALSEY:
        return False
    return True


def _resolve_traceable_factory() -> Callable[..., Callable[[Callable], Callable]] | None:
    if TRACEABLE_FACTORY is not None:
        return TRACEABLE_FACTORY
    try:
        from langsmith import traceable as ls_traceable
    except Exception:
        return None
    return ls_traceable


def _wrap_traceable(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Decorate ``fn`` with ``@traceable`` if possible, else return it unchanged.

    Any failure at decoration time (missing package, incompatible signature,
    exploding factory) degrades to the undecorated function.
    """
    if not tracing_enabled():
        return fn
    factory = _resolve_traceable_factory()
    if factory is None:
        return fn
    for kwargs in (
        # Belt and braces: newer langsmith versions can redact again on the way
        # in and out. Older ones do not accept these kwargs, hence the fallback.
        {"run_type": RUN_TYPE, "name": RUN_NAME,
         "process_inputs": redact, "process_outputs": redact},
        {"run_type": RUN_TYPE, "name": RUN_NAME},
    ):
        try:
            return factory(**kwargs)(fn)
        except Exception:
            continue
    return fn


def _attach_metadata(metadata: Mapping[str, Any]) -> None:
    """Attach metadata to the active run. Silent no-op if there is no run."""
    try:
        getter = RUN_TREE_GETTER
        if getter is None:
            from langsmith.run_helpers import get_current_run_tree

            getter = get_current_run_tree
        run_tree = getter()
        if run_tree is None:
            return
        current = getattr(run_tree, "metadata", None)
        if current is None:
            run_tree.metadata = dict(redact(dict(metadata)))
        else:
            current.update(redact(dict(metadata)))
    except Exception:
        # Observability must never break execution.
        return


def _redacted_copy(exc: BaseException) -> BaseException:
    """Rebuild an exception with its message redacted, preserving the type."""
    safe = redact_text(str(exc))
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
    """Invoke a Genesis agent inside a LangSmith run named ``genesis.agent.run``.

    Returns the same :class:`AgentRunResult` the client returns. Tracing is
    strictly additive: with no ``LANGSMITH_API_KEY`` this is a plain call.
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

    holder: dict[str, AgentRunResult] = {}

    async def _core(inputs: Mapping[str, Any]) -> dict[str, Any]:
        try:
            result = await client.run_agent(
                slug,
                task,
                mode=mode,
                require_artifact=require_artifact,
                **run_kwargs,
            )
        except Exception as exc:
            # Redact INSIDE the traced scope. @traceable records the raised
            # exception on the run, so an unredacted message would be shipped
            # to LangSmith before any outer handler could touch it.
            raise _redacted_copy(exc) from None
        holder["result"] = result
        _attach_metadata(build_metadata(result, extra_metadata))
        return build_outputs(result)

    runner = _wrap_traceable(_core)

    try:
        await runner(safe_inputs)
    except Exception as exc:
        if "result" in holder:
            # The agent call succeeded and the tracing layer failed afterwards.
            # Degrade: return the real result rather than surfacing an
            # observability error as an agent error.
            return holder["result"]
        # A genuine failure from the call path. Re-raise with a redacted
        # message so no credential can ride out inside the traceback text.
        raise _redacted_copy(exc) from None

    return holder["result"]


__all__ = [
    "RUN_NAME",
    "RUN_TYPE",
    "AgentRunResult",
    "Outcome",
    "build_metadata",
    "build_outputs",
    "redact",
    "traced_agent_run",
    "tracing_enabled",
]
