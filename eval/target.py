"""The evaluation target LangSmith calls once per dataset example.

``evaluate()`` hands the target one dict (the example's ``inputs``) and stores
whatever dict comes back as the run's ``outputs``. Evaluators/rubrics then read
those output keys. Both halves of that contract are pinned below — the dataset
author and the rubric author must both key off this schema.

INPUT SCHEMA (dataset example ``inputs``)
-----------------------------------------
==================  ========  =========================================================
key                 required  meaning
==================  ========  =========================================================
``slug``            yes       Agent to invoke. Any known spelling is accepted and
                              normalised to the LIVE gateway form: the hyphenated
                              bundle slug ``genesis-research`` and the live slug
                              ``genesis_research_x402`` both work. The resolved value
                              is echoed back as ``slug``.
``task``            yes*      The prompt / instruction. ``prompt`` and ``input`` are
                              accepted aliases; exactly one must be present. A
                              non-string value is sent as the structured ``input``
                              field instead of ``prompt``.
``mode``            no        ``"live_test"`` (default) or ``"full"``.
                              ``live_test`` sets ``mode: "live_test"`` +
                              ``testContext: true``, which skips AgentRuntime /
                              ConduitBridge startup and uses the fast persona LLM
                              path — required on the Render free tier (30s proxy
                              timeout). ``full`` exercises the real runtime and may
                              exceed that timeout or return an async job envelope.
                              The two measure DIFFERENT things; the value used is
                              always echoed in the outputs and the trace metadata.
``require_artifact``no        bool, forwarded to the gateway. Default False.
``metadata``        no        Free-form dict merged into the trace metadata. Redacted.
==================  ========  =========================================================

Unknown keys are ignored, so the dataset may carry rubric-only fields
(``expected``, ``criteria``, ``category``, ...) in the same ``inputs`` dict.

OUTPUT SCHEMA (what evaluators receive as ``outputs``)
------------------------------------------------------
====================  ==============================================================
key                   meaning
====================  ==============================================================
``response``          str. The agent's answer text. ``""`` when there is none.
``outcome``           str. One of ``success`` | ``auth_error`` | ``not_found`` |
                      ``upstream_error`` | ``indeterminate``.
``ok``                bool. ``outcome == "success"``.
``determinate``       bool. ``False`` only for ``indeterminate``. **Rubrics must
                      skip, not fail, an example where this is False** — the call
                      reached the wire and the result is unknown, which is not
                      evidence the agent is bad.
``slug``              str. The resolved LIVE slug actually invoked.
``requested_slug``    str. What the dataset asked for.
``slug_resolution``   str. ``verified`` | ``aliased`` | ``unverified``.
``mode``              str. ``live_test`` | ``full``.
``http_status``       int | None.
``elapsed_ms``        int.
``attempts``          int. 1 unless a retry happened.
``error_kind``        str | None. e.g. ``auth_rejected``, ``server_error_503``,
                      ``transport_read``, ``blocked_money_domain``.
``error_message``     str | None. Redacted.
``agent_name``        str | None. From the gateway's ``agentName`` field.
====================  ==============================================================
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Mapping

from .genesis_client import (
    AgentRunResult,
    GenesisClient,
    MoneyDomainBlocked,
    Outcome,
    UnknownSlug,
)
from .redaction import redact, redact_text
from .traceable import traced_agent_run

_TASK_KEYS = ("task", "prompt", "input", "question", "query")

_default_client: GenesisClient | None = None


class InvalidExample(ValueError):
    """The dataset example does not satisfy the documented input schema."""


def parse_example(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a dataset example against the input schema documented above."""
    if not isinstance(inputs, Mapping):
        raise InvalidExample("example inputs must be a mapping")

    slug = inputs.get("slug")
    if not isinstance(slug, str) or not slug.strip():
        raise InvalidExample("example inputs require a non-empty 'slug'")

    task: Any = None
    for key in _TASK_KEYS:
        value = inputs.get(key)
        if value is not None and value != "":
            task = value
            break
    if task is None:
        raise InvalidExample(
            "example inputs require one of: " + ", ".join(_TASK_KEYS)
        )

    mode = inputs.get("mode") or "live_test"
    if mode not in ("live_test", "full"):
        raise InvalidExample("mode must be 'live_test' or 'full'")

    extra = inputs.get("metadata")
    return {
        "slug": slug.strip(),
        "task": task,
        "mode": mode,
        "require_artifact": bool(inputs.get("require_artifact", False)),
        "extra_metadata": dict(extra) if isinstance(extra, Mapping) else None,
    }


def result_to_outputs(result: AgentRunResult) -> dict[str, Any]:
    """Project an :class:`AgentRunResult` onto the documented output schema."""
    return redact(
        {
            "response": result.response_text,
            "outcome": result.outcome.value,
            "ok": result.ok,
            "determinate": result.determinate,
            "slug": result.slug,
            "requested_slug": result.requested_slug,
            "slug_resolution": result.slug_resolution,
            "mode": result.mode,
            "http_status": result.http_status,
            "elapsed_ms": result.elapsed_ms,
            "attempts": result.attempts,
            "error_kind": result.error_kind,
            "error_message": result.error_message,
            "agent_name": result.agent_name,
        }
    )


def _blocked_outputs(inputs: Mapping[str, Any], exc: Exception, kind: str) -> dict[str, Any]:
    return redact(
        {
            "response": "",
            "outcome": Outcome.UPSTREAM_ERROR.value,
            "ok": False,
            "determinate": True,
            "slug": str(inputs.get("slug", "")),
            "requested_slug": str(inputs.get("slug", "")),
            "slug_resolution": "unverified",
            "mode": str(inputs.get("mode") or "live_test"),
            "http_status": None,
            "elapsed_ms": 0,
            "attempts": 0,
            "error_kind": kind,
            "error_message": redact_text(str(exc)),
            "agent_name": None,
        }
    )


async def arun_example(
    inputs: Mapping[str, Any],
    *,
    client: GenesisClient,
) -> dict[str, Any]:
    """Async form of the target. Injectable client — no live gateway required."""
    try:
        parsed = parse_example(inputs)
    except InvalidExample as exc:
        return _blocked_outputs(inputs, exc, "invalid_example")

    try:
        result = await traced_agent_run(client, **parsed)
    except MoneyDomainBlocked as exc:
        return _blocked_outputs(inputs, exc, "blocked_money_domain")
    except UnknownSlug as exc:
        # Not a 404 — the gateway would have answered with a generic persona
        # and returned 200. Surfaced as a row-level error, not an exception, so
        # one bad dataset row cannot kill the whole experiment.
        return _blocked_outputs(inputs, exc, "unknown_slug")

    return result_to_outputs(result)


def _run_sync(coro: Any) -> Any:
    """Run a coroutine whether or not an event loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def make_target(client: GenesisClient) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Build a sync LangSmith target bound to an explicit client.

    This is the form to use in unit tests and in any harness that supplies its
    own transport::

        target = make_target(GenesisClient(transport=FakeTransport(...)))
        outputs = target({"slug": "genesis-research", "task": "..."})
    """

    def target(inputs: Mapping[str, Any]) -> dict[str, Any]:
        return _run_sync(arun_example(inputs, client=client))

    target.__name__ = "genesis_target"
    return target


def make_async_target(
    client: GenesisClient,
) -> Callable[[Mapping[str, Any]], Any]:
    """Build an async LangSmith target bound to an explicit client."""

    async def atarget(inputs: Mapping[str, Any]) -> dict[str, Any]:
        return await arun_example(inputs, client=client)

    atarget.__name__ = "genesis_target"
    return atarget


def get_default_client() -> GenesisClient:
    """Process-wide client against the live gateway. Built on first use."""
    global _default_client
    if _default_client is None:
        _default_client = GenesisClient()
    return _default_client


def set_default_client(client: GenesisClient | None) -> None:
    """Replace (or clear) the process-wide client. Used by tests and harnesses."""
    global _default_client
    _default_client = client


def genesis_target(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Default sync target for ``langsmith.evaluate(genesis_target, data=...)``.

    Uses the process-wide client, i.e. the live gateway with credentials from
    the environment. Prefer :func:`make_target` when you want to inject one.
    """
    return _run_sync(arun_example(inputs, client=get_default_client()))


async def agenesis_target(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Default async target for ``langsmith.aevaluate``."""
    return await arun_example(inputs, client=get_default_client())


__all__ = [
    "InvalidExample",
    "agenesis_target",
    "arun_example",
    "genesis_target",
    "get_default_client",
    "make_async_target",
    "make_target",
    "parse_example",
    "result_to_outputs",
    "set_default_client",
]
