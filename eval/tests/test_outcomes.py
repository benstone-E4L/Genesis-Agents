"""All five outcome classes are produced and are mutually distinguishable."""

from __future__ import annotations

import asyncio
import json

import pytest

from eval.genesis_client import GenesisClient, Outcome, RawResponse
from eval.tests.fakes import (
    FakeTransport,
    RecordingSleep,
    connect_error,
    ok_response,
    read_timeout,
)


def _client(script, **kw):
    transport = FakeTransport(script)
    client = GenesisClient(
        transport=transport,
        api_key="test-key",
        jitter=False,
        sleep=RecordingSleep(),
        **kw,
    )
    return client, transport


def _run(script, slug="genesis-research", **kw):
    client, transport = _client(script, **kw)
    result = asyncio.run(client.run_agent(slug, "summarise the AI agent market"))
    return result, transport


def test_success():
    result, transport = _run([ok_response("here is the summary")])
    assert result.outcome is Outcome.SUCCESS
    assert result.ok is True
    assert result.determinate is True
    assert result.http_status == 200
    assert result.response_text == "here is the summary"
    assert result.agent_name == "Genesis Research Agent"
    assert result.attempts == 1
    assert transport.run_call_count == 1


def test_auth_error_401():
    body = json.dumps({"detail": "Invalid or missing X-Agent-Api-Key"})
    result, transport = _run([RawResponse(401, body)])
    assert result.outcome is Outcome.AUTH_ERROR
    assert result.ok is False
    assert result.determinate is True
    assert result.http_status == 401
    assert result.error_kind == "auth_rejected"
    assert transport.run_call_count == 1, "401 must not be retried"


def test_auth_error_403():
    result, _ = _run([RawResponse(403, '{"detail":"forbidden"}')])
    assert result.outcome is Outcome.AUTH_ERROR
    assert result.http_status == 403


def test_not_found_404():
    result, transport = _run([RawResponse(404, '{"detail":"Agent not found"}')])
    assert result.outcome is Outcome.NOT_FOUND
    assert result.determinate is True
    assert result.error_kind == "slug_not_found"
    assert transport.run_call_count == 1, "404 must not be retried"


def test_upstream_error_5xx_after_retry_cap():
    result, transport = _run([RawResponse(503, "upstream unavailable")])
    assert result.outcome is Outcome.UPSTREAM_ERROR
    assert result.determinate is True
    assert result.http_status == 503
    assert result.error_kind == "server_error_503"
    assert result.attempts == 3, "attempt cap is 3 (1 initial + 2 retries)"
    assert transport.run_call_count == 3


def test_upstream_error_connect_failure_never_reached_wire():
    result, transport = _run([connect_error()])
    assert result.outcome is Outcome.UPSTREAM_ERROR
    assert result.error_kind == "transport_connect"
    assert result.http_status is None
    assert transport.run_call_count == 3, "connect errors are retried to the cap"


def test_indeterminate_read_timeout_after_send():
    """A read timeout means the bytes went out and the agent may have run.

    Reporting that as a failure is wrong, and retrying could double-invoke.
    """
    result, transport = _run([read_timeout()])
    assert result.outcome is Outcome.INDETERMINATE
    assert result.ok is False
    assert result.determinate is False, "callers must be able to skip, not fail"
    assert result.error_kind == "transport_read"
    assert transport.run_call_count == 1, "an indeterminate call is NEVER retried"


def test_indeterminate_write_timeout_is_also_unknown():
    from eval.genesis_client import TransportFailure

    result, transport = _run([TransportFailure("write", "partial body sent")])
    assert result.outcome is Outcome.INDETERMINATE
    assert transport.run_call_count == 1


def test_all_five_outcomes_are_distinct():
    scripts = {
        Outcome.SUCCESS: [ok_response()],
        Outcome.AUTH_ERROR: [RawResponse(401, "nope")],
        Outcome.NOT_FOUND: [RawResponse(404, "nope")],
        Outcome.UPSTREAM_ERROR: [RawResponse(500, "boom")],
        Outcome.INDETERMINATE: [read_timeout()],
    }
    observed = {}
    for expected, script in scripts.items():
        result, _ = _run(script)
        observed[expected] = result.outcome
    assert observed == {k: k for k in scripts}, observed
    assert len(set(observed.values())) == 5


def test_5xx_recovers_on_retry():
    result, transport = _run([RawResponse(502, "bad gateway"), ok_response("recovered")])
    assert result.outcome is Outcome.SUCCESS
    assert result.attempts == 2
    assert result.response_text == "recovered"
    assert transport.run_call_count == 2


def test_elapsed_ms_is_recorded():
    ticks = iter([0.0, 1.25])
    client, _ = _client([ok_response()])
    client._clock = lambda: next(ticks)
    result = asyncio.run(client.run_agent("genesis-research", "task"))
    assert result.elapsed_ms == 1250


def test_default_timeout_is_above_the_30s_render_proxy_timeout():
    client, transport = _client([ok_response()])
    asyncio.run(client.run_agent("genesis-research", "task"))
    assert transport.calls[0]["timeout_s"] == 60.0
    assert transport.calls[0]["timeout_s"] > 30.0
